import os
import logging
from dotenv import load_dotenv
import httpx
from typing import Optional, Any

logger = logging.getLogger(__name__)
GEMINI_CLIENT: Optional[Any] = None

# Load environment variables from the .env file
load_dotenv()

# Database
MONGO_URI: str = os.getenv("MONGO_URI", "")
MONGO_DB_NAME: str = os.getenv("MONGO_DB_NAME", "zyphor_technologies")

# Webhook & Meta
VERIFY_TOKEN: str = os.getenv("VERIFY_TOKEN", "")
WHATSAPP_TOKEN: str = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID: str = os.getenv("PHONE_NUMBER_ID", "")
APP_SECRET: str = os.getenv("APP_SECRET", "")

# Gemini
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

def clean_shopify_url(url: str) -> str:
    if not url:
        return ""
    if "admin.shopify.com/store/" in url:
        return url.split("admin.shopify.com/store/")[-1].rstrip("/") + ".myshopify.com"
    return url.replace("https://", "").replace("http://", "").rstrip("/")

# Global HTTP client for connection pooling
http_client = httpx.AsyncClient(timeout=20.0)

if GEMINI_API_KEY:
    try:
        from google import genai

        GEMINI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        logger.error(f"Failed to initialize Gemini Client: {e}")
        GEMINI_CLIENT = None
