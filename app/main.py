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
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
EpochMillis = Annotated[StrictInt, Field(ge=100000000000, le=4102444800000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]

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
    db.employees.create_index(
        [("joined_on", ASCENDING)], name="employees_joined_on"
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


def require_date(value: str, parameter: str) -> str:
    """Validate a query/path calendar date and report it as a FastAPI 422."""
    try:
        return validate_calendar_date(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail=f"{parameter} must be a YYYY-MM-DD date"
        ) from exc


def require_month(value: str) -> str:
    """Validate the contract's YYYY-MM month form."""
    if not MONTH_PATTERN.fullmatch(value):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    return value


def month_bounds(month: str) -> tuple[str, str, str]:
    """Return the inclusive start/end and exclusive next-month date strings."""
    require_month(month)
    year, month_number = map(int, month.split("-"))
    start = datetime(year, month_number, 1)
    if month_number == 12:
        next_start = datetime(year + 1, 1, 1)
    else:
        next_start = datetime(year, month_number + 1, 1)
    return (
        start.date().isoformat(),
        (next_start - timedelta(days=1)).date().isoformat(),
        next_start.date().isoformat(),
    )


def mongo_half_up(expression: object, decimals: int) -> dict:
    """Round non-negative aggregation values half-up instead of MongoDB's banker's round."""
    scale = 10**decimals
    return {
        "$divide": [
            {"$floor": {"$add": [{"$multiply": [expression, scale]}, 0.5]}},
            scale,
        ]
    }


def mongo_is_weekday(date_expression: object) -> dict:
    """Return an aggregation expression for Monday through Friday."""
    return {
        "$in": [
            {"$dayOfWeek": {"$dateFromString": {"dateString": date_expression}}},
            [2, 3, 4, 5, 6],
        ]
    }


def mongo_present_days(date_expression: object, status_expression: object, half_day: object) -> dict:
    """Return R5-R7 present-day credit for one attendance-log expression."""
    return {
        "$cond": [
            {
                "$and": [
                    mongo_is_weekday(date_expression),
                    {"$in": [status_expression, PRESENCE_STATUSES]},
                ]
            },
            {"$cond": [{"$eq": [{"$ifNull": [half_day, False]}, True]}, 0.5, 1]},
            0,
        ]
    }


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    """Build the required MongoDB aggregation for one employee's monthly summary."""
    month_start, month_end, next_month = month_bounds(month)
    date_range = {"$gte": month_start, "$lt": next_month}
    working_days = {
        "$cond": [
            {"$gt": ["$joined_on", month_end]},
            0,
            {
                "$size": {
                    "$filter": {
                        "input": {
                            "$map": {
                                "input": {
                                    "$range": [
                                        0,
                                        {
                                            "$add": [
                                                {
                                                    "$dateDiff": {
                                                        "startDate": {
                                                            "$dateFromString": {
                                                                "dateString": {
                                                                    "$cond": [
                                                                        {
                                                                            "$gt": [
                                                                                "$joined_on",
                                                                                month_start,
                                                                            ]
                                                                        },
                                                                        "$joined_on",
                                                                        month_start,
                                                                    ]
                                                                }
                                                            }
                                                        },
                                                        "endDate": {
                                                            "$dateFromString": {
                                                                "dateString": month_end
                                                            }
                                                        },
                                                        "unit": "day",
                                                    }
                                                },
                                                1,
                                            ]
                                        },
                                    ]
                                },
                                "as": "offset",
                                "in": {
                                    "$dateAdd": {
                                        "startDate": {
                                            "$dateFromString": {
                                                "dateString": {
                                                    "$cond": [
                                                        {
                                                            "$gt": [
                                                                "$joined_on", month_start
                                                            ]
                                                        },
                                                        "$joined_on",
                                                        month_start,
                                                    ]
                                                }
                                            }
                                        },
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                            }
                        },
                        "as": "day",
                        "cond": {"$in": [{"$dayOfWeek": "$$day"}, [2, 3, 4, 5, 6]]},
                    }
                }
            },
        ]
    }
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$code"]},
                            "date": date_range,
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": mongo_present_days(
                                    "$date", "$status", "$half_day"
                                )
                            },
                            "leave_days": {
                                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "total_late_minutes": {
                                "$sum": {"$ifNull": ["$late_minutes", 0]}
                            },
                            "total_overtime_minutes": {
                                "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                            },
                        }
                    },
                ],
                "as": "metrics",
            }
        },
        {"$unwind": {"path": "$metrics", "preserveNullAndEmptyArrays": True}},
        {"$set": {"working_days": working_days}},
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": {"$literal": month},
                "working_days": 1,
                "present_days": {"$ifNull": ["$metrics.present_days", 0]},
                "leave_days": {"$ifNull": ["$metrics.leave_days", 0]},
                "late_count": {"$ifNull": ["$metrics.late_count", 0]},
                "total_late_minutes": {"$ifNull": ["$metrics.total_late_minutes", 0]},
                "total_overtime_minutes": {
                    "$ifNull": ["$metrics.total_overtime_minutes", 0]
                },
            }
        },
        {
            "$set": {
                "attendance_pct": {
                    "$cond": [
                        {"$eq": ["$working_days", 0]},
                        None,
                        mongo_half_up(
                            {
                                "$multiply": [
                                    {"$divide": ["$present_days", "$working_days"]},
                                    100,
                                ]
                            },
                            2,
                        ),
                    ]
                }
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str] = None) -> list[dict]:
    """Build the department monthly aggregation, retaining employees with no logs."""
    _month_start, month_end, next_month = month_bounds(month)
    employee_match: dict = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$code"]},
                            "date": {"$gte": month + "-01", "$lt": next_month},
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {"department": "$department", "emp_code": "$emp_code"},
                "present_days": {
                    "$sum": mongo_present_days(
                        "$logs.date", "$logs.status", "$logs.half_day"
                    )
                },
                "work_hours_total": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", PRESENCE_STATUSES]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", PRESENCE_STATUSES]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_hours_total": {"$sum": "$work_hours_total"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$eq": ["$work_hours_count", 0]},
                        None,
                        mongo_half_up(
                            {"$divide": ["$work_hours_total", "$work_hours_count"]}, 2
                        ),
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(
    month: str, limit: int, department: Optional[str] = None
) -> list[dict]:
    """Build a standard-competition-ranking late-arrival leaderboard pipeline."""
    _month_start, _month_end, next_month = month_bounds(month)
    pipeline: list[dict] = [
        {
            "$match": {
                "date": {"$gte": month + "-01", "$lt": next_month},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$group": {
                    "_id": {
                        "emp_code": "$emp_code",
                        "name": "$employee.name",
                        "department": "$employee.department",
                    },
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id.emp_code": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id.emp_code",
                    "name": "$_id.name",
                    "department": "$_id.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def department_trend_pipeline(
    department: str, date_from: str, date_to: str
) -> list[dict]:
    """Build a gap-free department trend entirely inside MongoDB."""
    start_date = {"$dateFromString": {"dateString": date_from}}
    end_date = {"$dateFromString": {"dateString": date_to}}
    date_string = {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}}
    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}
                },
            }
        },
        {
            "$project": {
                "employees": 1,
                "days": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": start_date,
                                                "endDate": end_date,
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": start_date,
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$days"},
        {"$set": {"day": "$days"}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$employees.emp_code", "day": date_string},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$eq": ["$date", "$$day"]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", PRESENCE_STATUSES]},
                                        {
                                            "$cond": [
                                                {
                                                    "$eq": [
                                                        {"$ifNull": ["$half_day", False]},
                                                        True,
                                                    ]
                                                },
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$late_minutes", 0]},
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "metrics",
            }
        },
        {"$unwind": {"path": "$metrics", "preserveNullAndEmptyArrays": True}},
        {
            "$set": {
                "date": date_string,
                "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "employee",
                            "cond": {"$lte": ["$$employee.joined_on", date_string]},
                        }
                    }
                },
                "present_count": {"$ifNull": ["$metrics.present_count", 0]},
                "late_count": {"$ifNull": ["$metrics.late_count", 0]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        mongo_half_up(
                            {"$divide": ["$present_count", "$headcount"]}, 4
                        ),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_raw", None]},
                        None,
                        mongo_half_up("$moving_avg_raw", 4),
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


def attendance_filter(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[Status],
) -> dict:
    """Validate and build the shared attendance-list/explain MongoDB filter."""
    if date_from is not None:
        date_from = require_date(date_from, "date_from")
    if date_to is not None:
        date_to = require_date(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not exceed date_to")
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


# --------------------------------------------------------------------------- #
# Models
#--------------------------------------------------------------------------- #
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
    punched_at: EpochMillis = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: EpochMillis = None
    punch_out: EpochMillis = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


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
    status: Optional[Status] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    records = list(
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [attendance_response(record) for record in records],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    """Apply one audited, optimistic-concurrency-safe manual attendance correction."""
    date = require_date(date, "date")
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    supplied = body.model_fields_set
    status = body.status if "status" in supplied else record["status"]
    original_punch_in = record.get("punch_in")
    original_punch_out = record.get("punch_out")
    punch_in = (
        instant_from_epoch_millis(body.punch_in)
        if "punch_in" in supplied
        else (as_utc(original_punch_in) if original_punch_in is not None else None)
    )
    punch_out = (
        instant_from_epoch_millis(body.punch_out)
        if "punch_out" in supplied
        else (as_utc(original_punch_out) if original_punch_out is not None else None)
    )

    if status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE corrections cannot include punch times",
            )
        punch_in = None
        punch_out = None
        derived = {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    else:
        if punch_in is None:
            raise HTTPException(
                status_code=422, detail="a presence status requires punch_in"
            )
        if (
            attendance_date_for(
                punch_in, employee["shift_start"], employee["shift_end"]
            )
            != date
        ):
            raise HTTPException(
                status_code=422, detail="punch_in must belong to the attendance date"
            )
        if punch_out is not None:
            elapsed_seconds = int((punch_out - punch_in).total_seconds())
            if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )
            work_hours = compute_work_hours(punch_in, punch_out)
            derived = {
                "work_hours": work_hours,
                "late_minutes": compute_late_minutes(
                    punch_in, employee["shift_start"], date
                ),
                "overtime_minutes": compute_overtime(
                    punch_out, employee["shift_start"], employee["shift_end"], date
                ),
                "half_day": work_hours < 4.50,
            }
        else:
            derived = {
                "work_hours": None,
                "late_minutes": compute_late_minutes(
                    punch_in, employee["shift_start"], date
                ),
                "overtime_minutes": 0,
                "half_day": False,
            }

    final_values = {"status": status, "punch_in": punch_in, "punch_out": punch_out, **derived}
    defaults = {
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    }
    changes = {}
    for field, new_value in final_values.items():
        old_value = record.get(field, defaults.get(field))
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(status_code=422, detail="correction does not change the record")

    optimistic_filter: dict = {"_id": record["_id"]}
    for field in final_values:
        if field in record:
            optimistic_filter[field] = record[field]
        else:
            optimistic_filter[field] = {"$exists": False}
    if "history" in record:
        optimistic_filter["history"] = record["history"]
    else:
        optimistic_filter["history"] = {"$exists": False}

    updated = db.attendance_logs.find_one_and_update(
        optimistic_filter,
        {
            "$set": final_values,
            "$push": {
                "history": {
                    "at": datetime.now(timezone.utc),
                    "by": body.regularized_by,
                    "reason": body.reason,
                    "changes": changes,
                }
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance record was updated concurrently")
    return attendance_response(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    require_month(month)
    if db.employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="employee not found")
    result = list(db.employees.aggregate(employee_monthly_pipeline(emp_code, month)))
    return result[0]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    require_month(month)
    return {"month": month, "items": list(db.employees.aggregate(department_summary_pipeline(month, department)))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    require_month(month)
    return {
        "month": month,
        "items": list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department))),
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    date_from = require_date(date_from, "from")
    date_to = require_date(date_to, "to")
    if date_to < date_from:
        raise HTTPException(status_code=422, detail="to must not be before from")
    range_days = (datetime.fromisoformat(date_to) - datetime.fromisoformat(date_from)).days + 1
    if range_days > 92:
        raise HTTPException(status_code=422, detail="range must not exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="department not found")
    return {
        "department": department,
        "items": list(db.employees.aggregate(department_trend_pipeline(department, date_from, date_to))),
    }


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    trend_from: Optional[str] = Query(default=None, alias="from"),
    trend_to: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """Run executionStats explain against the same main query or pipeline as the API."""
    if endpoint == "attendance_list":
        query = attendance_filter(emp_code, date_from, date_to, status)
        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        explain = db.command("explain", command, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipeline = employee_monthly_pipeline(emp_code, require_month(month))
        collection = "employees"
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = department_summary_pipeline(require_month(month), department)
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = late_leaderboard_pipeline(require_month(month), limit, department)
        collection = "attendance_logs"
    else:
        if department is None or trend_from is None or trend_to is None:
            raise HTTPException(
                status_code=422, detail="department, from and to are required"
            )
        trend_from = require_date(trend_from, "from")
        trend_to = require_date(trend_to, "to")
        if trend_to < trend_from:
            raise HTTPException(status_code=422, detail="to must not be before from")
        if (datetime.fromisoformat(trend_to) - datetime.fromisoformat(trend_from)).days + 1 > 92:
            raise HTTPException(status_code=422, detail="range must not exceed 92 days")
        pipeline = department_trend_pipeline(department, trend_from, trend_to)
        collection = "employees"

    explain = db.command(
        "explain",
        {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )
    return {"endpoint": endpoint, "collection": collection, "explain": explain}
