"""Attendance punch, listing, and regularization routes."""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.config import Status
from app.db.mongo import db
from app.schemas.attendance import PunchInIn, PunchOutIn, RegularizeIn
from app.services.analytics import attendance_filter
from app.services.attendance import compute_late_minutes, compute_overtime, compute_work_hours
from app.utils.dates import as_utc, attendance_date_for, instant_from_epoch_millis, require_date
from app.utils.serializers import attendance_response

router = APIRouter(tags=["attendance"])

@router.post("/attendance/punch-in", status_code=201)
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


@router.post("/attendance/punch-out")
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


@router.get("/attendance")
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


@router.patch("/attendance/{emp_code}/{date}")
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
