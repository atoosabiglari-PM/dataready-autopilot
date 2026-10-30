#!/usr/bin/env bash
# Deploy the REAL customer-upload DataReady app, not the fixed replay.
# Paid inference uses a durable Firestore quota; provider keys never enter Git.
set -euo pipefail
set +x

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PROJECT="dataready-autopilot-atoosa"
REGION="us-west1"
SERVICE="dataready-live"
ACCOUNT_NAME="dataready-live-runtime"
RUNTIME_ACCOUNT="$ACCOUNT_NAME@$PROJECT.iam.gserviceaccount.com"
NEBIUS_SECRET="NEBIUS_API_KEY"
SIGNING_SECRET="DATAREADY_APPROVAL_SIGNING_KEY"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# A newly enabled Google service can update IAM concurrently. Retry the whole
# gcloud read-modify-write operation with a fresh policy/ETag on each attempt.
# Permission errors and other non-conflict failures still stop immediately.
retry_iam_update() {
  local attempt=1 delay=2 exit_status error_file wait_seconds
  error_file="$(mktemp)"
  while true; do
    if "$@" 2>"$error_file"; then
      cat "$error_file" >&2
      rm -f "$error_file"
      return 0
    else
      exit_status=$?
    fi
    cat "$error_file" >&2
    if [[ "$attempt" -ge 5 ]] || ! grep -Eqi \
      'concurrent policy changes|ETag.*did not match|ABORTED|409.*conflict' "$error_file"; then
      rm -f "$error_file"
      return "$exit_status"
    fi
    printf -v wait_seconds '%d.%03d' "$delay" "$((RANDOM % 1000))"
    printf 'Temporary IAM conflict; retrying attempt %s/5 in %s seconds...\n' \
      "$((attempt + 1))" "$wait_seconds" >&2
    sleep "$wait_seconds"
    attempt=$((attempt + 1))
    delay=$((delay * 2))
  done
}

for command_name in gcloud openssl curl; do
  command -v "$command_name" >/dev/null 2>&1 || die "$command_name is required."
done
[[ -x .venv/bin/python ]] || die "The project's .venv/bin/python is missing."
[[ -f .env ]] || die "An ignored local .env with NEBIUS_API_KEY is required."

# Read the local .env as data, never execute it as shell commands.
.venv/bin/python - <<'PY' || exit 1
from dotenv import dotenv_values
key = dotenv_values(".env").get("NEBIUS_API_KEY")
if not key or not str(key).strip():
    raise SystemExit("ERROR: NEBIUS_API_KEY is missing from local .env.")
PY

ACCOUNT="$(gcloud auth list --filter='status:ACTIVE' --format='value(account)' | head -n 1)"
[[ -n "$ACCOUNT" ]] || die "Run gcloud auth login first."
printf '\nDataReady Live deployment\n'
printf 'Google account: %s\nProject: %s\nRegion: %s\nNew service: %s\n' \
  "$ACCOUNT" "$PROJECT" "$REGION" "$SERVICE"
printf '\nThis creates or updates BILLABLE Firestore, Cloud Run, and Secret Manager resources.\n'
printf 'The new service will be PUBLIC, with server-enforced limits of\n'
printf '20 NVIDIA analyses/day globally and 5/day per signed anonymous browser.\n'
printf 'Set a separate Nebius provider-side spending limit; quotas are not guarantees.\n'
printf 'If Firestore is absent, a Native-mode database is created in us-west1.\n'
printf 'The Gemini and fixed replay services will not be touched.\n\n'
read -r -p "Type DEPLOY to authorize these cloud changes: " CONFIRM
[[ "$CONFIRM" == "DEPLOY" ]] || die "Cancelled without changes."

printf '\nEnabling required Google Cloud services...\n'
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  firestore.googleapis.com iam.googleapis.com \
  --project="$PROJECT" --quiet >/dev/null

if ! gcloud iam service-accounts describe "$RUNTIME_ACCOUNT" \
  --project="$PROJECT" >/dev/null 2>&1; then
  printf 'Creating a dedicated DataReady runtime identity...\n'
  gcloud iam service-accounts create "$ACCOUNT_NAME" \
    --display-name="DataReady Live runtime" \
    --project="$PROJECT" --quiet >/dev/null
