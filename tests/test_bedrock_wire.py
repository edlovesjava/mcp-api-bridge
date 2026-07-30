"""Wire-level check against the real Bedrock SDK.

The other Bedrock tests use a fake client, which is fast but would keep
passing if the SDK renamed a parameter or changed how it serializes one.
This test drives the genuine `AsyncAnthropicBedrockMantle` and intercepts at
the HTTP transport, so it verifies the request we actually put on the wire —
without AWS credentials or network.
"""

from __future__ import annotations

import json

import httpx
import pytest
from anthropic import AsyncAnthropicBedrockMantle

from mcp_api_bridge.bedrock import QueryUnderstanding
from mcp_api_bridge.config import BridgeConfig

PLAN = {
    "keywords": "running shoes",
    "filters": [
        {"name": "category", "values": ["footwear"]},
        {"name": "price_max", "values": ["80"]},
    ],
    "sort": "price_low_to_high",
    "expansions": ["trainers"],
    "intent": "Budget running shoes.",
    "ambiguities": [],
}


@pytest.fixture
def captured() -> dict:
    return {}


@pytest.fixture
def bedrock_client(captured: dict) -> AsyncAnthropicBedrockMantle:
    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "anthropic.claude-opus-5",
                "content": [{"type": "text", "text": json.dumps(PLAN)}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        )

    return AsyncAnthropicBedrockMantle(
        aws_region="us-east-1",
        aws_access_key="AKIAtest",
        aws_secret_key="secret",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_request_shape_on_the_wire(
    config: BridgeConfig, products, bedrock_client, captured: dict
):
    plan = await QueryUnderstanding(config.bedrock, bedrock_client).understand(
        products, "running shoes under $80"
    )

    body = captured["body"]
    assert body["model"] == "anthropic.claude-opus-5"
    assert body["max_tokens"] == 2048
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["messages"] == [{"role": "user", "content": "running shoes under $80"}]

    # The catalog's own filter descriptions reach the model as vocabulary.
    assert "Merchandising category." in body["system"]
    assert "allowed values: footwear, apparel" in body["system"]

    # And a real SDK response round-trips into a usable plan.
    assert plan.keywords == "running shoes"
    assert plan.filter_map() == {"category": "footwear", "price_max": "80"}
    assert plan.sort == "price_low_to_high"


async def test_schema_sent_is_strict(config: BridgeConfig, products, bedrock_client, captured):
    await QueryUnderstanding(config.bedrock, bedrock_client).understand(products, "shoes")
    schema = captured["body"]["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
