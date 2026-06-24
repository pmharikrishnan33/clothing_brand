import json
import asyncio
import logging
import os
import string
from typing import Any, Dict, List, Optional, Tuple
from app.core.config import GEMINI_CLIENT as client, GEMINI_API_KEY

from app.services.pricing_service import extract_token_usage, record_ai_model_usage
from app.services.inventory_service import search_tenant_inventory, format_manual_inventory_for_ai, get_inventory_metadata
from app.services.shopify_service import fetch_clothing_inventory, format_products_for_ai, clean_shopify_url

DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"

def normalize_gemini_model_name(model_name: str) -> str:
    if not model_name:
        return DEFAULT_GEMINI_MODEL

    normalized = model_name.strip().lower().replace("_", "-").replace(" ", "-")
    return normalized

GEMINI_MODEL = normalize_gemini_model_name(os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL))

# --- DEFAULT PROMPT TEMPLATES ---
class SafeFormatter(string.Formatter):
    """Formatter that ignores missing keys to prevent KeyError."""
    def get_value(self, key, args, kwargs):
        if isinstance(key, str):
            return kwargs.get(key, f"{{{key}}}")
        return super().get_value(key, args, kwargs)

DEFAULT_EXTRACTION_PROMPT = """{persona}
Given a message, return a JSON object with the following fields:
- action: the main action the customer wants (e.g., refund, track_order, inquiry, general_inquiry)
- item: the product/item mentioned, if any (e.g., shirt, laptop), else null
- details: a short summary of the request
Only return valid JSON, nothing else.
Message: {message}"""

INVENTORY_TOOL = {
    "function_declarations": [
        {
            "name": "search_inventory",
            "description": "Searches the store inventory for products based on category, color, or price.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "category": {"type": "STRING", "description": "e.g. 'pants', 'shirts'"},
                    "colors": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "e.g. ['red', 'blue']"},
                    "max_price": {"type": "NUMBER"},
                    "query": {"type": "STRING", "description": "General keywords"}
                }
            }
        }
    ]
}

DEFAULT_RESPONSE_PROMPT = """{persona}
A customer sent this message: {original_message}

Our system extracted the following structured information:
- Action requested: {action}
- Item involved: {item}
- Details: {details}
{inventory_context}

Your goal is to respond to the customer.
If you need more information to fulfill a product request (e.g., size, color, specific style), you MUST ask for those details.
If the request is broad (e.g., "what do you sell?"), suggest categories.
If you have enough information to search inventory, use the `search_inventory` tool.

CRITICAL INSTRUCTIONS:
- Respond ONLY with a minified JSON payload. Do NOT generate conversational text.
- The JSON should have an "action" field indicating the next step.
- Possible actions:
    - "ask_details": If more information is needed. Include "question" (string) and "missing_slots" (list of strings, e.g., ["size", "color"]).
    - "search_inventory": If you have enough info to search. The model will call the tool directly.
    - "general_response": For general inquiries or when no specific product action is taken. Include "text" (string).
    - "show_categories": If the user asks broadly what is offered.
- Example for asking details: {{"action": "ask_details", "question": "What size and color are you looking for?", "missing_slots": ["size", "color"]}}
- Example for general response: {{"action": "general_response", "text": "Hello! How can I help you today?"}}
- Example for showing categories: {{"action": "show_categories"}}
"""

DEFAULT_FALLBACK_PROMPT = """{persona} A customer sent this message: {message}
This message doesn't match any of our predefined topics. Respond warmly if greeting, else explain our scope.
CRITICAL: Max 25 words, no sign-offs."""

logger = logging.getLogger(__name__)

safe_formatter = SafeFormatter()

def _strip_code_fences(text: str) -> str:
    text = text.strip().replace("“", "\"").replace("”", "\"")  # Handle fancy quotes
    if text.startswith("```"):
        text = text.strip("`") # Remove all backticks
    # If it's a JSON output, it might start with `json`
    if text.lower().startswith("json"):
        text = text[4:].strip() # Remove 'json' prefix
    return text.strip() # Final strip

def _response_text(response: Any) -> str:
    # Extracts the text output directly from Gemini's response object
    if response and hasattr(response, "text"):
        return response.text.strip()
    return ""

