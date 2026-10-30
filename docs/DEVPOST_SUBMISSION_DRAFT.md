# DataReady — submission draft

Status: prepared October 29, 2026. Public deployment, video upload, and the final
Devpost submission are not yet verified. Replace pending links before submitting.

## Submission fields

- **Project name:** DataReady
- **Tagline:** AI proposes data repairs. Independent checks verify them. You approve the export.
- **Track:** Best Apps and Agents
- **Source:** https://github.com/atoosabiglari-PM/dataready-autopilot/tree/feature/competing-repair-candidates
- **Working application:** PENDING — use the verified `dataready-live` URL.
- **Public YouTube video:** PENDING — keep the recording under three minutes.
- **License:** Apache-2.0, in the repository's LICENSE file.

## Inspiration

Preparing data for AI requires decisions about what a model may see, what it may
change, and how a person can verify the result. DataReady explores an approach in
which model suggestions are checked against measured evidence before a user
authorizes a separate repaired file.

## What it does

DataReady accepts bounded CSV, flat JSON, and Excel uploads. It checks their
structure and quality, identifies supported findings, and helps locate tables
inside workbooks with introductory notes. For eligible inputs, NVIDIA Nemotron
proposes constrained repair candidates through Nebius Token Factory.

The application checks each candidate against deterministic policy, runs
permitted repairs on temporary copies, and re-audits the results. It compares
measured improvements and regressions, recommends a candidate when supported,
and preserves unresolved findings. The user can authorize a separate CSV export;
export revalidates the source fingerprint, plan, and trial evidence. JSON evidence
records the findings and recommendation. Critical findings stop automatic planning
for human review, and unchanged or ambiguous cases need not produce a repair.

## How it is built

The competition application uses Python 3.12, FastAPI, Pydantic, pandas, openpyxl,
and a browser upload interface. NVIDIA Nemotron
(`nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`) is called at runtime through Nebius Token
Factory's OpenAI-compatible API. The model receives minimized issue evidence
with aliased column names; raw rows are not sent to it. It proposes structured
actions and cannot authorize execution or run generated code.

The core supports exact-duplicate removal, outer-whitespace trimming, and
missing-marker normalization under policy controls. SHA-256 fingerprints bind
the plan, source, trial, and exported result. The Cloud Run deployment script
configures server-side Secret Manager credentials and Firestore transactions
for public inference quotas. Hosted operation remains to be verified before
this draft can describe it as deployed. The earlier Gemini / Google ADK path
remains a separate provider option.

## Verified results

- The complete current automated suite passed: **355 tests**.
- A real local customer-UI run called NVIDIA Nemotron through Nebius Token Factory.
- The sample contained four rows, one exact duplicate, and one missing category.
- The actual run returned one candidate: remove the exact duplicate.
- Its independent trial produced three rows and zero duplicates, with no
  detected audit regressions. The heuristic score rose from 80 to 90.
- The category remained blank; the application did not invent its value.
- After explicit approval, the downloaded CSV exactly matched the trial's
  SHA-256 fingerprint. The evidence reported the original preserved.

These observations establish the sample workflow, not general data correctness
or production readiness. The live model is not required to return a fixed number
of candidates.

## Significant updates during the submission period

DataReady began with a constrained Gemini repair-planning workflow. During the
competition window, the project added the NVIDIA / Nebius integration, multiple
candidate support, isolated candidate trials, regression-aware comparison,
equivalent-output selection, the real customer-upload interface, CSV/JSON/XLSX
import, worksheet/header detection, signed approval and revalidated export, and
public inference quota controls. The final submission should point to the exact
competition branch/commit used in the demonstration.

## Challenges and limitations

AI proposals can be unsupported, non-improving, or ambiguous, so DataReady treats
them as untrusted suggestions and measures the outcome. Workbook layouts can
include notes, configuration cells, and multiple tables, so the user can override
the detected table. Saved formula results can be stale; missing caches block
planning and export until the workbook is recalculated and saved.

The current quality score is heuristic and combines several kinds of findings.
Optional blank fields may be expected in a business workflow. Contact-pattern
matching does not distinguish public business contacts from personal information
and can flag decimal coordinates. These findings remain visible and can stop
planning for review. DataReady does not currently infer business-required fields,
fill missing facts, or guarantee that a dataset is correct or compliant.

## Feedback on the tools used

Nebius Token Factory's OpenAI-compatible API supported integrating the NVIDIA
model through an explicit provider adapter. The recorded local run returned a
supported duplicate-removal proposal that the deterministic engine could test.
Independent validation was essential: accepting structured output alone would
not establish a useful repair or permission to change a file.

Useful future tooling would include more examples of constrained structured
repair output, clear behavior when no candidate is justified, and convenient
ways to compare model latency, cost, and output validity across repeated runs.
This project does not yet have a latency or cost benchmark, and it did not test
Nebius AI Cloud compute; feedback is limited to the Token Factory integration
and NVIDIA model behavior exercised here.

## Recording outline — target 2:30

| Time | Show | Explain |
| --- | --- | --- |
| 0:00–0:20 | DataReady live upload page | The problem: verifiable AI-assisted data repair |
| 0:20–0:45 | Upload a harmless sample and inspect findings | Actual input, duplicate count, unresolved missing value |
| 0:45–1:15 | Run live NVIDIA analysis | Nebius runtime call; minimized evidence; no raw rows |
| 1:15–1:50 | Actual candidate cards and trial evidence | Policy, before/after measurements, remaining findings |
| 1:50–2:15 | Approve and download the CSV and evidence | Separate output; source preservation; verified fingerprint |
| 2:15–2:30 | Brief architecture and limits | Provider-independent core and concrete next improvements |

Record actual behavior. If only one candidate is returned, describe one.
Do not present the fixed replay service as live inference or suggest the hotel
workbook was repaired. Use the verified public app once it is available.

## Final completion checklist

- [ ] Deploy and test the public live app; replace the pending URL above.
- [ ] Verify the submitted source link and setup instructions in a fresh browser.
- [ ] Check judge testing access, provider credits, and quota availability.
- [ ] Record, upload publicly to YouTube, and replace the pending video link.
- [ ] Paste the final description, track, feedback, and update statement into Devpost.
- [ ] Recheck all links and submit before October 30, 2026, 10:00 AM Pacific.
- [ ] Save the submission confirmation; keep judge access available through December 23.

Official requirements: https://nebiusglobalaihackathon.devpost.com/rules
