# Reproducibility report (brief step 10)

Command (regenerates the dataset twice into separate temporary directories with the same seed and compares row
counts and the SHA-256 of **every** output file, including reports and ground truth):

```bash
python run.py repro --profile full
python run.py repro --profile starter
```

What makes the output byte-identical: fixed seed + explicit as-of date; one RNG stream per generation phase seeded by
string (SHA-512, independent of `PYTHONHASHSEED`); rows sorted by primary key; no wall-clock values, hostnames or
absolute paths in any file; fixed float precision; `\n` line endings; sorted JSON keys.

### Profile `full`

* seed `20261022`, as-of `2026-11-13`, runs: 2 (independent output directories)
* row counts identical: **True**
* files compared by SHA-256: **44**
* files with different checksum: **0**
* **reproducible: True**

| table | rows |
|---|---:|
| alert | 4,323 |
| alert_event | 7,911 |
| audit_event | 94,277 |
| beneficial_owner | 1,839 |
| customer | 8,000 |
| customer_decision | 16,101 |
| customer_type | 2 |
| document | 20,000 |
| document_type | 6 |
| kyc_requirement | 21 |
| ownership_link | 2,059 |
| review_case | 3,000 |
| review_case_event | 11,510 |
| risk_rating | 8,352 |
| staff_user | 18 |
| verification | 20,344 |

Sample checksums (identical in both runs):

| file | sha256 |
|---|---|
| `operational/csv/audit_event.csv` | `bb880dcbd2f814b5f6cf22231694a6a0a28ec985ae8cb8c417da261d867fb0fe` |
| `operational/csv/customer.csv` | `b28375ae580e080dc452f3fe5cb7a601c2db03be4cd737e721ddbeea904d8ae1` |
| `operational/csv/document.csv` | `7026731a0627cee566b182e5d7ab95153ce83b838c5a24e197cbfbb3ecbfb9a2` |

### Profile `starter`

* seed `20261022`, as-of `2026-11-13`, runs: 2 (independent output directories)
* row counts identical: **True**
* files compared by SHA-256: **44**
* files with different checksum: **0**
* **reproducible: True**

| table | rows |
|---|---:|
| alert | 220 |
| alert_event | 414 |
| audit_event | 4,753 |
| beneficial_owner | 89 |
| customer | 400 |
| customer_decision | 813 |
| customer_type | 2 |
| document | 1,000 |
| document_type | 6 |
| kyc_requirement | 21 |
| ownership_link | 98 |
| review_case | 150 |
| review_case_event | 599 |
| risk_rating | 415 |
| staff_user | 18 |
| verification | 1,014 |

Sample checksums (identical in both runs):

| file | sha256 |
|---|---|
| `operational/csv/audit_event.csv` | `a7dd4747d30d23dba01a970332e583591c3cc23dfe9888e463819c895785a519` |
| `operational/csv/customer.csv` | `e4058d0d5d3d85f3c39119931eecb8341fa4f5f018c8f43bfd07d4eaf9e8172f` |
| `operational/csv/document.csv` | `f9e231a9e05f646d5c9f65e4df5820461cafe33f1332fb69b66fea806340cb2f` |