def generate_system_prompt(brand_name: str, audience: str, tone: str) -> str:
    """Generates a structured system persona for the AI based on onboarding data."""
    return (
        f"You are the AI Stylist for {brand_name}. Your target audience is {audience}. "
        f"Your communication tone is {tone}. "
        "Your primary goal is to assist customers with product inquiries. "
        "If you need more information (like size, color, or specific style) to fulfill a product request, you MUST ask for those details. "
        "Respond ONLY with a minified JSON payload. Do NOT generate conversational text unless it's part of a 'general_response' action."
    )

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
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]: # Return structured AI response and items
    if not client:
        logger.error("AI client (Gemini) is not initialized. Check GEMINI_API_KEY environment variable. Returning generic response fallback.")
        return {"action": "general_response", "text": "I understood your request. A team member will follow up shortly."}, []

    config = client_config or {}
    persona = config.get("ai_system_prompt") or "You are a helpful AI Stylist. Your primary goal is to assist customers with product inquiries. If you need more information (like size, color, or specific style) to fulfill a product request, you MUST ask for those details. Respond ONLY with a minified JSON payload. Do NOT generate conversational text unless it's part of a 'general_response' action."
    template = config.get("full_response_prompt") or DEFAULT_RESPONSE_PROMPT
    
    try:
        # Fetch dynamic metadata for the tenant to guide the AI
        inventory_guidance = ""
        if tenant_id:
            metadata = await get_inventory_metadata(tenant_id)
            valid_categories = list(metadata.get("categories", {}).keys())
            valid_types = list(metadata.get("types", {}).keys())
            
            if valid_categories:
                inventory_guidance += f"Supported product categories: {', '.join(valid_categories)}\n"
            if valid_types:
                inventory_guidance += f"Supported styles/types: {', '.join(valid_types)}\n"
            if inventory_guidance:
                inventory_guidance = f"\nAVAILABLE OFFERINGS GUIDANCE:\n{inventory_guidance}"

        # Inject all dynamic variables into the template from DB
        prompt = safe_formatter.format(template, 
            persona=persona,
            original_message=original_message,
            action=extraction_data.get('action', 'unknown'),
            item=extraction_data.get('item', 'not specified'),
            details=extraction_data.get('details', 'not provided'),
            inventory_context=inventory_guidance
        )

        # Agentic Loop: 1. Generate content with tools
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=GEMINI_MODEL, 
            contents=prompt,
            config={"tools": [INVENTORY_TOOL]}
        )
        
        # 2. Check for tool calls
        items_found = []
        first_part = None
        if getattr(response, "candidates", None):
            content = getattr(response.candidates[0], "content", None)
            parts = getattr(content, "parts", None) if content else None
            if parts:
                first_part = parts[0]

        function_call = getattr(first_part, "function_call", None)
        if function_call:
            fc = function_call
            args = fc.args
            
            # Execute actual search based on config
            inv_source = config.get("inventory_source", "manual")
            if inv_source == "shopify":
                items_found = await fetch_clothing_inventory(
                    shop_url=clean_shopify_url(config.get("shopify_url")),
                    access_token=config.get("shopify_access_token"),
                    query=args.get("query"), category=args.get("category"), max_price=args.get("max_price")
                )
                context = format_products_for_ai(items_found)
            else:
                items_found = await search_tenant_inventory(
                    tenant_id,
                    category=args.get("category"),
                    colors=args.get("colors"),
                    query=args.get("query"),
                    max_price=args.get("max_price"),
                )
                context = format_manual_inventory_for_ai(items_found)

            # 3. Final response with tool results
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=GEMINI_MODEL,
                contents=[prompt, response.candidates[0].content, {"function_response": {"name": fc.name, "response": {"result": context}}}]
            )

        await _record_usage(response, tenant_id, channel_id, "generate_response", client_config, interaction_id)
        
        # Parse the AI's JSON decision
        ai_decision = json.loads(_strip_code_fences(_response_text(response)))
        return ai_decision, items_found[:3]

    except Exception:
        logger.exception("Gemini response generation error for tenant_id=%s", tenant_id)
        return {"action": "general_response", "text": "I understood your request. A team member will follow up shortly."}, []

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
