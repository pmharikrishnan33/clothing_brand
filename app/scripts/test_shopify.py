import asyncio
import argparse
import logging
import sys
from app.core.client_manager import get_client_config
from app.services.shopify_service import fetch_clothing_inventory, format_products_for_ai
from app.core.config import clean_shopify_url, http_client

# Configure production-style logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger("shopify_tester")

async def test_fetch(phone_id: str, query: str):
    """Diagnostic tool to verify Shopify API connectivity for a specific tenant."""
    logger.info(f"Initiating diagnostic for Phone ID: {phone_id}")
    
    try:
        # Fetch config from MongoDB
        client = await get_client_config(phone_id)
        if not client:
            logger.error(f"Client lookup failed for phone_number_id: {phone_id}")
            return

        s_url = clean_shopify_url(client.get("shopify_url") or client.get("shopify_store_url") or "")
        s_token = client.get("shopify_access_token", "")
        s_ver = client.get("shopify_api_version", "2024-01")

        if not s_url or not s_token:
            logger.error("Incomplete Shopify credentials found in database.")
            return

        logger.info(f"Targeting Storefront: {s_url}")
        
        products = await fetch_clothing_inventory(
            shop_url=s_url, 
            access_token=s_token, 
            api_version=s_ver, 
            query=query
        )
        
        if not products:
            logger.warning("No products returned. Check query terms or Shopify permissions.")
        else:
            logger.info(f"Successfully retrieved {len(products)} products.")
            summary = format_products_for_ai(products, shop_url=s_url)
            print("\n" + "="*60 + "\nWHATSAPP CATALOGUE PREVIEW:\n" + "="*60)
            print(summary)
            print("="*60 + "\n")
    finally:
        # Ensure HTTP connections are released
        await http_client.aclose()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-tenant Shopify Integration Diagnostic Tool")
    parser.add_argument("--phone_id", required=True, help="The phone_number_id of the client in MongoDB.")
    parser.add_argument("--query", default="shirt", help="Search query (default: 'shirt').")
    
    args = parser.parse_args()
    asyncio.run(test_fetch(args.phone_id, args.query))