"""Attendance analytics routes."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.db.mongo import db
from app.services.analytics import (
    department_summary_pipeline,
    department_trend_pipeline,
    employee_monthly_pipeline,
    late_leaderboard_pipeline,
)
from app.utils.dates import require_date, require_month

router = APIRouter(prefix="/analytics", tags=["analytics"])

@router.get("/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    require_month(month)
    if db.employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="employee not found")
    result = list(db.employees.aggregate(employee_monthly_pipeline(emp_code, month)))
    return result[0]


@router.get("/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    require_month(month)
    return {"month": month, "items": list(db.employees.aggregate(department_summary_pipeline(month, department)))}


@router.get("/leaderboard/late")
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

@router.get("/departments/{department}/trend")
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
