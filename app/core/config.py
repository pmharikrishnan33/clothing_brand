import os
from dotenv import load_dotenv

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

if GEMINI_API_KEY:
    try:
        from google import genai

        GEMINI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)
    except Exception:
        GEMINI_CLIENT = None
