# DataReady — Nebius × NVIDIA live product competition plan

**Status (October 29, 2026, Pacific):** The competition feature branch has
355 passing tests. A real local NVIDIA / Nebius customer-upload run, approved
export, and downloaded CSV fingerprint were verified. Public deployment and
hosted end-to-end verification remain pending.

**Official rules:** https://nebiusglobalaihackathon.devpost.com/rules  
**FAQ:** https://nebiusglobalaihackathon.devpost.com/details/faqs  
**Judging guidance:** https://nebiusglobalaihackathon.devpost.com/updates/46204-here-s-how-judging-works

## 1. Frozen competition commitments

- Product stays **DataReady**; no rebranding.
- Strategy C: a **provider-independent DataReady Core** with **Nebius Token
  Factory + NVIDIA Nemotron as primary competition reasoning provider**.
  Existing Gemini/Google ADK remains a separate alternative.
- Target track: **Best Apps and Agents** (working multi-step application,
  not merely a model API wrapper).
- A **real end-user dataset upload** must drive the app. The fixed synthetic
  showcase is supplemental evidence, **not the competition live-demo URL**.
- AI sees **minimized, aliased audit evidence only**, never raw rows.
- Model suggestions are untrusted; deterministic policy controls permissions.
- Approved deterministic repairs run on **temporary isolated copies**;
  fresh re-audits and regression checks determine whether a candidate improves.
- Source SHA-256 is verified before and after trials.
- Human review precedes the **separate repaired CSV export**. No silent
  production write, no arbitrary model-generated code execution.
- Surface rejected alternatives, remaining issues, verification, model
  identity, and source preservation. No fictional improvements.
- Competition project must show **significant updates** made during the
  submission window, Aug 26 through Oct 30, 2026.

## 2. Live product acceptance tests

The public-facing **DataReady Live** entrypoint is `app.live_product:app`,
separate from the public replay service and existing Gemini service.

1. Open the live app URL in a browser and see an actual file-upload UI.
2. Choose a user-provided **CSV**, flat **JSON**, or **XLSX** workbook.
   The layout detector should automatically distinguish introductory notes
   and configuration rows from a well-supported data table. If uncertain,
   the customer confirms the worksheet and header row; DataReady never silently
   discards ambiguous tables. Structural inference is deterministic and local.
3. Run the real deterministic audit; verify findings and score reflect that
   specific file, not a fixed demo dataset.
4. Without signup, codes, or user-supplied provider keys, start the real
   Nebius Token Factory inference call to NVIDIA Nemotron. The backend
   automatically enforces global and per-browser fair-use limits.
5. See the model-generated proposed actions, policy status, independent trial
   measurements, regression results, and source-fingerprint evidence.
6. If none are useful, show a truthful abstention; if there are critical
   findings, show human review and **do not call the model**.
7. If a unique candidate measurably improves the dataset, show the
   recommendation and remaining unresolved issues.
8. Explicitly approve a separate repaired file. Re-upload the same source
   and validate its signed approval, source SHA-256, permitted plan, and
   independent trial evidence before generating a new CSV.
9. Download the repaired CSV and machine-readable JSON evidence.
10. Verify source bytes remain unchanged; no server-side long-term dataset
    persistence in the competition demo.
11. Verify different datasets produce different measured outputs.
12. Preserve existing Gemini functionality and the already-deployed public
    synthetic reference showcase.

**Not an unrestricted general-purpose data platform:** This competition build
supports tabular `.csv`, flat `.json`, and selectable-worksheet `.xlsx` with explicit header-row selection, up to 10 MB,
100,000 data rows, and 200 columns. Unsupported formats and risky inputs fail
closed. XLSX formulas are not executed or recalculated: saved calculated results can be audited with a clear warning. Missing cached formula results allow an audit of the available values, but block AI planning and repaired-file export until the workbook has been recalculated and saved. Do not upload
confidential customer information or regulated personal data to the public demo.
A complete production product would require persistent identity, per-customer
isolation, compliance controls, scaling, billing, and additional formats.

## 3. Safe local startup

Use the competition feature branch:

```bash
git pull --ff-only
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m pytest -q
./.venv/bin/python -m uvicorn app.live_product:app --host 127.0.0.1 --port 8080
```

Visit http://127.0.0.1:8080/. Click **Load a sample dataset**, or select
any compatible CSV/JSON/XLSX file. **Run safety & quality audit** works
without credentials.

