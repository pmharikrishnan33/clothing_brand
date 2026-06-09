import logging
import re
from datetime import datetime
from typing import List, Dict, Any, Optional
from app.core.config import http_client, clean_shopify_url

logger = logging.getLogger(__name__)

# --- RULE ENGINE DICTIONARIES ---
COLORS = ["black", "white", "blue", "red", "green", "yellow", "pink", "grey", "gray", "brown"]

CATEGORIES = {
    "shirt": ["shirt", "shirts"],
    "tshirt": ["tshirt", "t-shirt", "tee"],
    "jeans": ["jeans", "denim"],
    "trouser": ["trouser", "pants", "chino"],
    "clothing": ["clothing", "wear", "apparel"]
}

TYPES = {
    "formal": ["formal", "office", "business"],
    "casual": ["casual", "daily"],
    "party": ["party", "wedding", "festive"]
}

async def fetch_shopify_products(
    shop_url: Optional[str] = None,
    access_token: Optional[str] = None,
    api_version: Optional[str] = None,
    keyword: Optional[str] = None, 
    product_type: Optional[str] = None,
    limit: int = 20,
    max_price: Optional[float] = None
) -> List[Dict[str, Any]]:
    """
    Fetches products from the Shopify Admin REST API.

    :param shop_url: The cleaned Shopify store URL.
    :param access_token: The Admin API access token.
    :param api_version: The Shopify API version.
    :param keyword: Optional string to filter products by title.
    :param product_type: Optional string to filter by product type (e.g., 'Clothing').
    :param limit: Number of products to return.
    :param max_price: Optional float to filter results by price.
    :return: A list of product dictionaries.
    """
    store_url = clean_shopify_url(shop_url) if shop_url else None
    token = access_token
    
    # Production Safety: Validate version format YYYY-MM and ensure it's not a future version
    current_version = "2024-01"
    version = api_version if api_version and re.match(r"^\d{4}-\d{2}$", api_version) else current_version
    
    try:
        if int(version.split("-")[0]) > datetime.now().year + 1:
            version = current_version
    except:
        version = current_version

    if not store_url or not token:
        logger.error("Shopify configuration (URL or Token) missing. Multi-tenant credentials are required.")
        return []

    url = f"https://{store_url}/admin/api/{version}/products.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    
    params = {"limit": limit}
    if keyword:
        params["title"] = keyword
    if product_type:
        params["product_type"] = product_type

    try:
        response = await http_client.get(url, headers=headers, params=params)
        response.raise_for_status()
        products = response.json().get("products", [])
        logger.info(f"Successfully fetched {len(products)} products from Shopify.")
        
        # Local Price Filtering
        if max_price is not None:
            filtered = []
            for p in products:
                variants = p.get("variants", [])
                if variants and float(variants[0].get("price", 0)) <= max_price:
                    filtered.append(p)
            return filtered
            
        return products
    except Exception as e:
        if hasattr(e, 'response') and e.response:
            logger.error(f"Shopify API error {e.response.status_code} for URL {url}: {e.response.text}")
        else:
            logger.exception(f"Unexpected error fetching products from Shopify: {str(e)}")
    return []

def extract_rules_info(message: str) -> Dict[str, Any]:
    """Extracts structured data using regex and keyword dictionaries (No AI)."""
    msg = message.lower()
    
    detected_color = next((c for c in COLORS if c in msg), None)
    
    detected_category = None
    for cat, keywords in CATEGORIES.items():
        if any(kw in msg for kw in keywords):
            detected_category = cat
            break
            
    detected_type = None
    for t, keywords in TYPES.items():
        if any(kw in msg for kw in keywords):
            detected_type = t
            break
            
    # Budget detection using regex (e.g., "under 1000", "below 500")
    max_price = None
    price_match = re.search(r'(?:under|below|within)\s+(?:rs\.?|₹|inr)?\s*(\d+)', msg)
    if price_match:
        max_price = float(price_match.group(1))

    return {
        "color": detected_color,
        "category": detected_category,
        "type": detected_type,
        "max_price": max_price,
        "has_data": any([detected_color, detected_category, detected_type, max_price])
    }

async def fetch_clothing_inventory(
    shop_url: Optional[str] = None,
    access_token: Optional[str] = None,
    api_version: Optional[str] = None,
    query: Optional[str] = None, 
    category: Optional[str] = None, 
    max_price: Optional[float] = None
) -> List[Dict[str, Any]]:
    """Specifically fetches products based on extracted rules or queries."""
    return await fetch_shopify_products(
        shop_url=shop_url,
        access_token=access_token,
        api_version=api_version,
        keyword=query,
        product_type=category,
        max_price=max_price
    )

def format_products_for_ai(products: List[Dict[str, Any]], shop_url: Optional[str] = None) -> str:
    """Converts a list of Shopify products into a concise string for AI prompting and WhatsApp catalogue display."""
    if not products:
        return "No matching items found in inventory."
    
    lines = []
    for p in products[:3]:  # Top 3 products for better visibility in WhatsApp
        title = p.get("title", "Unknown Item")
        # Clean HTML from description and resolve common entities
        raw_desc = p.get("body_html", "") or ""
        description = re.sub(r'<[^>]*>', '', raw_desc).replace('&nbsp;', ' ').strip()
        description = (description[:100] + "...") if len(description) > 100 else description
        
        variants = p.get("variants", [{}])
        price = variants[0].get("price", "N/A")

        # Extract Options (Colors and Sizes)
        options = p.get("options", [])
        colors = next((opt.get("values", []) for opt in options if opt.get("name", "").lower() == "color"), [])
        sizes = next((opt.get("values", []) for opt in options if opt.get("name", "").lower() == "size"), [])

        # Construct Link if shop_url and handle are available
        link_str = ""
        if shop_url and p.get("handle"):
            link_str = f"\n🔗 View: https://{shop_url}/products/{p.get('handle')}"

        product_info = f"🛍️ *{title}*\n💰 Price: ₹{price}"
        
        if colors:
            product_info += f"\n🎨 Colors: {', '.join(colors)}"
        if sizes:
            product_info += f"\n📏 Sizes: {', '.join(sizes)}"
        if description:
            product_info += f"\n📝 {description}"
        
        product_info += link_str
        lines.append(product_info)
    return "\n\n".join(lines)