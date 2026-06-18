import json
import logging
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import uuid, Tuple
import uuid
from app.core.config import http_client, clean_shopify_url


from app.core.database import get_db
from app.services.inventory_service import search_tenant_inventory, format_manual_inventory_for_ai, format_single_item_for_whatsapp, get_distinct_manual_categories
from app.services.inventory_service import search_tenant_inventory, format_manual_inventory_for_ai, format_single_item_for_whatsapp
from app.services.ai_service import (
    ai_extract_info,
    ai_fallback_response,
    generate_ai_response,
)
from app.services.pricing_service import record_meta_conversation_usage
from app.services.shopify_service import extract_rules_info, fetch_clothing_inventory, format_products_for_ai, format_single_product_for_whatsapp, get_distinct_shopify_product_types
from app.services.shopify_service import extract_rules_info, fetch_clothing_inventory, format_products_for_ai, format_single_product_for_whatsapp

logger = logging.getLogger(__name__)

# -------------------- CONFIGURATION FLAGS --------------------
ENABLE_AI_EXTRACTION = True
ENABLE_AI_FALLBACK = True
ENABLE_SHOPIFY_INTEGRATION = True


def analyze_message(message: str, keywords: List[Dict[str, Any]]) -> Dict[str, Any]:
    msg_lower = (message or "").lower()
    matched = []

    for item in keywords:
        keywords_list = item.get("keywords", [])
        # Fallback to legacy single keyword field if keywords list is empty
        if not keywords_list and item.get("keyword"):
            keywords_list = [item.get("keyword")]
            
        match_type = item.get("match_type", "contains")
        is_match = False

        for kw in keywords_list:
            kw_lower = str(kw).lower()
            if not kw_lower:
                continue

            if match_type == "contains" and kw_lower in msg_lower:
                is_match = True
            elif match_type == "exact" and msg_lower == kw_lower:
                is_match = True
            elif match_type == "startswith" and msg_lower.startswith(kw_lower):
                is_match = True
            
            if is_match:
                matched.append(item)
                break

    result = {
        "matched": matched,
        "count": len(matched),
        "simple_reply": None,
    }

    if len(matched) == 1:
        # Support both 'response' (string) and 'responses' (list of strings)
        responses = matched[0].get("responses")
        if isinstance(responses, list) and responses:
            result["simple_reply"] = random.choice(responses)
        else:
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