For a real NVIDIA run, **the customer simply clicks Run live NVIDIA analysis**.
No access code is entered or distributed. The existing `NEBIUS_API_KEY`
is read server-side from the local ignored `.env`. An automatically
signed anonymous browser cookie is issued when the homepage opens.

For local development, the process enforces a small, reset-on-restart quota.
This is suitable for trusted local testing only. On Cloud Run, the service
requires a shared atomic daily quota in Firestore; it never falls back
to the process-local counter.

The backend also supports a separate
`DATAREADY_APPROVAL_SIGNING_KEY` (32+ characters). Without one, the
application generates an ephemeral signing key at startup for local testing;
for a stable hosted service, configure the signing key as a secret.

## 4. Cloud deployment — NEW service, not Gemini or reference showcase

Project: `dataready-autopilot-atoosa` (project number `350298872740`)  
Region: `us-west1`.

Keep the existing Cloud Run services **dataready-autopilot** (Gemini) and
**dataready-nvidia-showcase** (public synthetic replay) unchanged.

**Guided deployment:** After all tests pass, run
`bash scripts/deploy_dataready_live.sh` from the local repository root. It
requires authenticated `gcloud`, the ignored local `.env` with `NEBIUS_API_KEY`,
and a `.venv`. It prompts before making chargeable public cloud changes,
creates a distinct service identity, scoped Secret Manager grants, Firestore
quotas, and limits NVIDIA requests to 20/day globally and 5/day per signed
browser by default. Never run it from an untrusted fork. The service URL is
printed and `/health` is checked; verify actual paid inference afterward.

For operators provisioning resources manually, create **dataready-live** as
a separate service using:

```bash
gcloud run deploy dataready-live \
  --project=dataready-autopilot-atoosa \
  --source . \
  --region=us-west1 \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances=1 \
  --memory=1Gi \
  --cpu=1 \
  --concurrency=2 \
  --timeout=180 \
  --set-build-env-vars='GOOGLE_ENTRYPOINT=uvicorn app.live_product:app --host 0.0.0.0 --port 8080'
```

**Do not run this deployment command until the project has all these
server-side prerequisites:**

- **Secret Manager:** `NEBIUS_API_KEY` and
  `DATAREADY_APPROVAL_SIGNING_KEY` (independent 32+ character signing key).
  Grant the Cloud Run service account access to those specific secrets.
  Attach them with `--update-secrets`, after verifying the actual secret names.
  Do not include either secret value in the frontend, source code, or deploy command.
- **Firestore:** a Native-mode default Firestore database in the correct
  Google Cloud project, with the service account authorized for transactional
  document reads and writes (typically `roles/datastore.user`). Verify the
  database exists before enabling paid inference. Creation can be billable and
  database location decisions can be irreversible.
- **Quota enablement:** runtime configuration
  `DATAREADY_PUBLIC_INFERENCE_ENABLED=true` and
  `GOOGLE_CLOUD_PROJECT=dataready-autopilot-atoosa`, after successfully
  testing the Firestore reservations.
- **Automatic quotas:** default cap of **5 live NVIDIA requests per anonymous
  browser session per UTC day**, plus **50 total per UTC day across the entire
  Cloud Run service**. Operators may reduce limits through
  `DATAREADY_PUBLIC_SESSION_LIMIT` and
  `DATAREADY_PUBLIC_DAILY_LIMIT`; the app will reject values exceeding
  its built-in hard caps. Clearing cookies can reset the per-browser limit,
  but cannot evade the shared Firestore global quota.
- **Provider-level controls:** configure Nebius spending monitoring/limits
  separately and Cloud Billing budget alerts. Usage limits reduce exposure,
  but do not guarantee zero cost or eliminate all abuse.

Without an active Firestore quota, correct service-account permissions,
and explicit operator enablement, `POST /api/analyze` fails closed (503).
No customer or judge access code is requested. `/api/audit` remains
available without paid NVIDIA calls.

Cloud Run allows cost-bearing user-provided uploads and Nebius inference.
The service's max-instance and concurrency settings do **not** enforce a
financial spending cap or replace provider-side budget controls.

## 5. Formal hackathon submission requirements

**Deadline:** Fri **Oct 30, 2026 at 10:00 AM Pacific**.

