# Testing guide - step by step

Everything uses only Python 3.9+. On Mac/Linux write `python3` instead of `python`. Run all commands from the project folder.

## A. Automated checks (2 minutes)

| # | Command | Expected |
|---|---|---|
| 1 | `python -m unittest discover -s tests -v` | `Ran 44 tests ... OK` (acceptance, boundaries, validator mutation, HTTP app) |
| 2 | `python run.py all --profile full` | `PASS`, 200/200 injected edge cases detected, 0 integrity/reconciliation defects |
| 3 | `python run.py repro --profile full` | `"reproducible": true` (two regenerations are byte-identical) |
| 4 | `python run.py validate --data data/full` | `"passed": true` |

## B. Manual test of the application (10 minutes)

Start: `python run.py serve --reset`, open http://127.0.0.1:8000/ (top bar: **Acting as** = an OPS analyst, e.g. `USR-OPS-001`).

1. **Onboard.** Customers > + New customer: name `Asha Test`, INDIVIDUAL, LOW > Create. Status is **PENDING**, completeness 0%.
2. **Evidence.** Submit `ID_PROOF` (number `A1`, name `Asha Test`, issue date any date up to the business date) then `ADDRESS_PROOF` (number `A2`). Click **Verify** on each. After the second, status becomes **APPROVED**; the Decisions table shows the appended history.
3. **Name mismatch.** New customer `Ravi Real`, submit ID_PROOF with name `Someone Else` > Verify > **REJECTED**; the customer stays PENDING.
4. **Duplicate.** For another customer, submit ID_PROOF number `A1` > error `DUPLICATE_DOCUMENT` (409).
5. **Ownership.** New BUSINESS customer, add owner 60% then 50% > second is refused (`OWNERSHIP_OVER_100`); add 40% > accepted.
6. **Reviews.** On an APPROVED customer click *Schedule periodic review*. Reviews tab: move SCHEDULED > OPEN > IN_PROGRESS. Switch **Acting as** to a reviewer (`USR-CMP-001`) and try COMPLETED with an empty resolution > refused (`RESOLUTION_REQUIRED`); enter `NO_CHANGE` > completed. An illegal jump (e.g. OPEN > COMPLETED) is refused.
7. **Roles.** As the reviewer try + New customer > 403. As an analyst click *Run daily batch* > 403. Switch to the admin (`USR-ADM-001`) for batch actions.
8. **Expiry alerts.** As admin set the business date to e.g. `2026-12-20` > Set date > **Run daily batch**. Document statuses move to EXPIRED, expiry alerts (90/60/30/7 days) and overdue-review alerts appear (Alerts tab). Run the batch again: 0 new items (idempotent). The date can only move forward.
9. **Audit.** Audit trail tab, filter by a customer id: every step above is recorded.
10. **Exceptions.** Exceptions tab runs the validator on live data. Expect only BUSINESS_RULE findings (the 15 seeded edge cases). INTEGRITY and RECONCILIATION must be absent.

## C. Generate reports after your changes

Stop the server (Ctrl+C) or leave it running, then:

```
python run.py report                       # snapshot of kyc_app.db -> reports_live/
python run.py report --db kyc_full.db --data data/full --out reports_full_live
```

Output folder:

* `validation_summary.md` - counts, findings by category/rule, **RESULT: PASS/FAIL** (exit code 0 / 1)
* `validation_findings.csv`, `validation_report.json` - every finding with rule, entity and customer id
* `csv/<table>.csv` - every table as it is now, including your new customers, documents, decisions, alerts and audit events

Open the Markdown in any editor and the CSVs in Excel.

## D. Regenerate everything for a new change

| Situation | Do this |
|---|---|
| Changed a rule, threshold or volume in `config/*.json` or code | `python run.py all --profile full` (rebuilds data, ground truth, reports, checksums), then `python -m unittest discover -s tests`, then `python run.py repro --profile full` |
| Want the app on the rebuilt data | `python run.py serve --reset` (add `--data data/full --db kyc_full.db` for the full set) |
| Want to keep what you entered in the app | Do **not** use `--reset`; run `python run.py report` first to save a snapshot |
| Different seed / size | `python run.py all --profile starter --seed 123 --customers 2000 --businesses 250 --documents 5000 --reviews 750` |

Note: `all` regenerates synthetic data from the seed and never reads the application database, so app changes are not part of `data/`. They live in `kyc_app.db` and in the `report` snapshot.

## E. If something fails

* Port in use: `python run.py serve --port 8001`.
* `FAIL` in a report: open `validation_findings.csv`, filter category `INTEGRITY` or `RECONCILIATION`; each row names the rule and entity.
* Start clean: delete `kyc_app.db` or use `--reset`.
