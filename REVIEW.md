# REVIEW.md

| # | Where | What was wrong | How it appears | Fix |
|---|---|---|---|---|
| 1 | `health` | Always returned 200 without checking MongoDB. | Stop MongoDB; readiness still looked healthy. | Ping MongoDB and return 503 on failure. |
| 2 | Startup | No required indexes were created. | Duplicates and slow collection scans. | Create idempotent unique and query indexes at startup. |
| 3 | `create_employee` | Check-then-insert was a race. | Parallel duplicate creates could both succeed. | Use the unique index and map duplicate-key errors to 409. |
| 4 | Employee model | Contract validation was missing. | Bad employee code, email, shift or date was accepted. | Added Pydantic constraints and calendar/shift validation. |
| 5 | Employee response | Returned BSON/ISO datetime instead of epoch milliseconds. | Contract response mismatch. | Serialize `created_at` to epoch milliseconds. |
| 6 | `list_employees` | Used the wrong skip offset, unfiltered total, and no sort. | Page one skipped records; filtered total was wrong. | Query-filtered count, `(page - 1) * page_size`, indexed sort. |
| 7 | `punch_in` | Unknown employee caused a server error. | `None["shift_start"]` raises 500. | Return 404 before calculations. |
| 8 | `punch_in` | Timestamp used local server time and no strict validation. | Seconds/floats and non-IST dates behaved incorrectly. | Enforce epoch milliseconds, UTC storage, second truncation, IST conversion. |
| 9 | Punch-in duplicate check | Read-before-insert was not concurrent-safe. | Parallel punch-ins could create duplicates. | Unique natural-key index plus duplicate-key 409. |
| 10 | Late/work/overtime helpers | Ignored overnight dates, strict grace, 30-minute threshold, and half-up rounding. | Overnight and threshold cases returned wrong values. | Rebuilt calculations around IST shift instants and `Decimal` half-up rounding. |
| 11 | `list_attendance` | Loaded every document into Python, sorted there, and exposed `id`. | Slow at scale and invalid response field. | MongoDB sort/count/pagination and response serialization. |
| 12 | Starter routes | Punch-out, corrections, analytics, and explain routes were absent. | OpenAPI paths returned 404. | Implemented all remaining contract endpoints. |
| 13 | Manual corrections | No audit or concurrency protection existed. | Corrections could silently overwrite each other. | Recompute derived values, append history, optimistic conditional update. |

I kept the environment-variable configuration approach: `load_dotenv(override=False)` correctly allows real deployment variables to win over local `.env` values.

Also note anything you looked at and decided was **not** a defect, and why.
