import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.core.database import get_db


DEFAULT_CURRENCY = os.getenv("DEFAULT_USAGE_CURRENCY", "INR")
META_CONVERSATION_WINDOW_HOURS = int(os.getenv("META_CONVERSATION_WINDOW_HOURS", "24"))
META_CONVERSATION_CATEGORY = os.getenv("META_CONVERSATION_CATEGORY", "service")
META_CONVERSATION_PRICE = float(
    os.getenv("META_CONVERSATION_PRICE", os.getenv("META_CONVERSATION_PRICE_INR", "0"))
)
GEMINI_INPUT_PRICE_PER_MILLION = float(
    os.getenv("GEMINI_INPUT_PRICE_PER_MILLION", os.getenv("GEMINI_INPUT_PRICE_PER_MILLION_INR", "0"))
)
GEMINI_OUTPUT_PRICE_PER_MILLION = float(
    os.getenv("GEMINI_OUTPUT_PRICE_PER_MILLION", os.getenv("GEMINI_OUTPUT_PRICE_PER_MILLION_INR", "0"))
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _currency(client: Optional[Dict[str, Any]]) -> str:
    return str((client or {}).get("usage_currency") or (client or {}).get("currency") or DEFAULT_CURRENCY)


def _client_identity(client: Optional[Dict[str, Any]], channel_id: str) -> Dict[str, str]:
    config = client or {}
    client_id = str(config.get("_id") or channel_id)
    return {
        "client_id": client_id,
        "channel_id": str(channel_id),
        "phone_number_id": str(config.get("phone_number_id") or channel_id),
    }


def _model_pricing(client: Optional[Dict[str, Any]], model: str) -> Dict[str, float]:
    pricing = (client or {}).get("ai_model_pricing", {})
    model_pricing = pricing.get(model, {}) if isinstance(pricing, dict) else {}

    return {
        "input_per_million": _float_value(
            model_pricing.get("input_per_million")
            or model_pricing.get("input_per_million_inr")
            or model_pricing.get("input_per_million_usd")
            or model_pricing.get("prompt_per_million")
            or model_pricing.get("prompt_per_million_inr")
            or model_pricing.get("prompt_per_million_usd")
            or (client or {}).get("ai_input_price_per_million")
            or (client or {}).get("ai_input_price_per_million_inr")
            or (client or {}).get("ai_input_price_per_million_usd"),
            GEMINI_INPUT_PRICE_PER_MILLION,
        ),
        "output_per_million": _float_value(
            model_pricing.get("output_per_million")
            or model_pricing.get("output_per_million_inr")
            or model_pricing.get("output_per_million_usd")
            or model_pricing.get("completion_per_million")
            or model_pricing.get("completion_per_million_inr")
            or model_pricing.get("completion_per_million_usd")
            or (client or {}).get("ai_output_price_per_million")
            or (client or {}).get("ai_output_price_per_million_inr")
            or (client or {}).get("ai_output_price_per_million_usd"),
            GEMINI_OUTPUT_PRICE_PER_MILLION,
        ),
    }


def extract_token_usage(response: Any) -> Dict[str, int]:
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    completion_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
    total_tokens = int(getattr(usage, "total_token_count", 0) or prompt_tokens + completion_tokens)

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def record_ai_model_usage(
    tenant_id: str,
    channel_id: str,
    provider: str,
    model: str,
    operation: str,
    token_usage: Dict[str, int],
    client_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    pricing = _model_pricing(client_config, model)
    identity = _client_identity(client_config, channel_id)
    currency = _currency(client_config)
    prompt_tokens = int(token_usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(token_usage.get("completion_tokens", 0) or 0)
    total_tokens = int(token_usage.get("total_tokens", 0) or prompt_tokens + completion_tokens)
    input_cost = (prompt_tokens / 1_000_000) * pricing["input_per_million"]
    output_cost = (completion_tokens / 1_000_000) * pricing["output_per_million"]
    total_cost = round(input_cost + output_cost, 8)

    document = {
        "tenant_id": tenant_id,
        **identity,
        "provider": provider,
        "model": model,
        "operation": operation,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "input_price_per_million": pricing["input_per_million"],
        "output_price_per_million": pricing["output_per_million"],
        "total_cost": total_cost,
        "currency": currency,
        "pricing_snapshot": {
            "input_price_per_million": pricing["input_per_million"],
            "output_price_per_million": pricing["output_per_million"],
            "currency": currency,
        },
        "created_at": _now(),
    }

    get_db().ai_model_usage.insert_one(document)
    return document


def record_meta_conversation_usage(
    client: Dict[str, Any],
    tenant_id: str,
    channel_id: str,
    customer_phone: str,
    send_result: Dict[str, Any],
) -> Dict[str, Any]:
    db = get_db()
    now = _now()
    identity = _client_identity(client, channel_id)
    window_hours = int(client.get("meta_conversation_window_hours") or META_CONVERSATION_WINDOW_HOURS)
    category = client.get("meta_conversation_category") or META_CONVERSATION_CATEGORY
    price = _float_value(
        client.get("meta_conversation_price")
        or client.get("meta_conversation_price_inr")
        or client.get("meta_conversation_price_usd"),
        META_CONVERSATION_PRICE,
    )
    currency = _currency(client)

    active = db.meta_conversation_usage.find_one(
        {
            "tenant_id": tenant_id,
            "client_id": identity["client_id"],
            "phone_number_id": identity["phone_number_id"],
            "customer_phone": customer_phone,
            "category": category,
            "expires_at": {"$gt": now},
        }
    )
    if active:
        return {"status": "existing", "conversation_id": str(active.get("_id")), "currency": active.get("currency", currency)}

    message_id = None
    messages = send_result.get("messages") if isinstance(send_result, dict) else None
    if messages and isinstance(messages, list):
        message_id = messages[0].get("id")

    document = {
        "tenant_id": tenant_id,
        **identity,
        "customer_phone": customer_phone,
        "category": category,
        "price": price,
        "currency": currency,
        "pricing_snapshot": {
            "price": price,
            "currency": currency,
            "window_hours": window_hours,
            "category": category,
        },
        "opened_at": now,
        "expires_at": now + timedelta(hours=window_hours),
        "window_hours": window_hours,
        "meta_message_id": message_id,
        "raw_send_result": send_result,
        "created_at": now,
    }

    inserted = db.meta_conversation_usage.insert_one(document)
    return {"status": "created", "conversation_id": str(inserted.inserted_id), "price": price, "currency": currency}


def _match_for_tenant(tenant_id: str, start_at: Optional[datetime], end_at: Optional[datetime]) -> Dict[str, Any]:
    query: Dict[str, Any] = {"tenant_id": tenant_id}
    if start_at or end_at:
        query["created_at"] = {}
        if start_at:
            query["created_at"]["$gte"] = start_at
        if end_at:
            query["created_at"]["$lte"] = end_at
    return query


def usage_summary(tenant_id: str, start_at: Optional[datetime] = None, end_at: Optional[datetime] = None) -> Dict[str, Any]:
    db = get_db()
    ai_match = _match_for_tenant(tenant_id, start_at, end_at)
    meta_match = _match_for_tenant(tenant_id, start_at, end_at)

    ai_by_model = list(
        db.ai_model_usage.aggregate(
            [
                {"$match": ai_match},
                {
                    "$group": {
                        "_id": {"provider": "$provider", "model": "$model", "currency": {"$ifNull": ["$currency", DEFAULT_CURRENCY]}},
                        "requests": {"$sum": 1},
                        "prompt_tokens": {"$sum": "$prompt_tokens"},
                        "completion_tokens": {"$sum": "$completion_tokens"},
                        "total_tokens": {"$sum": "$total_tokens"},
                        "total_cost": {"$sum": {"$ifNull": ["$total_cost", "$total_cost_usd"]}},
                    }
                },
                {"$sort": {"total_cost": -1}},
            ]
        )
    )
    meta_by_category = list(
        db.meta_conversation_usage.aggregate(
            [
                {"$match": meta_match},
                {
                    "$group": {
                        "_id": {"category": "$category", "currency": {"$ifNull": ["$currency", DEFAULT_CURRENCY]}},
                        "conversations": {"$sum": 1},
                        "total_cost": {"$sum": {"$ifNull": ["$price", "$price_usd"]}},
                    }
                },
                {"$sort": {"total_cost": -1}},
            ]
        )
    )
    ai_by_phone_number = list(
        db.ai_model_usage.aggregate(
            [
                {"$match": ai_match},
                {
                    "$group": {
                        "_id": {"phone_number_id": {"$ifNull": ["$phone_number_id", "$channel_id"]}, "currency": {"$ifNull": ["$currency", DEFAULT_CURRENCY]}},
                        "ai_requests": {"$sum": 1},
                        "ai_tokens": {"$sum": "$total_tokens"},
                        "ai_cost": {"$sum": {"$ifNull": ["$total_cost", "$total_cost_usd"]}},
                    }
                },
            ]
        )
    )
    meta_by_phone_number = list(
        db.meta_conversation_usage.aggregate(
            [
                {"$match": meta_match},
                {
                    "$group": {
                        "_id": {"phone_number_id": {"$ifNull": ["$phone_number_id", "$channel_id"]}, "currency": {"$ifNull": ["$currency", DEFAULT_CURRENCY]}},
                        "meta_conversations": {"$sum": 1},
                        "meta_cost": {"$sum": {"$ifNull": ["$price", "$price_usd"]}},
                    }
                },
            ]
        )
    )

    currency = DEFAULT_CURRENCY
    phone_number_totals: Dict[str, Dict[str, Any]] = {}
    for item in ai_by_phone_number:
        phone_number_id = str(item.get("_id", {}).get("phone_number_id") or "unknown")
        currency = item.get("_id", {}).get("currency") or currency
        phone_number_totals[phone_number_id] = {
            "phone_number_id": phone_number_id,
            "currency": currency,
            "ai_requests": item.get("ai_requests", 0),
            "ai_tokens": item.get("ai_tokens", 0),
            "ai_cost": item.get("ai_cost", 0),
            "meta_conversations": 0,
            "meta_cost": 0,
        }
    for item in meta_by_phone_number:
        phone_number_id = str(item.get("_id", {}).get("phone_number_id") or "unknown")
        currency = item.get("_id", {}).get("currency") or currency
        current = phone_number_totals.setdefault(
            phone_number_id,
            {
                "phone_number_id": phone_number_id,
                "currency": currency,
                "ai_requests": 0,
                "ai_tokens": 0,
                "ai_cost": 0,
                "meta_conversations": 0,
                "meta_cost": 0,
            },
        )
        current["meta_conversations"] = item.get("meta_conversations", 0)
        current["meta_cost"] = item.get("meta_cost", 0)

    ai_total = round(sum(item.get("total_cost", 0) for item in ai_by_model), 8)
    meta_total = round(sum(item.get("total_cost", 0) for item in meta_by_category), 8)

    return {
        "tenant_id": tenant_id,
        "currency": currency,
        "ai_model_usage": {
            "total_cost": ai_total,
            "by_model": [
                {
                    "provider": item["_id"].get("provider"),
                    "model": item["_id"].get("model"),
                    "currency": item["_id"].get("currency"),
                    "requests": item.get("requests", 0),
                    "prompt_tokens": item.get("prompt_tokens", 0),
                    "completion_tokens": item.get("completion_tokens", 0),
                    "total_tokens": item.get("total_tokens", 0),
                    "total_cost": round(item.get("total_cost", 0), 8),
                }
                for item in ai_by_model
            ],
        },
        "meta_conversations": {
            "total_cost": meta_total,
            "by_category": [
                {
                    "category": item["_id"].get("category"),
                    "currency": item["_id"].get("currency"),
                    "conversations": item.get("conversations", 0),
                    "total_cost": round(item.get("total_cost", 0), 8),
                }
                for item in meta_by_category
            ],
        },
        "by_phone_number_id": [
            {
                "phone_number_id": item.get("phone_number_id"),
                "currency": item.get("currency", currency),
                "ai_requests": item.get("ai_requests", 0),
                "ai_tokens": item.get("ai_tokens", 0),
                "ai_cost": round(item.get("ai_cost", 0), 8),
                "meta_conversations": item.get("meta_conversations", 0),
                "meta_cost": round(item.get("meta_cost", 0), 8),
                "total_cost": round(item.get("ai_cost", 0) + item.get("meta_cost", 0), 8),
            }
            for item in sorted(phone_number_totals.values(), key=lambda value: value["phone_number_id"])
        ],
        "total_cost": round(ai_total + meta_total, 8),
    }
