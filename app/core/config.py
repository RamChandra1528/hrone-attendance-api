"""Application configuration and shared constants."""
import os
import re
from datetime import timedelta, timezone
from typing import Annotated, Literal

from dotenv import load_dotenv
from pydantic import Field, StrictInt

# Environment values supplied by deployment always take precedence.
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
IST = timezone(timedelta(hours=5, minutes=30))
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
EpochMillis = Annotated[StrictInt, Field(ge=100000000000, le=4102444800000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]

