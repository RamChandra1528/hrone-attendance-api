# Employee Attendance and Analytics API

FastAPI and MongoDB implementation of the HROne attendance assignment. The API contract is in `openapi.yaml`; MongoDB document shapes are in `DATA_MODEL.md`.

## Run locally

MongoDB 6.0+ must be running. Python 3.11+ is required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python sample_seed.py
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` can be set as real environment variables; they take precedence over `.env`. The supplied seed command deletes and reloads only `employees` and `attendance_logs`, so use it only against a disposable local database.

OpenAPI documentation is available while running at `http://127.0.0.1:8000/docs`.

## Implemented behavior

- Readiness ping and idempotent MongoDB index creation at startup.
- Employee creation/listing with validation, pagination, sorting, and duplicate-safe writes.
- Punch-in, punch-out, IST/overnight shifts, half-up work-hour rounding, and concurrent request protection.
- Attendance listing and audited manual corrections.
- Monthly employee, department, leaderboard, and daily-trend analytics implemented with MongoDB aggregation pipelines.
- `GET /admin/explain/{endpoint}` for execution-statistics explain output of each main list/analytics query.

The sample files demonstrate document shape only. The application is designed for the larger grader dataset and creates its own indexes at startup. Never commit `.env` or credentials.
