import json
import asyncio
import logging
import os
import string
from typing import Any, Dict, Optional
from google import genai
from app.core.config import GEMINI_CLIENT as client, GEMINI_API_KEY

from app.services.pricing_service import extract_token_usage, record_ai_model_usage

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# --- DEFAULT PROMPT TEMPLATES ---
class SafeFormatter(string.Formatter):
    """Formatter that ignores missing keys to prevent KeyError."""
    def get_value(self, key, args, kwargs):
        if isinstance(key, str):
            return kwargs.get(key, f"{{{key}}}")
        return super().get_value(key, args, kwargs)

DEFAULT_EXTRACTION_PROMPT = """{persona}
Given a message, return a JSON object with the following fields:
- action: the main action the customer wants (e.g., refund, track_order, complaint, inquiry)
- item: the product/item mentioned, if any (e.g., shirt, laptop), else null
- details: a short summary of the request
Only return valid JSON, nothing else.
Message: {message}"""

DEFAULT_RESPONSE_PROMPT = """{persona}
A customer sent this message: {original_message}

Our system extracted the following structured information:
- Action requested: {action}
- Item involved: {item}
- Details: {details}
{inventory_context}

CRITICAL RULES:
- Maximum 30 words
- Maximum 2 sentences
- Be direct and concise
- No formal greetings like Hello! or sign-offs

Write a helpful, warm, and concise response addressing their request.
Return only the plain response text."""

DEFAULT_FALLBACK_PROMPT = """{persona} A customer sent this message: {message}
This message doesn't match any of our predefined topics. Respond warmly if greeting, else explain our scope.
CRITICAL: Max 25 words, no sign-offs."""

logger = logging.getLogger(__name__)

safe_formatter = SafeFormatter()

def _strip_code_fences(text: str) -> str:
    text = text.strip().replace("“", "\"").replace("”", "\"")  # Handle fancy quotes
    if text.startswith("```"):
        text = text.strip("```").strip()
    if text.lower().startswith("json"):
        text = text[4:].strip()
    return text

def _response_text(response: Any) -> str:
    # Extracts the text output directly from Gemini's response object
    if response and hasattr(response, "text"):
        return response.text.strip()
    return ""

# -------------------- GEMINI AI EXTRACTION -------------------- 
async def _record_usage(
    response: Any,
    tenant_id: Optional[str],
    channel_id: Optional[str],
    operation: str,
    client_config: Optional[Dict[str, Any]] = None,
    interaction_id: Optional[str] = None, # <--- Add this
) -> None:
    if not tenant_id or not channel_id:
        return

    try:
        await record_ai_model_usage(
            tenant_id=tenant_id,
            channel_id=channel_id,
            provider="gemini",
            model=GEMINI_MODEL,
            operation=operation,
            token_usage=extract_token_usage(response),
            client_config=client_config,
            interaction_id=interaction_id, # <--- Pass it here
        )
    except Exception:
        logger.exception("AI usage tracking failed for tenant_id=%s operation=%s", tenant_id, operation)

async def ai_extract_info(
    message: str,
    tenant_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    client_config: Optional[Dict[str, Any]] = None,
    interaction_id: Optional[str] = None,
) -> dict:
    if not client:
        logger.error("AI client (Gemini) is not initialized. Check GEMINI_API_KEY environment variable. Returning default extraction data.")
        return {
            "action": "unknown",
            "item": None,
            "details": message,
        }

    config = client_config or {}
    persona = config.get("ai_extraction_prompt") or "You are a helpful assistant that extracts structured information."
    template = config.get("full_extraction_prompt") or DEFAULT_EXTRACTION_PROMPT
    
    try:
        prompt = safe_formatter.format(template, 
            persona=persona,
            message=message
        )
        
        # Run synchronous AI call in a separate thread to prevent blocking
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=GEMINI_MODEL, contents=prompt
        )
        await _record_usage(response, tenant_id, channel_id, "extract_info", client_config, interaction_id)
        raw_text = _strip_code_fences(_response_text(response))
        return json.loads(raw_text)
    except Exception:
        logger.exception("Gemini extraction error for tenant_id=%s", tenant_id)
        return {"action": "unknown", "item": None, "details": message}

# -------------------- GEMINI AI RESPONSE GENERATOR -------------------- 
async def generate_ai_response(
    extraction_data: dict,
    original_message: str,
    tenant_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    client_config: Optional[Dict[str, Any]] = None,
    interaction_id: Optional[str] = None,
    inventory: Optional[str] = None,
) -> str:
    if not client:
        logger.error("AI client (Gemini) is not initialized. Check GEMINI_API_KEY environment variable. Returning generic response fallback.")
        return "I understood your request. A team member will follow up shortly."
        
    inventory_context = f"\nAVAILABLE INVENTORY:\n{inventory}" if inventory else ""

    config = client_config or {}
    persona = config.get("ai_system_prompt") or "You are an AI Stylist and Concierge."
    template = config.get("full_response_prompt") or DEFAULT_RESPONSE_PROMPT
    
    try:
        # Inject all dynamic variables into the template from DB
        prompt = safe_formatter.format(template, 
            persona=persona,
            original_message=original_message,
            action=extraction_data.get('action', 'unknown'),
            item=extraction_data.get('item', 'not specified'),
            details=extraction_data.get('details', 'not provided'),
            inventory_context=inventory_context
        )

        # Run synchronous AI call in a separate thread
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=GEMINI_MODEL, contents=prompt
        )
        await _record_usage(response, tenant_id, channel_id, "generate_response", client_config, interaction_id)
        return _response_text(response)
    except Exception:
        logger.exception("Gemini response generation error for tenant_id=%s", tenant_id)
        return "I understood your request. A team member will follow up shortly."

# -------------------- GEMINI AI FALLBACK FOR UNKNOWN -------------------- 
async def ai_fallback_response(
    message: str,
    tenant_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    client_config: Optional[Dict[str, Any]] = None,
    interaction_id: Optional[str] = None,
) -> str:
    if not client:
        logger.error("AI client (Gemini) is not initialized. Check GEMINI_API_KEY environment variable. Returning generic fallback response.")
        return "I'm not sure how to help with that. Could you rephrase or ask about our services?"
        
    config = client_config or {}
    persona = config.get("ai_fallback_prompt") or "You are an AI Stylist and Concierge."
    template = config.get("full_fallback_prompt") or DEFAULT_FALLBACK_PROMPT
    
    try:
        prompt = safe_formatter.format(template, 
            persona=persona,
            message=message
        )

        # Run synchronous AI call in a separate thread
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=GEMINI_MODEL, contents=prompt
        )
        await _record_usage(response, tenant_id, channel_id, "fallback_response", client_config, interaction_id)
        return _response_text(response)
    except Exception:
        logger.exception("Gemini fallback error for tenant_id=%s", tenant_id)
        return "I'm not sure how to help with that. Could you rephrase or ask about our services?"
