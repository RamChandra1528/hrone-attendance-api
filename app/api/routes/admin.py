"""Administrative query-plan inspection route."""
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query

from app.core.config import Status
from app.db.mongo import db
from app.services.analytics import (
    attendance_filter,
    department_summary_pipeline,
    department_trend_pipeline,
    employee_monthly_pipeline,
    late_leaderboard_pipeline,
)
from app.utils.dates import require_date, require_month

router = APIRouter(prefix="/admin", tags=["admin"])

@router.get("/explain/{endpoint}")
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

