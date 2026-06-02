import json
import logging
from datetime import datetime, timezone
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from app.core.database import get_db
from app.services.ai_service import (
    ai_extract_info_openai,
    ai_fallback_response_openai,
    generate_response_with_openai,
)

logger = logging.getLogger(__name__)

# -------------------- CONFIGURATION FLAGS --------------------
ENABLE_AI_EXTRACTION = True
ENABLE_AI_FALLBACK = True


def analyze_message(message: str, keywords: List[Dict[str, Any]]) -> Dict[str, Any]:
    msg_lower = (message or "").lower()
    matched = []

    for item in keywords:
        kw = str(item.get("keyword", "")).lower()
        match_type = item.get("match_type", "contains")

        if not kw:
            continue

        if match_type == "contains" and kw in msg_lower:
            matched.append(item)
        elif match_type == "exact" and msg_lower == kw:
            matched.append(item)
        elif match_type == "startswith" and msg_lower.startswith(kw):
            matched.append(item)

    result = {
        "matched": matched,
        "count": len(matched),
        "simple_reply": None,
    }

    if len(matched) == 1:
        result["simple_reply"] = matched[0].get("response")

    return result


def lookup_learned_keywords(matched_keywords: List[str], learned: List[Dict[str, Any]]) -> Optional[str]:
    sorted_incoming = sorted(matched_keywords)

    for entry in learned:
        sorted_entry = sorted(entry.get("keywords", []))
        if sorted_incoming == sorted_entry:
            return entry.get("response")

    return None


def save_learned_keywords(filepath: str, learned: List[Dict[str, Any]]) -> None:
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(learned, f, ensure_ascii=False, indent=2)


def save_learned_combination(
    tenant_id: str,
    keywords_list: List[str],
    response: str,
    learned: List[Dict[str, Any]],
    filepath: Optional[str] = None,
) -> None:
    db = get_db()
    sorted_keywords = sorted(keywords_list)

    for entry in learned:
        if sorted(entry.get("keywords", [])) == sorted_keywords:
            return

    document = {
        "tenant_id": tenant_id,
        "keywords": sorted_keywords,
        "response": response,
    }
    db.learned_responses.insert_one(document)

    learned.append(document)

    if filepath:
        save_learned_keywords(filepath, learned)


def get_keywords_by_tenant(tenant_id: str) -> List[Dict[str, Any]]:
    db = get_db()
    cursor = db.keywords.find({"tenant_id": tenant_id, "is_active": {"$ne": False}})
    return list(cursor)


def get_learned_keywords_by_tenant(tenant_id: str) -> List[Dict[str, Any]]:
    db = get_db()
    cursor = db.learned_responses.find({"tenant_id": tenant_id})
    return list(cursor)


def save_message_record(
    tenant_id: str,
    channel_id: str,
    customer_phone: str,
    direction: str,
    body: str,
    raw_payload: Optional[Dict[str, Any]] = None,
) -> None:
    db = get_db()
    db.messages.insert_one(
        {
            "tenant_id": tenant_id,
            "channel_id": channel_id,
            "customer_phone": customer_phone,
            "direction": direction,
            "body": body,
            "raw_payload": raw_payload or {},
        }
    )


def save_conversation_touch(tenant_id: str, channel_id: str, customer_phone: str) -> None:
    db = get_db()
    db.conversations.update_one(
        {
            "tenant_id": tenant_id,
            "channel_id": channel_id,
            "customer_phone": customer_phone,
        },
        {
            "$setOnInsert": {
                "tenant_id": tenant_id,
                "channel_id": channel_id,
                "customer_phone": customer_phone,
                "status": "active",
            },
            "$set": {"last_message_at": datetime.now(timezone.utc)},
        },
        upsert=True,
    )


def send_whatsapp_message(phone_number_id: str, access_token: str, to: str, body: str) -> Dict[str, Any]:
    url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {"status": "sent"}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8") if exc.fp else str(exc)
        logger.error("WhatsApp send error: %s", error_body)
        return {"status": "error", "detail": error_body}
    except Exception as exc:
        logger.exception("Unexpected WhatsApp send error")
        return {"status": "error", "detail": str(exc)}


def _extract_message_text(message: Dict[str, Any]) -> str:
    if not message:
        return ""

    if message.get("type") == "text":
        return message.get("text", {}).get("body", "") or ""

    if "button" in message:
        return message.get("button", {}).get("text", "") or ""

    if "interactive" in message:
        interactive = message.get("interactive", {})
        button_reply = interactive.get("button_reply", {})
        list_reply = interactive.get("list_reply", {})
        return button_reply.get("title") or list_reply.get("title") or ""

    return message.get("text", {}).get("body", "") or ""


async def message_service(client: Dict[str, Any], from_phone: str, text_data: str, phone_id: str) -> Dict[str, Any]:
    tenant_id = str(client.get("tenant_id") or client.get("_id") or phone_id)
    channel_id = str(client.get("_id") or phone_id)
    incoming_text = (text_data or "").strip()

    if not incoming_text:
        return {"status": "ignored", "reason": "empty_message"}

    save_message_record(
        tenant_id=tenant_id,
        channel_id=channel_id,
        customer_phone=from_phone,
        direction="inbound",
        body=incoming_text,
        raw_payload={"phone_id": phone_id},
    )
    save_conversation_touch(tenant_id, channel_id, from_phone)

    keywords = get_keywords_by_tenant(tenant_id)
    analysis = analyze_message(incoming_text, keywords)

    reply = analysis.get("simple_reply")
    learned = get_learned_keywords_by_tenant(tenant_id)

    if reply is None and analysis.get("matched"):
        matched_keywords = [item.get("keyword", "") for item in analysis["matched"] if item.get("keyword")]
        reply = lookup_learned_keywords(matched_keywords, learned)

    if not reply:
        if ENABLE_AI_EXTRACTION:
            extraction = ai_extract_info_openai(incoming_text)
            reply = generate_response_with_openai(extraction, incoming_text)
        elif ENABLE_AI_FALLBACK:
            reply = ai_fallback_response_openai(incoming_text)
        else:
            reply = "Thanks for your message. We will get back to you shortly."

    access_token = client.get("whatsapp_token", "")
    if not access_token:
        logger.error("Missing WhatsApp token for tenant_id=%s phone_id=%s", tenant_id, phone_id)
        return {
            "status": "error",
            "tenant_id": tenant_id,
            "reason": "missing_whatsapp_token",
        }

    send_result = send_whatsapp_message(phone_id, access_token, from_phone, reply)
    if send_result.get("status") == "error":
        return {
            "status": "error",
            "tenant_id": tenant_id,
            "reason": "whatsapp_send_failed",
            "detail": send_result.get("detail"),
        }

    save_message_record(
        tenant_id=tenant_id,
        channel_id=channel_id,
        customer_phone=from_phone,
        direction="outbound",
        body=reply,
        raw_payload={"source": "message_service", "send_result": send_result},
    )

    return {
        "status": "success",
        "tenant_id": tenant_id,
        "reply": reply,
        "send_result": send_result,
    }
