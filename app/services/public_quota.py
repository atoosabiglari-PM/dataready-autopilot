"""Bounded, server-side public inference quotas.

No access codes or provider API credentials are requested from customers.
For Cloud Run, every paid inference requires a Firestore transaction so quota
checks remain atomic across instances and restarts. If Firestore is unavailable,
inference fails closed. Local development uses a process-local quota only.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from threading import Lock

DEFAULT_GLOBAL_DAILY_LIMIT = 50
DEFAULT_SESSION_DAILY_LIMIT = 5
_COLLECTION = "dataready_nebius_quotas"
_LOCAL_COUNTS: dict[str, int] = {}
_LOCAL_LOCK = Lock()


class PublicQuotaExceeded(RuntimeError):
    """The customer/session or global demo inference budget was exhausted."""


class PublicQuotaUnavailable(RuntimeError):
    """Public model inference is disabled or the durable quota store failed."""


def _limit(env: str, default: int) -> int:
    """Allow operators to lower usage ceilings without setting unlimited values."""
    value = os.getenv(env)
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise PublicQuotaUnavailable("Public inference limit is misconfigured.") from exc
    if not 1 <= number <= default:
        raise PublicQuotaUnavailable("Public inference limits must be within approved bounds.")
    return number


def _reserve_local(day: str, identity: str, daily: int, per_session: int) -> None:
    """Developer-only process limit; never used for a hosted deployment."""
    with _LOCAL_LOCK:
        global_key = f"global:{day}"
        session_key = f"session:{day}:{identity}"
        if _LOCAL_COUNTS.get(global_key, 0) >= daily:
            raise PublicQuotaExceeded("Today's public NVIDIA demo allowance is exhausted.")
        if _LOCAL_COUNTS.get(session_key, 0) >= per_session:
            raise PublicQuotaExceeded("This browser has reached today's NVIDIA demo allowance.")
        _LOCAL_COUNTS[global_key] = _LOCAL_COUNTS.get(global_key, 0) + 1
        _LOCAL_COUNTS[session_key] = _LOCAL_COUNTS.get(session_key, 0) + 1


def _reserve_firestore(day: str, identity: str, daily: int, per_session: int) -> None:
    """Commit one inference reservation atomically across Cloud Run instances."""
    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT")
    if not project:
        raise PublicQuotaUnavailable("Cloud project configuration is unavailable.")

    try:
        from google.cloud import firestore

        client = firestore.Client(project=project)
        global_doc = client.collection(_COLLECTION).document("global_" + day)
        session_doc = client.collection(_COLLECTION).document(
            "session_" + day + "_" + identity
        )

        @firestore.transactional
        def reserve(transaction):
            # All reads MUST precede all writes in a Firestore transaction.
            global_snapshot = global_doc.get(transaction=transaction)
            session_snapshot = session_doc.get(transaction=transaction)
            global_count = int((global_snapshot.to_dict() or {}).get("count", 0))
            session_count = int((session_snapshot.to_dict() or {}).get("count", 0))

            if global_count >= daily:
                raise PublicQuotaExceeded(
                    "Today's public NVIDIA demo allowance is exhausted."
                )
            if session_count >= per_session:
                raise PublicQuotaExceeded(
                    "This browser has reached today's NVIDIA demo allowance."
                )

            transaction.set(global_doc, {"count": global_count + 1})
            transaction.set(session_doc, {"count": session_count + 1})

        reserve(client.transaction(max_attempts=5))
    except PublicQuotaExceeded:
        raise
    except Exception as exc:
        raise PublicQuotaUnavailable(
            "The server cannot safely reserve a public NVIDIA request."
        ) from exc


def reserve_public_inference(session_id: str) -> None:
    """Reserve capacity BEFORE any billable Nebius inference can start.

    Per-browser limits improve fairness; an independently enforced global
    Firestore cap prevents unlimited calls even if cookies are replaced.
    """
    if not isinstance(session_id, str) or not 16 <= len(session_id) <= 100:
        raise PublicQuotaUnavailable("Anonymous browser session is invalid.")
    if os.getenv("DATAREADY_PUBLIC_INFERENCE_ENABLED", "").lower() in {
        "false", "0", "off"
    }:
        raise PublicQuotaUnavailable("Public NVIDIA analysis is temporarily disabled.")

    daily = _limit("DATAREADY_PUBLIC_DAILY_LIMIT", DEFAULT_GLOBAL_DAILY_LIMIT)
    per_session = _limit(
        "DATAREADY_PUBLIC_SESSION_LIMIT", DEFAULT_SESSION_DAILY_LIMIT
    )

    # Never fall back to a process counter in a hosted deployment.
    hosted = bool(os.getenv("K_SERVICE"))
    if hosted and os.getenv("DATAREADY_PUBLIC_INFERENCE_ENABLED", "").lower() != "true":
        raise PublicQuotaUnavailable(
            "Public NVIDIA analysis has not been enabled for this cloud service."
        )

    today = datetime.now(timezone.utc).date().isoformat()
    # Do not store the browser session identifier in Firestore.
    hashed = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    if hosted:
        _reserve_firestore(today, hashed, daily, per_session)
    else:
        _reserve_local(today, hashed, daily, per_session)
