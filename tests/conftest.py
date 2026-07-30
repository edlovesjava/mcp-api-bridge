from __future__ import annotations

import pytest

from mcp_api_bridge.config import BridgeConfig

SEARCH_CONFIG = {
    "version": 1,
    "defaults": {"page_size": 10, "max_retries": 1, "timeout_seconds": 5},
    "bedrock": {"region": "us-east-1", "model_id": "anthropic.claude-opus-5"},
    "apis": [
        {
            "name": "products",
            "description": "Retail products.",
            "base_url": "https://catalog.test",
            "auth": {"type": "bearer", "token_env": "TEST_CATALOG_TOKEN"},
            "search": {
                "method": "GET",
                "path": "/v2/search",
                "query": {
                    "q": "{query}",
                    "page": "{page}",
                    "size": "{page_size}",
                    "sort": "{sort}",
                    "channel": "web",
                },
                "filters": {
                    "category": {
                        "param": "categoryId",
                        "type": "string",
                        "description": "Merchandising category.",
                        "values": ["footwear", "apparel"],
                    },
                    "price_max": {
                        "param": "priceTo",
                        "type": "number",
                        "description": "Maximum price in USD.",
                    },
                    "brand": {
                        "param": "brand",
                        "type": "string",
                        "description": "Brand name.",
                        "style": "csv",
                    },
                    "in_stock": {
                        "param": "availableOnly",
                        "type": "boolean",
                        "description": "Only in-stock items.",
                    },
                },
                "sorts": [
                    {"name": "relevance", "value": "score_desc", "description": "Best match."},
                    {
                        "name": "price_low_to_high",
                        "value": "price_asc",
                        "description": "Cheapest first.",
                    },
                ],
                "response": {
                    "items_path": "data.items",
                    "total_path": "data.totalCount",
                    "fields": {
                        "id": "sku",
                        "title": "name",
                        "url": "links.pdp",
                        "image": "media[0].url",
                    },
                    "attributes": {"brand": "brand.name", "price": "pricing.current"},
                },
            },
            "get_item": {
                "method": "GET",
                "path": "/v2/products/{id}",
                "response": {
                    "item_path": "data",
                    "fields": {"id": "sku", "title": "name"},
                },
            },
        },
        {
            "name": "parts",
            "description": "Spare parts.",
            "base_url": "https://parts.test",
            "auth": {"type": "api_key", "key_env": "TEST_PARTS_KEY", "header": "X-API-Key"},
            "search": {
                "method": "POST",
                "path": "/search",
                "body": {"text": "{query}", "offset": "{page}", "limit": "{page_size}"},
                "first_page": 0,
                "filters": {
                    "manufacturer": {
                        "param": "manufacturer",
                        "type": "string",
                        "location": "body",
                        "description": "Part manufacturer.",
                    }
                },
                "response": {
                    "items_path": "results",
                    "total_path": "count",
                    "fields": {"id": "partNumber", "title": "description"},
                },
            },
        },
    ],
}


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> BridgeConfig:
    monkeypatch.setenv("TEST_CATALOG_TOKEN", "secret-token")
    monkeypatch.setenv("TEST_PARTS_KEY", "secret-key")
    return BridgeConfig.model_validate(SEARCH_CONFIG)


@pytest.fixture
def products(config: BridgeConfig):
    return config.api("products")


@pytest.fixture
def parts(config: BridgeConfig):
    return config.api("parts")
