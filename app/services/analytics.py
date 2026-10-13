"""MongoDB aggregation builders and attendance query filtering."""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException

from app.core.config import PRESENCE_STATUSES, Status
from app.utils.dates import month_bounds, require_date, require_month

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
