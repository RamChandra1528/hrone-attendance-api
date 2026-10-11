# DECISIONS.md

I created unique indexes on `employees.emp_code` and `attendance_logs (emp_code, date)`. They protect the two business keys and make duplicate requests safe. The remaining indexes support employee department/join-date lookups, attendance list sort/filter combinations, monthly employee scans, and latest-punch lookup. I considered an email unique index but rejected it because the contract only declares `emp_code` unique.

For simultaneous identical punch-ins, both requests may pass employee lookup, but both insert the same natural key. MongoDB accepts one insert and rejects the other with `DuplicateKeyError`; the API returns one 201 and one or more 409 responses.

The leaderboard uses MongoDB's standard competition rank. If employees tie at the cutoff rank, all tied employees are returned because the filter is `rank <= limit`; names are then ordered by `emp_code`.

Department summary starts from eligible employees, then left-joins their logs. Therefore an employee with no logs still contributes one headcount row.

At 100 times the data, I would add a pre-aggregated daily department collection updated by a background worker, while keeping the source logs as the audit record.
