"""MongoDB document serializers for API responses."""
from app.utils.dates import epoch_millis

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
