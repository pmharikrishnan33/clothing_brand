# clothing_brand

## Usage pricing

The backend tracks Meta WhatsApp conversation usage and AI model usage per `tenant_id`.

Environment defaults:

```env
META_CONVERSATION_PRICE_USD=0
META_CONVERSATION_WINDOW_HOURS=24
META_CONVERSATION_CATEGORY=service
GEMINI_INPUT_PRICE_PER_MILLION_USD=0
GEMINI_OUTPUT_PRICE_PER_MILLION_USD=0
DEFAULT_USAGE_CURRENCY=USD
```

Client documents can override these with:

```json
{
  "meta_conversation_price_usd": 0,
  "meta_conversation_window_hours": 24,
  "meta_conversation_category": "service",
  "ai_input_price_per_million_usd": 0,
  "ai_output_price_per_million_usd": 0,
  "ai_model_pricing": {
    "gemini-2.5-flash-lite": {
      "input_per_million_usd": 0,
      "output_per_million_usd": 0
    }
  }
}
```

Usage endpoints:

- `GET /usage/{tenant_id}/summary`
- `GET /usage/{tenant_id}/ai`
- `GET /usage/{tenant_id}/meta`
