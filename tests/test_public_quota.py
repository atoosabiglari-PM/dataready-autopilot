"""Anonymous NVIDIA inference budget controls, without customer access codes."""

import pytest

from app.services import public_quota as quota


@pytest.fixture(autouse=True)
def isolated_quota(monkeypatch):
    for name in (
        "K_SERVICE",
        "DATAREADY_PUBLIC_INFERENCE_ENABLED",
        "DATAREADY_PUBLIC_DAILY_LIMIT",
        "DATAREADY_PUBLIC_SESSION_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)
    with quota._LOCAL_LOCK:
        quota._LOCAL_COUNTS.clear()
    yield
    with quota._LOCAL_LOCK:
        quota._LOCAL_COUNTS.clear()


def test_local_customer_can_use_nvidia_without_any_code():
    # A normal user only receives a browser session automatically.
    quota.reserve_public_inference("anonymous_customer_abcdefghijklmnop")


def test_fair_use_is_enforced_per_browser():
    identity = "anonymous_browser_0123456789abcdefghijkl"
    for _ in range(quota.DEFAULT_SESSION_DAILY_LIMIT):
        quota.reserve_public_inference(identity)
    with pytest.raises(quota.PublicQuotaExceeded, match="browser"):
        quota.reserve_public_inference(identity)


def test_global_daily_quota_cannot_be_evaded_with_new_sessions():
    for i in range(quota.DEFAULT_GLOBAL_DAILY_LIMIT):
        quota.reserve_public_inference(f"anonymous_guest_{i:04d}_abcdefghijkl")
    with pytest.raises(quota.PublicQuotaExceeded, match="public NVIDIA"):
        quota.reserve_public_inference("new_guest_beyond_the_global_budget")


def test_operator_may_lower_limits_but_cannot_silently_raise_hard_caps(monkeypatch):
    monkeypatch.setenv("DATAREADY_PUBLIC_DAILY_LIMIT", "2")
    monkeypatch.setenv("DATAREADY_PUBLIC_SESSION_LIMIT", "1")
    quota.reserve_public_inference("local_session_first_abcdefgh")
    with pytest.raises(quota.PublicQuotaExceeded, match="browser"):
        quota.reserve_public_inference("local_session_first_abcdefgh")
    quota.reserve_public_inference("local_session_second_abcdefgh")
    with pytest.raises(quota.PublicQuotaExceeded, match="public NVIDIA"):
        quota.reserve_public_inference("local_session_third_abcdefgh")

    monkeypatch.setenv(
        "DATAREADY_PUBLIC_DAILY_LIMIT",
        str(quota.DEFAULT_GLOBAL_DAILY_LIMIT + 1),
    )
    with pytest.raises(quota.PublicQuotaUnavailable, match="within approved bounds"):
        quota.reserve_public_inference("brand_new_guest_abcdefghijklmnop")


def test_an_operator_can_disable_public_inference(monkeypatch):
    monkeypatch.setenv("DATAREADY_PUBLIC_INFERENCE_ENABLED", "false")
    with pytest.raises(quota.PublicQuotaUnavailable, match="disabled"):
        quota.reserve_public_inference("guest_with_inference_disabled")


def test_hosted_inference_fails_closed_until_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "dataready-live")
    with pytest.raises(quota.PublicQuotaUnavailable, match="not been enabled"):
        quota.reserve_public_inference("anonymous_cloud_browser_abcdefghijkl")


def test_hosted_inference_always_uses_durable_atomic_reservation(monkeypatch):
    observed = []

    def record(day, identity, total, per_visitor):
        observed.append((day, identity, total, per_visitor))

    monkeypatch.setenv("K_SERVICE", "dataready-live")
    monkeypatch.setenv("DATAREADY_PUBLIC_INFERENCE_ENABLED", "true")
    monkeypatch.setattr(quota, "_reserve_firestore", record)
    quota.reserve_public_inference("guest_in_cloud_abcdefghijklmnop")
    assert len(observed) == 1
    day, identity, total, per_visitor = observed[0]
    assert len(day) == 10
    assert len(identity) == 64  # Hash, not a raw session identifier.
    assert "guest_in_cloud" not in identity
    assert total == quota.DEFAULT_GLOBAL_DAILY_LIMIT
    assert per_visitor == quota.DEFAULT_SESSION_DAILY_LIMIT
    assert quota._LOCAL_COUNTS == {}


def test_firestore_unavailable_cannot_fall_back_to_local_quota(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "dataready-live")
    monkeypatch.setenv("DATAREADY_PUBLIC_INFERENCE_ENABLED", "true")

    def broken(*args):
        raise quota.PublicQuotaUnavailable("Firestore is unavailable.")

    monkeypatch.setattr(quota, "_reserve_firestore", broken)
    with pytest.raises(quota.PublicQuotaUnavailable):
        quota.reserve_public_inference("guest_firestorm_abcdefghijklmnop")
    assert quota._LOCAL_COUNTS == {}


def test_invalid_browser_identity_does_not_reserve_capacity():
    with pytest.raises(quota.PublicQuotaUnavailable):
        quota.reserve_public_inference("short")
    assert quota._LOCAL_COUNTS == {}
