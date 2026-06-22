from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query

from app.core.database import get_db
from app.services.pricing_service import usage_summary

router = APIRouter(prefix="/usage", tags=["usage"])


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _serialize_document(document: Dict[str, Any]) -> Dict[str, Any]:
    document["_id"] = str(document["_id"])
    return document


@router.get("/{tenant_id}/summary")
async def get_usage_summary(
    tenant_id: str,
    start_at: Optional[str] = Query(None, description="ISO datetime, inclusive"),
    end_at: Optional[str] = Query(None, description="ISO datetime, inclusive"),
):
    return await usage_summary(
        tenant_id=tenant_id,
        start_at=_parse_datetime(start_at),
        end_at=_parse_datetime(end_at),
    )


@router.get("/{tenant_id}/ai")
async def get_ai_usage(
    tenant_id: str,
    limit: int = Query(100, ge=1, le=500),
):
    cursor = (
        get_db()
        .ai_model_usage.find({"tenant_id": tenant_id})
        .sort("created_at", -1)
        .limit(limit)
    )
    items = await cursor.to_list(length=limit)
    return {"tenant_id": tenant_id, "items": [_serialize_document(item) for item in items]}


@router.get("/{tenant_id}/meta")
async def get_meta_conversation_usage(
    tenant_id: str,
    limit: int = Query(100, ge=1, le=500),
):
    cursor = (
        get_db()
        .meta_conversation_usage.find({"tenant_id": tenant_id})
        .sort("created_at", -1)
        .limit(limit)
    )
    items = await cursor.to_list(length=limit)
    return {"tenant_id": tenant_id, "items": [_serialize_document(item) for item in items]}
