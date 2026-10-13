"""MongoDB client, database handle, and index bootstrap."""
from pymongo import ASCENDING, DESCENDING, MongoClient

from app.core.config import MONGO_DB, MONGO_URI

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

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

