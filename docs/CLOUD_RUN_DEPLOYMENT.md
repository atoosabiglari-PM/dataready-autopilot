# DataReady Autopilot — Google Cloud Deployment

## Live Service

DataReady Autopilot is deployed on Google Cloud Run.

Service:

```text
dataready-autopilot
```

Region:

```text
us-west1
```

Live URL:

```text
https://dataready-autopilot-350298872740.us-west1.run.app
```

Health endpoint:

```text
GET /health
```

Governed Gemini demo endpoint:

```text
POST /demo/repair
```

---

## Google Cloud Architecture

DataReady Autopilot uses:

- Google Cloud Run for the live application backend
- Google Cloud Build for source builds
- Artifact Registry for the Cloud Run container image
- Google Secret Manager for the Gemini API key
- Gemini Developer API for constrained repair planning
- Google Agent Development Kit (ADK) for the Gemini agent

The Gemini API key is not stored in source code or committed to GitHub.

It is stored in Secret Manager and injected into Cloud Run as:

```text
GOOGLE_API_KEY
```

The application also uses:

```text
GOOGLE_GENAI_USE_VERTEXAI=FALSE
```

for Gemini Developer API mode.

---

## Trust Boundary

The deployed Cloud Run service preserves the same governance boundary as the local application:

```text
CSV
 ↓
Deterministic Audit
 ↓
Minimized Evidence
 ↓
Gemini Planner
 ↓
Structured RepairPlan
 ↓
Deterministic Policy Authorization
 ↓
Deterministic Repair Executor
 ↓
Re-Audit
 ↓
Before/After Comparison
 ↓
SHA-256 Lineage
 ↓
Machine-Readable Evidence
```

Gemini proposes repairs.

Gemini does not directly edit the CSV and does not authorize its own recommendations.

---

## Live Governed Demo

The Cloud Run endpoint:

```text
POST /demo/repair
```

runs the bundled safe-repair demonstration dataset through the real governed workflow.

The expected successful result includes:

```text
status: REPAIRED
platform: Google Cloud Run
initial_readiness: QUARANTINED
policy_status: APPROVED
execution_authorized: true
post_repair_readiness: READY
```

The current safe-repair demonstration uses:

```text
REMOVE_EXACT_DUPLICATES
```

and produces deterministic evidence including:

- before/after quality scores
- resolved finding codes
- source SHA-256
- output SHA-256
- source preservation status
- executed repair actions
- machine-readable evidence

---

## Public API Verification

Health:

```powershell
python -c "import requests; u='https://dataready-autopilot-350298872740.us-west1.run.app/health'; r=requests.get(u, timeout=30); print(r.status_code, r.json())"
```

Expected:

```text
200 {'status': 'healthy'}
```

Governed Gemini workflow:

```powershell
python -c "import requests; u='https://dataready-autopilot-350298872740.us-west1.run.app/demo/repair'; r=requests.post(u, timeout=120); d=r.json(); print('HTTP:', r.status_code); print('STATUS:', d.get('status')); print('PLATFORM:', d.get('platform')); print('POLICY:', d.get('policy_status')); print('AUTHORIZED:', d.get('execution_authorized')); print('AFTER:', d.get('post_repair_readiness')); print('SCORE:', d.get('initial_quality_score'), '->', d.get('post_repair_quality_score')); print('ACTIONS:', d.get('gemini_repair_actions')); print('SOURCE PRESERVED:', (d.get('lineage_evidence') or {}).get('source_preserved'))"
```

A successful response should show:

```text
HTTP: 200
STATUS: REPAIRED
PLATFORM: Google Cloud Run
POLICY: APPROVED
AUTHORIZED: True
AFTER: READY
SCORE: 90 -> 100
SOURCE PRESERVED: True
```

---

## Deploy From Source

Required Google Cloud APIs:

```text
run.googleapis.com
cloudbuild.googleapis.com
artifactregistry.googleapis.com
secretmanager.googleapis.com
```

Deploy from the repository root:

```powershell
gcloud run deploy dataready-autopilot --source . --region us-west1 --allow-unauthenticated --set-build-env-vars "GOOGLE_ENTRYPOINT=uvicorn app.web:app --host 0.0.0.0 --port 8080"
```

The Python buildpack launches:

```text
uvicorn app.web:app --host 0.0.0.0 --port 8080
```

---

## Secret Manager

The Gemini API key is stored in Google Secret Manager rather than in the repository.

Cloud Run receives the secret as:

```text
GOOGLE_API_KEY
```

The Cloud Run runtime service account requires:

```text
roles/secretmanager.secretAccessor
```

The service also uses:

