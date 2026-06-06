import asyncio
import os
from dotenv import load_dotenv
from app.services.shopify_service import fetch_clothing_inventory, format_products_for_ai

load_dotenv()

async def test_fetch():
    print("--- Testing Shopify Connection ---")
    # Try searching for a common item like 'shirt'
    products = await fetch_clothing_inventory(query="shirt")
    
    if not products:
        print("No products found. Check your SHOPIFY_STORE_URL, SHOPIFY_ACCESS_TOKEN, and ensure you have products with type 'Clothing'.")
    else:
        print(f"Found {len(products)} products.")
        summary = format_products_for_ai(products)
        print("\nFormatted for AI:")
        print(summary)

if __name__ == "__main__":
    asyncio.run(test_fetch())