import json
import os
from typing import Any, Dict

from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return text


def _response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", "")
    if output_text:
        return output_text.strip()

    pieces = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                pieces.append(text)
    return "".join(pieces).strip()

# -------------------- OPENAI AI EXTRACTION --------------------
def ai_extract_info_openai(message: str) -> dict:
    if not client:
        return {
            "action": "unknown",
            "item": None,
            "details": message,
        }

    prompt = f"""
    You are a helpful assistant that extracts structured information from customer messages.
    Given a message, return a JSON object with the following fields:
    - "action": the main action the customer wants (e.g., "refund", "track_order", "complaint", "inquiry")
    - "item": the product/item mentioned, if any (e.g., "shirt", "laptop"), else null
    - "details": a short summary of the request

    Only return valid JSON, nothing else.

    Message: "{message}"
    """

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
            store=True,
        )
        raw_text = _strip_code_fences(_response_text(response))
        return json.loads(raw_text)
    except Exception as e:
        print(f"OpenAI extraction error: {e}")
        return {
            "action": "unknown",
            "item": None,
            "details": message
        }


# -------------------- OPENAI AI RESPONSE GENERATOR --------------------
def generate_response_with_openai(extraction_data: dict, original_message: str) -> str:
    if not client:
        return "I understood your request. A team member will follow up shortly."

    prompt = f"""
    You are an AI Stylist and Concierge. Speak confidently, helper-oriented, and use "we", "our", and "us".
    
    A customer sent this message: "{original_message}"

    Our system extracted the following structured information:
    - Action requested: {extraction_data.get('action', 'unknown')}
    - Item involved: {extraction_data.get('item', 'not specified')}
    - Details: {extraction_data.get('details', 'not provided')}

    Write a helpful, warm, and concise response (2-3 sentences max) addressing their request.
    If the action is "refund", explain the next steps.
    If the action is "track_order", ask for their order number.
    If the action is "unknown", politely ask for clarification.

    Return only the plain response text. No code blocks, no JSON.
    """

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
            store=True,
        )
        return _response_text(response)
    except Exception as e:
        print(f"OpenAI response generation error: {e}")
        return "I understood your request. A team member will follow up shortly."


# -------------------- OPENAI AI FALLBACK FOR UNKNOWN --------------------
def ai_fallback_response_openai(message: str) -> str:
    if not client:
        return "I'm not sure how to help with that. Could you rephrase or ask about our services?"

    prompt = f"""
    You are an AI Stylist and Concierge.
    A customer sent this message: "{message}"
    
    This message doesn't match any of our predefined topics.
    
    Your task:
    1. If the message is a greeting, respond warmly and ask how you can help.
    2. If it's a question we might handle, try to help or suggest they rephrase.
    3. If it's completely unrelated, politely explain that we handle automation and style inquiries.
    
    CRITICAL RULES:
    - Maximum 20 words
    - Maximum 1 sentence
    - Be direct and concise
    - No formal greetings like "Hello!" or sign-offs
    
    Return only the plain text response.
    """

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
            store=True,
        )
        return _response_text(response)
    except Exception as e:
        print(f"OpenAI fallback error: {e}")
        return "I'm not sure how to help with that. Could you rephrase or ask about our services?"