```text
GOOGLE_GENAI_USE_VERTEXAI=FALSE
```

The secret value must never be printed, committed, or placed directly inside deployment documentation.

---

## Cloud Run Secret Configuration

The Gemini API key secret is attached to the Cloud Run service as an environment variable.

Example configuration:

```powershell
gcloud run services update dataready-autopilot --region us-west1 --update-secrets GOOGLE_API_KEY=dataready-gemini-api-key:latest --set-env-vars GOOGLE_GENAI_USE_VERTEXAI=FALSE
```

The secret itself is never embedded in the command.

---

## Deployment Verification

The deployed application exposes:

```text
GET /
GET /health
POST /demo/repair
```

The root endpoint confirms that the backend is hosted on Google Cloud Run.

The health endpoint verifies that the service is available.

The `/demo/repair` endpoint executes the real governed Gemini workflow.

---

## Governed Cloud Workflow

The public request path is:

```text
Public Request
     ↓
Google Cloud Run
     ↓
Deterministic CSV Audit
     ↓
Sanitized Evidence
     ↓
Google ADK
     ↓
Gemini Repair Planner
     ↓
Structured RepairPlan
     ↓
Deterministic Policy Authorization
     ↓
Deterministic Repair Executor
     ↓
Separate Output Copy
     ↓
Independent Re-Audit
     ↓
Before/After Comparison
     ↓
SHA-256 Lineage Verification
     ↓
Machine-Readable Evidence
     ↓
Cloud Run Response
```

---

## What Gemini Does

Gemini is used as a constrained reasoning component.

Gemini receives minimized audit evidence and proposes a structured repair plan.

Gemini does not:

- directly modify the original CSV
- authorize its own recommendation
- bypass deterministic policy
- overwrite the source file
- disable fingerprint validation
- execute unsupported repair actions

---

## What Deterministic Controls Do

Deterministic code is responsible for:

- preflight safety checks
- audit findings
- source fingerprinting
- policy authorization
- repair execution
- source preservation
- re-auditing
- before/after measurement
- cryptographic lineage
- machine-readable evidence generation

This preserves the core trust boundary:

> Gemini reasons. Deterministic controls govern, execute, verify, and prove.

---

## Live Safe-Repair Demonstration

The deployed demo uses:

```text
demo_data/01_safe_repair.csv
```

The initial deterministic audit identifies:

```text
Status: QUARANTINED
Quality score: 90
Finding: DUPLICATE_ROWS
```

Gemini can propose:

```text
REMOVE_EXACT_DUPLICATES
```

The deterministic policy then decides whether that action is authorized.

When approved, the executor repairs only a separate output copy.

The resulting dataset is independently re-audited.

Expected result:

```text
Before readiness: QUARANTINED
After readiness: READY

Before quality score: 90
After quality score: 100

Duplicate rows before: 1
Duplicate rows after: 0

Source preserved: True
```

---

## Cryptographic Evidence

A successful Cloud Run execution produces evidence linking:

```text
Source CSV
Source SHA-256
     ↓
Original Audit
     ↓
Gemini RepairPlan
     ↓
Policy Decision
     ↓
Deterministic Repair
     ↓
Repaired Output
Output SHA-256
     ↓
Post-Repair Audit
```

The source SHA-256 and output SHA-256 are different because the repaired dataset is a new artifact.

The source file remains unchanged.

---

## Machine-Readable Evidence

The workflow also builds structured evidence containing:

```text
original audit
repair plan
policy decision
post-repair audit
readiness comparison
source fingerprint
output fingerprint
executed actions
source preservation evidence
```

The evidence layer provides a foundation for enterprise AI governance and auditability.

---

## Competition Evidence

The deployed service demonstrates that DataReady Autopilot is not merely a local Python prototype.

The real governed Gemini workflow executes through Google Cloud infrastructure:

```text
User / Judge
     ↓
Public Cloud Run Endpoint
     ↓
DataReady Autopilot
     ↓
Google ADK + Gemini
     ↓
Deterministic Governance
     ↓
Verified Repair Outcome
```

The project combines:

- Gemini
- Google ADK
- Google Cloud Run
- Google Cloud Build
- Artifact Registry
- Google Secret Manager
- deterministic data safety controls
- cryptographic SHA-256 lineage
- machine-readable governance evidence

---

## Core Competition Principle

DataReady Autopilot does not make Gemini the security layer.

Instead:

```text
Gemini reasons.
Schemas constrain.
Policy authorizes.
Deterministic code executes.
Independent auditing verifies.
Cryptographic lineage proves.
```

This is the central architecture behind DataReady Autopilot.