**Judging:** Dec 9–23, 2026, per the official rules checked October 29. Keep
the free-to-test working application available to judges until Dec 23. Our public live application requires
no login or judge credentials; supply the direct upload-and-analysis URL.

**Track:** Best Apps and Agents.

**Judging dimensions (equal weight):**

1. Technological implementation: real Nebius runtime model calls, meaningful
   Nemotron reasoning, reliable independent engineering.
2. Design: a coherent app workflow end-to-end; upload → analysis → review → export.
3. Potential impact: business value from safe, traceable data readiness.
4. Quality of idea: novel provider-independent governance and evidence checks
   instead of unverified model-written data changes.

**Submission:** Devpost project description, **working live-app URL**,
public GitHub repository, clear README and Apache-2.0 LICENSE (already
present; verify visible on GitHub), <3-minute public **YouTube demo video**
showing the project working on device, used tools, track selection, and
specific **feedback** on Nebius/NVIDIA tooling and models.

Document substantial additions during the competition: candidate-generation
abstraction, NVIDIA/Nebius integration, independent candidate trials,
policy checks, regression detection, selection, safeguarded API,
user-provided-file frontend, export approval, and security tests.

### Suggested 2:30 demo video outline

- 0:00–0:20: Problem: enterprises cannot blindly trust AI-generated data repairs.
- 0:20–0:45: Open actual DataReady Live, upload a *new* synthetic/clean CSV,
  show real audit findings and source fingerprint.
- 0:45–1:15: Start real **NVIDIA Nemotron through Nebius Token Factory**
  inference. Explain minimized inputs/aliased columns, no raw rows.
- 1:15–1:50: Show the actual returned candidates, isolated trials, and independent
  audit evidence. If this run returns one proposal, say one; show rejected
  alternatives only when the actual run produces them.
- 1:50–2:15: Approve the unique improving candidate; download the *separate*
  repaired CSV; show original untouched and remaining findings.
- 2:15–2:30: The enterprise governance insight: model proposes, evidence
  proves, humans authorize. Mention provider-independent core and practical
  applications.

Record real on-screen behavior; do not splice synthetic replay evidence
into the video as if it were fresh inference.

## 6. Outstanding before declaring submission ready

- [x] Core audit, policy, separate-file executor, regression checks and model
      candidate generation built and successfully exercised locally earlier.
- [x] Independent multi-candidate NVIDIA live run succeeded locally.
- [x] Current complete suite: **355 passed**, independently run here and confirmed
      on the developer's Mac. Existing Ruff findings remain documented.
- [x] Real-customer upload web flow, multiple formats, model-gated API and
      explicit approved export implemented in the **new feature branch**.
- [x] **Run full local tests after this addition** and repair the two incorrect
      test expectations; code checkpoint `be587579`.
- [x] Verify a real upload, real Nebius/Nemotron call, and real CSV export
      in the new customer UI. Sample result: 4 to 3 rows, 1 to 0 duplicates,
      score 80 to 90, one missing category preserved. Downloaded output SHA-256
      exactly matched the independent trial's fingerprint.
- [ ] Securely configure Cloud Run runtime secrets, Firestore atomic quota
      transactions, anonymous guest sessions, and spending controls.
- [ ] Deploy and verify separate **dataready-live** URL using real uploads.
- [x] Update the competition branch README to prominently describe the live
      customer product, setup, observed verification, and known limitations.
- [ ] Check the submitted repository link opens the competition implementation
      and its README; do not accidentally submit only the legacy Gemini version.
- [ ] Capture a real working demo video (<3 minutes), publish to YouTube.
- [ ] Complete Devpost entry, competition feedback, and significance statement
      **before Oct 30 at 10:00 AM Pacific**.
- [ ] Verify judges can complete testing despite the public demo's quotas;
      provide clear test-build access/instructions and resolve access failures.
- [ ] Maintain application and credentials for judges through Dec 23, 2026.

## 7. Submission scope and remaining limitations

Freeze feature development for the deadline. The hotel workbook showed useful
table detection, but public contact fields and a decimal Longitude value triggered
critical contact-pattern findings. Blank optional notes/dates also count against
the heuristic score. The score is not a business-usefulness measure. These are
documented limitations; they are not a reason to disable the review checks.

Use a harmless, accurately labeled sample for the live repair demonstration.
Do not claim the hotel workbook was repaired or that every upload is supported.
Do not commit customer workbook rows or credentials into the submission repository.
