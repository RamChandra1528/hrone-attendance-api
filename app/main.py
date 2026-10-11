"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# `load_dotenv` does not replace values injected by the grader or deployment.
load_dotenv(override=False)

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EpochMillis = Annotated[StrictInt, Field(ge=100000000000, le=4102444800000)]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def validate_calendar_date(value: str) -> str:
    """Accept only a real calendar date written exactly as YYYY-MM-DD."""
    if not DATE_PATTERN.fullmatch(value):
        raise ValueError("must be a YYYY-MM-DD date")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("must be a real calendar date") from exc
    return value


def epoch_millis(value: datetime) -> int:
    """Serialize a MongoDB UTC datetime at the API boundary."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def instant_from_epoch_millis(value: Optional[int]) -> datetime:
    """Convert an API instant to UTC and truncate it to the stored second."""
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    seconds, _milliseconds = divmod(value, 1000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def as_utc(value: datetime) -> datetime:
    """Treat legacy naive BSON datetimes as UTC before performing calculations."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """Return the IST shift-start date required by R1, including overnight shifts."""
    local = as_utc(punch_in).astimezone(IST)
    end_hour, end_minute = map(int, shift_end.split(":"))
    is_before_overnight_end = (local.hour, local.minute, local.second) < (
        end_hour,
        end_minute,
        0,
    )
    if shift_end <= shift_start and is_before_overnight_end:
        return (local - timedelta(days=1)).date().isoformat()
    return local.date().isoformat()


def shift_time_on(attendance_date: str, shift_time: str) -> datetime:
    """Build an IST shift instant from the stored date and an HH:MM value."""
    hour, minute = map(int, shift_time.split(":"))
    return datetime.fromisoformat(attendance_date).replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
        tzinfo=IST,
    )


def employee_response(employee: dict) -> dict:
    """Remove MongoDB internals and convert the stored creation instant."""
    result = {key: value for key, value in employee.items() if key != "_id"}
    result["created_at"] = epoch_millis(result["created_at"])
    return result


def attendance_response(record: dict) -> dict:
    """Convert BSON dates and legacy optional fields to the attendance API shape."""
    result = {key: value for key, value in record.items() if key != "_id"}
    for field in ("punch_in", "punch_out"):
        if result.get(field) is not None:
            result[field] = epoch_millis(result[field])
        else:
            result[field] = None
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)

    history = []
    for entry in result.get("history", []):
        serialized_entry = dict(entry)
        if serialized_entry.get("at") is not None:
            serialized_entry["at"] = epoch_millis(serialized_entry["at"])
        changes = {}
        for field, change in serialized_entry.get("changes", {}).items():
            serialized_change = dict(change)
            if field in {"punch_in", "punch_out"}:
                for direction in ("from", "to"):
                    if serialized_change.get(direction) is not None:
                        serialized_change[direction] = epoch_millis(
                            serialized_change[direction]
                        )
            changes[field] = serialized_change
        serialized_entry["changes"] = changes
        history.append(serialized_entry)
    result["history"] = history
    return result


def create_indexes() -> None:
    """Create the indexes required by API lookups; repeated calls are safe."""
    # The unique natural keys are also the concurrency guarantees for creates.
    db.employees.create_index(
        [("emp_code", ASCENDING)], name="uniq_employees_emp_code", unique=True
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="employees_department_emp_code",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="employees_department_joined_on",
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        name="uniq_attendance_emp_code_date",
        unique=True,
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_emp_code",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING)],
        name="attendance_emp_code_date",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_status_date_emp_code",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_out", ASCENDING), ("punch_in", DESCENDING)],
        name="attendance_open_record_lookup",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="attendance_emp_code_punch_in",
    )


@app.on_event("startup")
def ensure_indexes_at_startup() -> None:
    """Bootstrap indexes without preventing `/health` from reporting DB failure."""
    try:
        create_indexes()
    except PyMongoError:
        # If MongoDB is temporarily unavailable, health will return 503 instead of
        # making the ASGI process fail during import/startup.
        pass


def compute_late_minutes(
    punch_in: datetime, shift_start: str, attendance_date: str
) -> int:
    """Apply R2's strict ten-minute grace period using the shift-start instant."""
    shift_start_at = shift_time_on(attendance_date, shift_start)
    elapsed_seconds = int(
        (as_utc(punch_in).astimezone(IST) - shift_start_at).total_seconds()
    )
    if elapsed_seconds <= 10 * 60:
        return 0
    return elapsed_seconds // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """Apply R4's two-decimal, half-up rounding without float rounding errors."""
    elapsed_seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    hours = Decimal(elapsed_seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime, shift_start: str, shift_end: str, attendance_date: str
) -> int:
    """Apply R3, moving an overnight shift end to the next IST calendar day."""
    shift_end_at = shift_time_on(attendance_date, shift_end)
    if shift_end <= shift_start:
        shift_end_at += timedelta(days=1)
    elapsed_seconds = int(
        (as_utc(punch_out).astimezone(IST) - shift_end_at).total_seconds()
    )
    elapsed_minutes = max(0, elapsed_seconds // 60)
    return elapsed_minutes if elapsed_minutes >= 30 else 0


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    shift_end: str = Field(
        default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_must_be_a_date(cls, value: str) -> str:
        return validate_calendar_date(value)

    @model_validator(mode="after")
    def shift_times_must_differ(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return employee_response(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q)
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    items = [employee_response(item) for item in items]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = instant_from_epoch_millis(body.punched_at)
    attendance_date = attendance_date_for(
        punched_at, emp["shift_start"], emp["shift_end"]
    )
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            punched_at, emp["shift_start"], attendance_date
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409, detail="already punched in for this date"
        ) from exc
    return attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = instant_from_epoch_millis(body.punched_at)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punched_at},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="attendance is already punched out")

    punch_in_at = as_utc(record["punch_in"])
    elapsed_seconds = int((punched_at - punch_in_at).total_seconds())
    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be after punch_in and within 24 hours",
        )

    work_hours = compute_work_hours(punch_in_at, punched_at)
    update_fields = {
        "punch_out": punched_at,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(
            punched_at, emp["shift_start"], emp["shift_end"], record["date"]
        ),
        "half_day": work_hours < 4.50,
    }
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": update_fields},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance is already punched out")
    return attendance_response(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    docs = list(db.attendance_logs.find(q))
    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
