"""ASGI application entrypoint.

Run with: uvicorn app.main:app --port 8000
"""
from fastapi import FastAPI
from pymongo.errors import PyMongoError

from app.api.routes import admin, analytics, attendance, employees, system
from app.db.mongo import create_indexes

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def ensure_indexes_at_startup() -> None:
    """Create indexes without preventing health reporting during a DB outage."""
    try:
        create_indexes()
    except PyMongoError:
        pass


app.include_router(system.router)
app.include_router(employees.router)
app.include_router(attendance.router)
app.include_router(analytics.router)
app.include_router(admin.router)