fi

# The default Firestore DB is required for transactional quotas.
if ! gcloud firestore databases describe --database='(default)' \
  --project="$PROJECT" >/dev/null 2>&1; then
  printf 'Creating the Firestore Native-mode default database in %s...\n' "$REGION"
  gcloud firestore databases create \
    --database='(default)' --location="$REGION" \
    --edition=standard --type=firestore-native \
    --project="$PROJECT" --quiet >/dev/null
fi

printf 'Granting transaction access to the DataReady runtime...\n'
retry_iam_update gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$RUNTIME_ACCOUNT" \
  --role="roles/datastore.user" \
  --quiet >/dev/null

ensure_secret() {
  local name="$1"
  if ! gcloud secrets describe "$name" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud secrets create "$name" \
      --replication-policy=automatic --project="$PROJECT" --quiet >/dev/null
  fi
}

has_version() {
  [[ -n "$(gcloud secrets versions list "$1" --project="$PROJECT" \
    --filter='state=ENABLED' --format='value(name)' 2>/dev/null | head -n 1)" ]]
}

ensure_secret "$NEBIUS_SECRET"
if ! has_version "$NEBIUS_SECRET"; then
  printf 'Storing the Nebius credential in Secret Manager (value hidden)...\n'
  .venv/bin/python -c \
    'import sys; from dotenv import dotenv_values; sys.stdout.write(str(dotenv_values(".env")["NEBIUS_API_KEY"]))' |
    gcloud secrets versions add "$NEBIUS_SECRET" \
      --data-file=- --project="$PROJECT" --quiet >/dev/null
fi

ensure_secret "$SIGNING_SECRET"
if ! has_version "$SIGNING_SECRET"; then
  printf 'Generating the independent approval signing key (value hidden)...\n'
  openssl rand -hex 32 | tr -d '\n' |
    gcloud secrets versions add "$SIGNING_SECRET" \
      --data-file=- --project="$PROJECT" --quiet >/dev/null
fi

for name in "$NEBIUS_SECRET" "$SIGNING_SECRET"; do
  retry_iam_update gcloud secrets add-iam-policy-binding "$name" \
    --member="serviceAccount:$RUNTIME_ACCOUNT" \
    --role="roles/secretmanager.secretAccessor" \
    --project="$PROJECT" --quiet >/dev/null
done

printf '\nBuilding and deploying the NEW public dataready-live service...\n'
# Python 3.12 requires the google-22 stack; the default google-24 builder
# does not provide it. Keep runtime updates tied to explicit deployments.
gcloud run deploy "$SERVICE" \
  --project="$PROJECT" --source=. --region="$REGION" \
  --base-image=python312 --no-automatic-updates \
  --service-account="$RUNTIME_ACCOUNT" \
  --allow-unauthenticated \
  --min-instances=0 --max-instances=1 \
  --memory=1Gi --cpu=1 --concurrency=2 --timeout=180 \
  --set-build-env-vars='GOOGLE_PYTHON_VERSION=3.12,GOOGLE_ENTRYPOINT=uvicorn app.live_product:app --host 0.0.0.0 --port 8080' \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=$PROJECT,DATAREADY_PUBLIC_INFERENCE_ENABLED=true,DATAREADY_PUBLIC_DAILY_LIMIT=20,DATAREADY_PUBLIC_SESSION_LIMIT=5" \
  --update-secrets="NEBIUS_API_KEY=$NEBIUS_SECRET:latest,DATAREADY_APPROVAL_SIGNING_KEY=$SIGNING_SECRET:latest" \
  --quiet

URL="$(gcloud run services describe "$SERVICE" \
  --project="$PROJECT" --region="$REGION" \
  --format='value(status.url)')"
[[ "$URL" == https://* ]] || die "No valid Cloud Run HTTPS URL returned."
printf '\nPublic DataReady Live URL: %s\n' "$URL"
printf 'Checking the deployed service health...\n'
curl --fail --silent --show-error --max-time 30 "$URL/health"
printf '\n\nDeployment finished. Test with a harmless sample CSV before sharing the URL.\n'
