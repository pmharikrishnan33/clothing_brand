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


async def record_ai_model_usage(
    tenant_id: str,
    channel_id: str,
    provider: str,
    model: str,
    operation: str,
    token_usage: Dict[str, int],
    client_config: Optional[Dict[str, Any]] = None,
    interaction_id: Optional[str] = None, # <--- NEW PARAMETER
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

    db = get_db()
    now = _now()

    # Consolidation: maintain a single cumulative document per tenant_id
    await db.ai_model_usage.update_one(
        {"tenant_id": tenant_id},
        {
            "$setOnInsert": {
                "created_at": now,
            },
            "$set": {
                **identity,
                "provider": provider,
                "model": model,
                "input_price_per_million": pricing["input_per_million"],
                "output_price_per_million": pricing["output_per_million"],
                "currency": currency,
                "updated_at": now
            },
            "$inc": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "total_cost": total_cost,
                "total_requests": 1
            },
            "$addToSet": {
                "operations": operation
            }
        },
        upsert=True
    )
    return {"status": "success", "tenant_id": tenant_id}


async def record_meta_conversation_usage(
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

    active = await db.meta_conversation_usage.find_one(
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

    inserted = await db.meta_conversation_usage.insert_one(document)
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


async def usage_summary(tenant_id: str, start_at: Optional[datetime] = None, end_at: Optional[datetime] = None) -> Dict[str, Any]:
    db = get_db()
    ai_match = _match_for_tenant(tenant_id, start_at, end_at)
    meta_match = _match_for_tenant(tenant_id, start_at, end_at)

    ai_by_model = await db.ai_model_usage.aggregate(
            [
                {"$match": ai_match},
                {
                    "$group": {
                        "_id": {"provider": "$provider", "model": "$model", "currency": {"$ifNull": ["$currency", DEFAULT_CURRENCY]}},
                        "requests": {"$sum": {"$ifNull": ["$total_requests", 1]}},
                        "prompt_tokens": {"$sum": "$prompt_tokens"},
                        "completion_tokens": {"$sum": "$completion_tokens"},
                        "total_tokens": {"$sum": "$total_tokens"},
                        "total_cost": {"$sum": {"$ifNull": ["$total_cost", "$total_cost_usd"]}},
                    }
                },
                {"$sort": {"total_cost": -1}},
            ]
        ).to_list(length=None)

    meta_by_category_raw = await db.meta_conversation_usage.aggregate(
            [
                {"$match": meta_match},
                {
                    "$group": {
                        "_id": {"category": "$category", "currency": {"$ifNull": ["$currency", DEFAULT_CURRENCY]}},
                        "conversations": {"$sum": 1},
                        "total_cost_raw": {"$sum": {"$ifNull": ["$price", "$price_usd"]}},
                    }
                },
            ]
        ).to_list(length=None)
    
    currency = DEFAULT_CURRENCY

    # --- META CONVERSATION LOGIC (First 1000 Free) ---
    meta_categories_processed = []
    meta_total_cost = 0.0

    for item in meta_by_category_raw:
        category = item["_id"].get("category")
        curr = item["_id"].get("currency")
        conversations = item.get("conversations", 0)
        raw_cost = item.get("total_cost_raw", 0)
        
        # Calculate unit price
        unit_price = raw_cost / conversations if conversations > 0 else 0
        
        # Meta allows 1000 free "service" (user-initiated) conversations
        free_allowance = 1000 if category == "service" else 0
        free_conversations_used = min(conversations, free_allowance)
        billable_conversations = max(0, conversations - free_allowance)
        
        # Only charge for conversations AFTER the first 1000
        adjusted_cost = billable_conversations * unit_price
        meta_total_cost += adjusted_cost
        
        meta_categories_processed.append({
            "category": category,
            "currency": curr,
            "total_conversations": conversations,
            "free_conversations": free_conversations_used,
            "billable_conversations": billable_conversations,
            "dynamic_total_cost": round(adjusted_cost, 8)
        })

    # --- AI MODEL LOGIC (Dynamic Tokens) ---
    ai_total_cost = 0.0
    ai_models_processed = []
    
    for item in ai_by_model:
        cost = item.get("total_cost", 0)
        ai_total_cost += cost
        ai_models_processed.append({
            "provider": item["_id"].get("provider"),
            "model": item["_id"].get("model"),
            "currency": item["_id"].get("currency"),
            "total_requests": item.get("requests", 0),
            "dynamic_prompt_tokens": item.get("prompt_tokens", 0),
            "dynamic_completion_tokens": item.get("completion_tokens", 0),
            "total_tokens": item.get("total_tokens", 0),
            "dynamic_total_cost": round(cost, 8),
        })

    return {
        "tenant_id": tenant_id,
        "currency": currency,
        "ai_model_usage": {
            "total_cost": round(ai_total_cost, 8),
            "by_model": ai_models_processed, # <--- This will automatically list all 3 models with dynamic tokens
        },
        "meta_conversations": {
            "total_cost": round(meta_total_cost, 8),
            "by_category": meta_categories_processed, # <--- Only bills AFTER 1000 free
        },
        "total_combined_cost": round(ai_total_cost + meta_total_cost, 8),
    }