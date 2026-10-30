# Human review and coordinate detection

The review update is tested locally and must be redeployed to `dataready-live`
before the new controls appear on the public site. The earlier public sample
verification remains evidence for the prior deployed revision.

## Workflow

1. Upload a file, confirm the selected worksheet/header if needed, and run the audit.
2. Use **Review findings and recheck** to make individual declarations:
   - Confirm that an explicitly named public/business phone or email column
     contains publicly listed business contacts.
   - Declare a missing-value column optional for the intended use. Its blanks stay blank.
3. Enter a reviewer reference and reason, confirm the declarations, and click
   **Apply review & recheck**. No file values are edited during review.
4. Inspect the remaining findings and review record. Other critical findings
   remain blocking and require a corrected source file followed by a fresh audit.
5. If noncritical findings remain, **Continue to live NVIDIA analysis** is available.
   A measurable, verified repair still requires separate approval before export.
   If no findings remain under the declared context, no AI repair is needed.
6. Download the evidence JSON to retain the decisions, accepted findings, remaining
   findings, and fingerprint. The downloaded evidence excludes authorization tokens.

The reviewer reference is self-reported; this anonymous prototype does not verify
identity or independently prove that a contact is public. Review is a recorded
business assertion, not a certification. There is no blanket “ignore critical
findings” switch. Sensitive patterns incompatible with the declared contact type,
prompt-injection findings, structural failures, and uncached Excel formulas cannot
be cleared by these declarations.

## Coordinate fix and score interpretation

Phone detection no longer flags valid whole numeric values in recognized
latitude/longitude columns. Values must be finite and within the coordinate's
range. A coordinate column name alone cannot hide a phone, email, SSN pattern,
or invalid value. Invalid coordinates receive their own finding.

The UI calls the number a **heuristic check score**, not a readiness percentage.
After review it measures remaining findings under the declared context. Accepted
findings stay in the evidence; a score change from accepting context is not a
data repair or a measured improvement by NVIDIA.

## Integrity and verification

Review receipts are signed, expire after 15 minutes, and bind the original upload,
normalized table, worksheet, and header row. Changing the file or table requires
a new review. Repair approvals also bind the exact review context. Source audit,
independent trials, regression checks, and export use the same context. Export
must reproduce the fresh trial's fingerprint.

Local validation on October 29, 2026:

- **393 automated tests passed**, including 38 new review/coordinate cases.
- Changed Python files pass Ruff. Unrelated legacy lint findings remain.
- DOM integration exercised the visible review controls, consent, recheck,
  continuation, separate export approval, token-free evidence, reset, and
  invalidation after edits. Its model response was a test fixture, not live inference.
- The actual hotel workbook was checked locally. Both tables no longer report
  the Longitude phone-pattern false positive. Test declarations completed review
  without changing source bytes. These test declarations need the owner's
  confirmation in the public UI; they are not an owner approval record.
- A rendered-browser check and hosted verification of this update remain pending.

## Hotel recording setup

Select **Hotel directory**, header row **8**, for the main **86-row, 24-column**
table. **Boundary and exclusions** is a different **23-row, 12-column** table.
The screenshot of the earlier Longitude block came from that second table.

For the hotel directory, the available declarations cover **Public phone**,
**Public email**, **Last contact**, **Next follow-up**, **Team notes**, and
**Listing notes**. Select only declarations the owner confirms are appropriate.
If all six are confirmed, the local check found no remaining audit findings under
that context. The file still contains blanks and contact details, no rows have
been repaired, and NVIDIA has not been called. Demonstrate that outcome accurately.
The built-in sample remains available for the live NVIDIA repair/export workflow.
