"""System-health routes."""
from fastapi import APIRouter, HTTPException
from pymongo.errors import PyMongoError

from app.db.mongo import db

router = APIRouter(tags=["system"])

@router.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok"}

