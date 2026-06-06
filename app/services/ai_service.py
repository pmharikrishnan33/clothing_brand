import json
import logging
import os
from typing import Any, Dict, Optional
from google import genai
from app.core.config import GEMINI_CLIENT as client, GEMINI_API_KEY

from app.services.pricing_service import extract_token_usage, record_ai_model_usage

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

logger = logging.getLogger(__name__)

def _strip_code_fences(text: str) -> str:
    text = text.strip()
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
def _record_usage(
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
        record_ai_model_usage(
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

def ai_extract_info(
    message: str,
    tenant_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    client_config: Optional[Dict[str, Any]] = None,
    interaction_id: Optional[str] = None,
) -> dict:
    if not client:
        return {
            "action": "unknown",
            "item": None,
            "details": message,
        }
        
    prompt = f"""You are a helpful assistant that extracts structured information from customer messages. 
Given a message, return a JSON object with the following fields:
- action: the main action the customer wants (e.g., refund, track_order, complaint, inquiry)
- item: the product/item mentioned, if any (e.g., shirt, laptop), else null
- details: a short summary of the request
Only return valid JSON, nothing else.
Message: {message}"""
    
    try:
        # Replaced client.responses.create with client.models.generate_content
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        _record_usage(response, tenant_id, channel_id, "extract_info", client_config, interaction_id)
        raw_text = _strip_code_fences(_response_text(response))
        return json.loads(raw_text)
    except Exception:
        logger.exception("Gemini extraction error for tenant_id=%s", tenant_id)
        return {"action": "unknown", "item": None, "details": message}

# -------------------- GEMINI AI RESPONSE GENERATOR -------------------- 
def generate_ai_response(
    extraction_data: dict,
    original_message: str,
    tenant_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    client_config: Optional[Dict[str, Any]] = None,
    interaction_id: Optional[str] = None,
    inventory: Optional[str] = None,
) -> str:
    if not client:
        return "I understood your request. A team member will follow up shortly."
        
    inventory_context = f"\nAVAILABLE INVENTORY:\n{inventory}" if inventory else ""

    prompt = f"""You are an AI Stylist and Concierge. Speak confidently, helper-oriented, and use 'we', 'our', and 'us'.
A customer sent this message: {original_message}

Our system extracted the following structured information:
- Action requested: {extraction_data.get('action', 'unknown')}
- Item involved: {extraction_data.get('item', 'not specified')}
- Details: {extraction_data.get('details', 'not provided')}
{inventory_context}

CRITICAL RULES:
- Maximum 30 words
- Maximum 2 sentences
- Be direct and concise
- No formal greetings like Hello! or sign-offs

Write a helpful, warm, and concise response addressing their request.
If inventory is provided and matches their item, suggest specific pieces.
If the action is 'refund', explain that we are processing it.
If the action is 'track_order', ask for an order number.
If the action is 'unknown', politely ask for clarification.
Return only the plain response text. No code blocks, no JSON."""
    
    try:
        # Replaced client.responses.create with client.models.generate_content
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        _record_usage(response, tenant_id, channel_id, "generate_response", client_config, interaction_id)
        return _response_text(response)
    except Exception:
        logger.exception("Gemini response generation error for tenant_id=%s", tenant_id)
        return "I understood your request. A team member will follow up shortly."

# -------------------- GEMINI AI FALLBACK FOR UNKNOWN -------------------- 
def ai_fallback_response(
    message: str,
    tenant_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    client_config: Optional[Dict[str, Any]] = None,
    interaction_id: Optional[str] = None,
) -> str:
    if not client:
        return "I'm not sure how to help with that. Could you rephrase or ask about our services?"
        
    prompt = f"""You are an AI Stylist and Concierge. A customer sent this message: {message}
This message doesn't match any of our predefined topics.

Your task:
1. If the message is a greeting, respond warmly and ask how you can help.
2. If it's a question we might handle, try to help or suggest they rephrase.
3. If it's completely unrelated, politely explain that we handle automation and style inquiries.

CRITICAL RULES:
- Maximum 25 words
- Maximum 2 sentences
- Be direct and concise
- No formal greetings like Hello! or sign-offs

Return only the plain text response."""
    
    try:
        # Replaced client.responses.create with client.models.generate_content
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        _record_usage(response, tenant_id, channel_id, "fallback_response", client_config, interaction_id)
        return _response_text(response)
    except Exception:
        logger.exception("Gemini fallback error for tenant_id=%s", tenant_id)
        return "I'm not sure how to help with that. Could you rephrase or ask about our services?"
