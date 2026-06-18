import json
import asyncio
import logging
import os
import string
from typing import Any, Dict, List, Optional, Tuple
from typing import Any, Dict, Optional
from google import genai
from app.core.config import GEMINI_CLIENT as client, GEMINI_API_KEY

from app.services.pricing_service import extract_token_usage, record_ai_model_usage
from app.services.inventory_service import search_tenant_inventory, format_manual_inventory_for_ai
from app.services.shopify_service import fetch_clothing_inventory, format_products_for_ai, clean_shopify_url

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

# --- DEFAULT PROMPT TEMPLATES ---
class SafeFormatter(string.Formatter):
    """Formatter that ignores missing keys to prevent KeyError."""
    def get_value(self, key, args, kwargs):
        if isinstance(key, str):
            return kwargs.get(key, f"{{{key}}}")
        return super().get_value(key, args, kwargs)

DEFAULT_EXTRACTION_PROMPT = """{persona}
Given a message, return a JSON object with the following fields:
- action: the main action the customer wants (e.g., refund, track_order, complaint, inquiry, general_inquiry)
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

# --- Gemini Tool Definitions ---
inventory_tool_schema = genai.protos.Tool(
    function_declarations=[
        genai.protos.FunctionDeclaration(
            name="search_inventory",
            description="Search for clothing items in the inventory based on various criteria.",
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "query": genai.protos.Schema(type=genai.protos.Type.STRING, description="A general search query for the item, e.g., 'blue shirt' or 'summer dress'."),
                    "category": genai.protos.Schema(type=genai.protos.Type.STRING, description="The category of the clothing item, e.g., 'shirt', 'jeans', 'dress'."),
                    "item_type": genai.protos.Schema(type=genai.protos.Type.STRING, description="The type of the clothing item, e.g., 'formal', 'casual', 'party'."),
                    "colors": genai.protos.Schema(type=genai.protos.Type.ARRAY, items=genai.protos.Schema(type=genai.protos.Type.STRING), description="A list of desired colors, e.g., ['red', 'blue']."),
                    "max_price": genai.protos.Schema(type=genai.protos.Type.NUMBER, description="Maximum price for the item."),
                    "limit": genai.protos.Schema(type=genai.protos.Type.INTEGER, description="Maximum number of results to return (default 5).")
                },
                required=["query"] # Query is often the primary driver, but category/colors can be inferred.
            ),
        )
    ]
)

async def _execute_inventory_tool(
    tool_call: Any,
    tenant_id: Optional[str],
    channel_id: Optional[str],
    client_config: Optional[Dict[str, Any]],
    interaction_id: Optional[str],
) -> Tuple[str, List[Dict[str, Any]]]:
    """Executes the search_inventory tool and returns formatted results and raw items."""
    tool_name = tool_call.function.name
    tool_args = {k: v for k, v in tool_call.function.args.items()}
    
    logger.info(f"Executing tool: {tool_name} with args: {tool_args}")

    if tool_name == "search_inventory":
        inventory_source = client_config.get("inventory_source", "manual") if client_config else "manual"
        
        # Extract Shopify Config from DB Client object
        s_url = clean_shopify_url(client_config.get("shopify_url") or client_config.get("shopify_store_url") or "") if client_config else ""
        s_token = client_config.get("shopify_access_token", "") if client_config else ""
        s_ver = client_config.get("shopify_api_version", "2024-01") if client_config else "2024-01"

        products: List[Dict[str, Any]] = []
        
        if inventory_source == "shopify":
            products = await fetch_clothing_inventory(
                shop_url=s_url, access_token=s_token, api_version=s_ver,
                query=tool_args.get("query"),
                category=tool_args.get("category"),
                max_price=tool_args.get("max_price"),
            )
            formatted_output = format_products_for_ai(products, shop_url=s_url)
        else: # manual inventory
            products = await search_tenant_inventory(
                tenant_id=tenant_id,
                query=tool_args.get("query"),
                category=tool_args.get("category"),
                item_type=tool_args.get("item_type"),
                colors=tool_args.get("colors"),
                limit=tool_args.get("limit", 5)
            )
            formatted_output = format_manual_inventory_for_ai(products)
        
        return formatted_output, products[:3] # Return formatted string and top 3 raw items
    
    return "Tool not found or not implemented.", []

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
) -> Tuple[str, List[Dict[str, Any]]]: # Returns AI response text and list of items
    inventory: Optional[str] = None,
) -> str:
    if not client:
        logger.error("AI client (Gemini) is not initialized. Check GEMINI_API_KEY environment variable. Returning generic response fallback.")
        return "I understood your request. A team member will follow up shortly.", []
        return "I understood your request. A team member will follow up shortly."
        
    # inventory_context = f"\nAVAILABLE INVENTORY:\n{inventory}" if inventory else "" # Removed
    inventory_context = f"\nAVAILABLE INVENTORY:\n{inventory}" if inventory else ""

    config = client_config or {}
    persona = config.get("ai_system_prompt") or "You are an AI Stylist and Concierge. You can search inventory using the provided tools."
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
            inventory_context="" # Initially empty, will be filled by tool output if called
            inventory_context=inventory_context
        )
        
        # Initial parts for the model, including the tool definition
        model_parts = [
            genai.protos.Part(text=prompt),
        ]
        
        # Add tools to the model call
        tools_to_use = [inventory_tool_schema]

        # First call to the model, potentially triggering a tool call
        # Run synchronous AI call in a separate thread
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=GEMINI_MODEL, contents=model_parts, tools=tools_to_use
            model=GEMINI_MODEL, contents=prompt
        )
        await _record_usage(response, tenant_id, channel_id, "generate_response_initial", client_config, interaction_id)

        # Check if the model wants to call a tool
        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.function_call:
                    logger.info(f"Model requested tool call: {part.function_call.function.name}")
                    tool_output_str, raw_items = await _execute_inventory_tool(
                        part.function_call, tenant_id, channel_id, client_config, interaction_id
                    )
                    
                    # Add the tool output to the conversation history and call the model again
                    model_parts.append(part) # Add the function call part
                    model_parts.append(genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name=part.function_call.function.name,
                            response={"result": tool_output_str}
                        )
                    ))
                    
                    # Second call to the model with tool output
                    final_response = await asyncio.to_thread(
                        client.models.generate_content,
                        model=GEMINI_MODEL, contents=model_parts, tools=tools_to_use
                    )
                    await _record_usage(final_response, tenant_id, channel_id, "generate_response_tool_followup", client_config, interaction_id)
                    return _response_text(final_response), raw_items
        
        # If no tool call, or if the tool call didn't return items, just return the initial response
        return _response_text(response), []
        await _record_usage(response, tenant_id, channel_id, "generate_response", client_config, interaction_id)
        return _response_text(response)
    except Exception:
        logger.exception("Gemini response generation error for tenant_id=%s", tenant_id)
        return "I understood your request. A team member will follow up shortly.", []
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
