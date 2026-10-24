# DataReady — Nebius × NVIDIA live product competition plan

**Status:** Live customer-upload product code created on the competition feature
branch. Do not claim deployment or real-cloud verification until completed.

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
2. Choose a user-provided **CSV**, flat **JSON**, or **XLSX** worksheet.
3. Run the real deterministic audit; verify findings and score reflect that
   specific file, not a fixed demo dataset.
4. With access credentials from the judging instructions, start the real
   Nebius Token Factory inference call to NVIDIA Nemotron.
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
supports tabular `.csv`, flat `.json`, and first-sheet `.xlsx`, up to 10 MB,
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

For a real NVIDIA run, set **DATAREADY_DEMO_ACCESS_TOKEN** to a long
independent demo code (at least 32 characters); keep it out of Git, the
application HTML, screenshots, and command history. `NEBIUS_API_KEY`
continues to be read server-side from the local ignored `.env`.

Do not confuse the guest competition access code with the actual Nebius key.

The backend also supports a separate
`DATAREADY_APPROVAL_SIGNING_KEY` (32+ characters). Without one, the
application generates an ephemeral signing key at startup for local testing;
for a stable hosted service, configure the signing key as a secret.

## 4. Cloud deployment — NEW service, not Gemini or reference showcase

Project: `dataready-autopilot-atoosa` (project number `350298872740`)  
Region: `us-west1`.

Keep the existing Cloud Run services **dataready-autopilot** (Gemini) and
**dataready-nvidia-showcase** (public synthetic replay) unchanged.
Create **dataready-live** as a separate service using:

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

**Do not run this deployment command until the project has a secure
configuration for these runtime secret variables:**
`NEBIUS_API_KEY`, `DATAREADY_DEMO_ACCESS_TOKEN`, and
`DATAREADY_APPROVAL_SIGNING_KEY`.

Configure them through **Google Secret Manager** (not in Git or cloud build
variables) and grant the runtime service account only secret-access rights.
Attach them to the new service with `--update-secrets` after verifying
actual secret names and runtime service-account permissions.

A public site without a secret can still show the upload audit, but
`POST /api/analyze` fails closed (HTTP 503). A guest code supplied to judges
allows billable model calls. Maintain Nebius account spending controls,
monitor Cloud Run charges, and prevent credential disclosure. Instance limits
do **not** enforce a financial budget.

Cloud Run allows cost-bearing user-supplied uploads and paid inference in
the authenticated workflow. Do not represent it as cost-free hosting.

## 5. Formal hackathon submission requirements

**Deadline:** Fri **Oct 30, 2026 at 10:00 AM Pacific**.

**Judging:** Dec 1–15, 2026. Keep the free-to-test working application
available to judges until Dec 15 and include working judge credentials
in Devpost testing instructions if using a restricted application.

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
- 1:15–1:50: Show multiple candidates, isolated trials, rejected non-improving
  alternatives, and independent audit evidence.
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
- [x] Previous app showed 290 passing tests.
- [x] Real-customer upload web flow, multiple formats, model-gated API and
      explicit approved export implemented in the **new feature branch**.
- [ ] **Run full local tests after this addition** and repair any regressions.
- [ ] Verify a real upload, real Nebius/Nemotron call, and real CSV export
      in the new customer UI (not a mock).
- [ ] Securely configure Cloud Run runtime secrets and spending controls.
- [ ] Deploy and verify separate **dataready-live** URL using real uploads.
- [ ] Update the main README to prominently describe the live customer product
      and how to reproduce the full demonstration.
- [ ] Capture a real working demo video (<3 minutes), publish to YouTube.
- [ ] Complete Devpost entry, competition feedback, and significance statement
      **before Oct 30 at 10:00 AM Pacific**.
- [ ] Maintain application and credentials for judges through Dec 15, 2026.