async def save_learned_combination(
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
    
    new_entry = {"keywords": sorted_keywords, "response": response}
    
    # UPSERT: Add to the 'learned' array inside the tenant's single document
    await db.learned_keywords.update_one(
        {"tenant_id": tenant_id},
        {"$push": {"learned": new_entry}},
        upsert=True
    )

    learned.append(new_entry)

    if filepath:
        save_learned_keywords(filepath, learned)


async def get_keywords_by_tenant(tenant_id: str) -> List[Dict[str, Any]]:
    db = get_db()
    doc = await db.keywords.find_one({"tenant_id": tenant_id})
    if not doc:
        return []
    # Filter active rules from the client's 'rules' array
    return [r for r in doc.get("rules", []) if r.get("is_active") is not False]


async def get_learned_keywords_by_tenant(tenant_id: str) -> List[Dict[str, Any]]:
    db = get_db()
    doc = await db.learned_keywords.find_one({"tenant_id": tenant_id})
    return doc.get("learned", []) if doc else []


async def save_message_record(
    tenant_id: str,
    channel_id: str,
    customer_phone: str,
    direction: str,
    body: str,
    raw_payload: Optional[Dict[str, Any]] = None,
) -> None:
    db = get_db()
    await db.messages.insert_one(
        {
            "tenant_id": tenant_id,
            "channel_id": channel_id,
            "customer_phone": customer_phone,
            "direction": direction,
            "body": body,
            "raw_payload": raw_payload or {},
        }
    )


async def save_conversation_touch(tenant_id: str, channel_id: str, customer_phone: str) -> None:
    db = get_db()
    await db.conversations.update_one(
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


async def send_whatsapp_message(
    phone_number_id: str, 
    access_token: str, 
    to: str, 
    body: str, 
    image_url: Optional[str] = None,
    interactive_payload: Optional[Dict[str, Any]] = None # New parameter for interactive messages
    image_url: Optional[str] = None
) -> Dict[str, Any]:
    url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
    }
    
    if interactive_payload:
        payload["type"] = "interactive"
        payload["interactive"] = interactive_payload
    elif image_url:
    if image_url:
        payload["type"] = "image"
        payload["image"] = {"link": image_url, "caption": body}
    else:
        payload["type"] = "text"
        payload["text"] = {"body": body}

    try:
        response = await http_client.post(url, json=payload, headers=headers)
        if response.status_code >= 400:
            logger.error("WhatsApp send error: %s", response.text)
            return {"status": "error", "detail": response.text}
        return response.json()
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
    items_to_send = []

    interaction_id = str(uuid.uuid4())

    if not incoming_text:
        return {"status": "ignored", "reason": "empty_message"}

    await save_message_record(
        tenant_id=tenant_id,
        channel_id=channel_id,
        customer_phone=from_phone,
        direction="inbound",
        body=incoming_text,
        raw_payload={"phone_id": phone_id},
    )
    await save_conversation_touch(tenant_id, channel_id, from_phone)

    keywords = await get_keywords_by_tenant(tenant_id)
    analysis = analyze_message(incoming_text, keywords)

    reply = analysis.get("simple_reply")
    learned = await get_learned_keywords_by_tenant(tenant_id)

    if reply is None and analysis.get("matched"):
        # Collect the primary keyword (first in the list) from each matched group 
        # to maintain compatibility with the learned responses logic.
        matched_keywords = []
        for item in analysis["matched"]:
            kws = item.get("keywords") or ([item.get("keyword")] if item.get("keyword") else [])
            if kws:
                matched_keywords.append(kws[0])
        reply = lookup_learned_keywords(matched_keywords, learned)

    if not reply:
        if ENABLE_AI_EXTRACTION:
            inventory_source = client.get("inventory_source", "manual")
            # Extract Shopify Config from DB Client object
            s_url = clean_shopify_url(client.get("shopify_url") or client.get("shopify_store_url") or "")
            s_token = client.get("shopify_access_token", "")
            s_ver = client.get("shopify_api_version", "2024-01")

            # Step 1: AI Extraction (now includes 'general_inquiry')
            extraction = await ai_extract_info(
                incoming_text,
                tenant_id=tenant_id,
                channel_id=channel_id,
                client_config=client,
                interaction_id=interaction_id,
            )
            # Step 1: Rule-Based Extraction
            rules_data = extract_rules_info(incoming_text)
            inventory_summary = None
            
            # Handle General Inquiry (Point 3)
            if extraction.get("action") == "general_inquiry":
                categories_list = []
            # Step 2: Try Product Search using Rules
            if rules_data["has_data"]:
                if inventory_source == "shopify" and ENABLE_SHOPIFY_INTEGRATION:
                    categories_list = await get_distinct_shopify_product_types(
                        shop_url=s_url, access_token=s_token, api_version=s_ver
                    color_str = " ".join(rules_data['colors'])
                    search_query = f"{color_str} {rules_data['type'] or ''}".strip() or rules_data['category']
                    products = await fetch_clothing_inventory(
                        shop_url=s_url, access_token=s_token, api_version=s_ver,
                        query=search_query, 
                        category=rules_data['category'], 
                        max_price=rules_data['max_price']
                    )
                    if products:
                        for p in products[:3]:
                            img_data = p.get("image") or (p.get("images", [{}])[0] if p.get("images") else None)
                            items_to_send.append({
                                "text": format_single_product_for_whatsapp(p, shop_url=s_url),
                                "image": img_data.get("src") if img_data else None
                            })
                    
                    inventory_summary = format_products_for_ai(products, shop_url=s_url)
                elif inventory_source == "manual":
                    categories_list = await get_distinct_manual_categories(tenant_id)
                
                if categories_list:
                    sections = [{"title": "Categories", "rows": []}]
                    for i, cat in enumerate(categories_list[:10]): # Limit to 10 for WhatsApp list message
                        sections[0]["rows"].append({
                            "id": f"category_{cat.lower().replace(' ', '_')}",
                            "title": cat,
                            "description": f"Explore {cat}"
                        )
                    
                    interactive_payload = {
                        "type": "list",
                        "header": {"type": "text", "text": "Explore Our Collection"},
                        "body": {"text": "We offer a wide variety of premium clothing. Please select a category to explore:"},
                        "footer": {"text": "Tap to view options"},
                        "action": {
                            "button": "View Categories",
                            "sections": sections
                        }
                    }
                    # Send interactive message and return
                    send_result = await send_whatsapp_message(
                        phone_id, access_token, from_phone, interactive_payload=interactive_payload
                    )
                    if send_result.get("status") == "error":
                        logger.error("Failed to send interactive category message: %s", send_result.get("detail"))
                        return {"status": "error", "reason": "whatsapp_send_failed"}
                    
                    await save_message_record(
                    items = await search_tenant_inventory(
                        tenant_id=tenant_id,
                        channel_id=channel_id,
                        customer_phone=from_phone,
                        direction="outbound",
                        body="Interactive category message sent.",
                        raw_payload={"source": "message_service", "send_result": send_result},
                        category=rules_data['category'],
                        item_type=rules_data['type'],
                        colors=rules_data['colors'],
                        limit=3
                    )
                    return {"status": "success", "tenant_id": tenant_id, "reply": "Interactive category message sent."}
                else:
                    reply = "We offer a variety of clothing items! What are you looking for?"
                    if items:
                        for item in items[:3]:
                            items_to_send.append({
                                "text": format_single_item_for_whatsapp(item),
                                "image": item.get("media", [None])[0]
                            })
                    inventory_summary = format_manual_inventory_for_ai(items)

                extraction = {"action": "inquiry", "item": rules_data['category'], "details": "Extracted via rules"}
            
            # If not general inquiry, proceed with AI response generation (now with tool calling)
            if not reply: # If reply wasn't set by general_inquiry handler
                ai_reply_text, items_from_tool = await generate_ai_response(
                    extraction,
            # Step 3: Fallback to AI Extraction if Rules yielded nothing
            else:
                extraction = await ai_extract_info(
                    incoming_text,
                    tenant_id=tenant_id,
                    channel_id=channel_id,
                    client_config=client,
                    interaction_id=interaction_id,
                )
                reply = ai_reply_text
                items_to_send = items_from_tool # Populate items_to_send from AI service result
                item_query = extraction.get("item")
                if item_query:
                    if inventory_source == "shopify" and ENABLE_SHOPIFY_INTEGRATION:
                        products = await fetch_clothing_inventory(
                            shop_url=s_url, access_token=s_token, api_version=s_ver, 
                            query=item_query
                        )
                        if products:
                            for p in products[:3]:
                                img_data = p.get("image") or (p.get("images", [{}])[0] if p.get("images") else None)
                                items_to_send.append({
                                    "text": format_single_product_for_whatsapp(p, shop_url=s_url),
                                    "image": img_data.get("src") if img_data else None
                                })
                            
                        inventory_summary = format_products_for_ai(products, shop_url=s_url)
                    elif inventory_source == "manual":
                        items = await search_tenant_inventory(
                            tenant_id=tenant_id,
                            query=item_query,
                            limit=3
                        )
                        if items:
                            for item in items[:3]:
                                items_to_send.append({
                                    "text": format_single_item_for_whatsapp(item),
                                    "image": item.get("media", [None])[0]
                                })
                        inventory_summary = format_manual_inventory_for_ai(items)

            reply = await generate_ai_response(
                extraction,
                incoming_text,
                tenant_id=tenant_id,
                channel_id=channel_id,
                client_config=client,
                interaction_id=interaction_id,
                inventory=inventory_summary,
            )

        elif ENABLE_AI_FALLBACK:
            reply = await ai_fallback_response(
                incoming_text,
                tenant_id=tenant_id,
                channel_id=channel_id,
                client_config=client,
                interaction_id=interaction_id,
            )
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

    # Send primary text greeting
    send_result = await send_whatsapp_message(phone_id, access_token, from_phone, reply)
    
    # Send product cards as separate image messages
    for item_data in items_to_send:
        await send_whatsapp_message(phone_id, access_token, from_phone, item_data["text"], image_url=item_data["image"])

    if send_result.get("status") == "error":
        return {
            "status": "error",
            "tenant_id": tenant_id,
            "reason": "whatsapp_send_failed",
            "detail": send_result.get("detail"),
        }

    try:
        meta_usage = await record_meta_conversation_usage(client, tenant_id, channel_id, from_phone, send_result)
    except Exception:
        logger.exception("Meta conversation usage tracking failed for tenant_id=%s", tenant_id)
        meta_usage = {"status": "error"}

    await save_message_record(
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
        "meta_usage": meta_usage,
    }
