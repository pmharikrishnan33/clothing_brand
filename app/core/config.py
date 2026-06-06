import os
from dotenv import load_dotenv
import httpx

GEMINI_CLIENT = None

# Load environment variables from the .env file
load_dotenv()

# Define these as standalone variables so they can be imported directly
MONGO_URI = os.getenv("MONGO_URI")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
APP_SECRET = os.getenv("APP_SECRET")
SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL")

def clean_shopify_url(url: str) -> str:
    if not url:
        return ""
    if "admin.shopify.com/store/" in url:
        return url.split("admin.shopify.com/store/")[-1].rstrip("/") + ".myshopify.com"
    return url.replace("https://", "").replace("http://", "").rstrip("/")

SHOPIFY_STORE_URL = clean_shopify_url(os.getenv("SHOPIFY_STORE_URL", ""))

SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-01")

# Global HTTP client for connection pooling
http_client = httpx.AsyncClient(timeout=20.0)

if GEMINI_API_KEY:
    try:
        from google import genai

        GEMINI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)
    except Exception:
        GEMINI_CLIENT = None
