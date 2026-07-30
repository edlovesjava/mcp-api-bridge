from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mcp_api_bridge.bedrock import (
    QueryUnderstanding,
    QueryUnderstandingError,
    _strict_schema,
    describe_vocabulary,
)
from mcp_api_bridge.models import QueryPlan


class FakeMessages:
    """Stands in for `client.messages`, recording the call and replaying a body."""

    def __init__(self, payload: object, stop_reason: str = "end_turn") -> None:
        self._payload = payload
        self._stop_reason = stop_reason
        self.last_kwargs: dict = {}

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self._payload is None:
            # No text block at all — what a truncated or empty turn looks like.
            return SimpleNamespace(content=[], stop_reason=self._stop_reason)
        text = self._payload if isinstance(self._payload, str) else json.dumps(self._payload)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason=self._stop_reason,
        )


def fake_client(payload: object, stop_reason: str = "end_turn") -> SimpleNamespace:
    """A stand-in Bedrock client. `payload=None` yields a response with no text."""
    return SimpleNamespace(messages=FakeMessages(payload, stop_reason))


PLAN = {
    "keywords": "running shoes",
    "filters": [
        {"name": "category", "values": ["footwear"]},
        {"name": "price_max", "values": ["80"]},
    ],
    "sort": "price_low_to_high",
    "expansions": ["trainers", "sneakers"],
    "intent": "Budget running shoes.",
    "ambiguities": [],
}


class TestVocabulary:
    def test_lists_filters_with_types_and_values(self, products):
        text = describe_vocabulary(products)
        assert "category — string — Merchandising category." in text
        assert "allowed values: footwear, apparel" in text
        assert "price_max — number — Maximum price in USD." in text

    def test_lists_sorts(self, products):
        text = describe_vocabulary(products)
        assert "- price_low_to_high — Cheapest first." in text

    def test_notes_absence_of_sorts(self, parts):
        text = describe_vocabulary(parts)
        assert "no sort options" in text


class TestStrictSchema:
    def test_every_object_is_closed_and_fully_required(self):
        schema = _strict_schema(QueryPlan)

        def check(node):
            if isinstance(node, dict):
                if node.get("type") == "object" and "properties" in node:
                    assert node["additionalProperties"] is False
                    assert set(node["required"]) == set(node["properties"])
                for value in node.values():
                    check(value)
            elif isinstance(node, list):
                for value in node:
                    check(value)

        check(schema)

    def test_nested_definitions_are_tightened(self):
        schema = _strict_schema(QueryPlan)
        selection = schema["$defs"]["FilterSelection"]
        assert selection["additionalProperties"] is False
        assert set(selection["required"]) == {"name", "values"}


class TestUnderstand:
    async def test_returns_a_validated_plan(self, config, products):
        client = fake_client(PLAN)
        plan = await QueryUnderstanding(config.bedrock, client).understand(
            products, "running shoes under $80"
        )
        assert plan.keywords == "running shoes"
        assert plan.sort == "price_low_to_high"
        assert plan.filter_map() == {"category": "footwear", "price_max": "80"}

    async def test_sends_schema_effort_and_vocabulary(self, config, products):
        client = fake_client(PLAN)
        await QueryUnderstanding(config.bedrock, client).understand(products, "shoes")

        kwargs = client.messages.last_kwargs
        assert kwargs["model"] == "anthropic.claude-opus-5"
        assert kwargs["output_config"]["effort"] == "low"
        assert kwargs["output_config"]["format"]["type"] == "json_schema"
        assert "Merchandising category." in kwargs["system"]
        assert kwargs["messages"] == [{"role": "user", "content": "shoes"}]

    async def test_guidance_is_appended_to_the_prompt(self, config, products):
        config.bedrock.guidance = "House rule: 'kicks' means footwear."
        client = fake_client(PLAN)
        await QueryUnderstanding(config.bedrock, client).understand(products, "kicks")
        assert "House rule: 'kicks' means footwear." in client.messages.last_kwargs["system"]

    async def test_multi_value_filter_stays_a_list(self, config, products):
        payload = dict(PLAN, filters=[{"name": "brand", "values": ["Acme", "Globex"]}])
        plan = await QueryUnderstanding(config.bedrock, fake_client(payload)).understand(
            products, "acme or globex"
        )
        assert plan.filter_map() == {"brand": ["Acme", "Globex"]}

    async def test_hallucinated_filter_is_dropped_not_raised(self, config, products):
        payload = dict(
            PLAN,
            filters=[
                {"name": "category", "values": ["footwear"]},
                {"name": "waterproof", "values": ["true"]},
            ],
        )
        plan = await QueryUnderstanding(config.bedrock, fake_client(payload)).understand(
            products, "waterproof shoes"
        )
        assert plan.filter_map() == {"category": "footwear"}

    async def test_hallucinated_sort_is_dropped(self, config, products):
        payload = dict(PLAN, sort="most_popular")
        plan = await QueryUnderstanding(config.bedrock, fake_client(payload)).understand(
            products, "popular shoes"
        )
        assert plan.sort is None

    async def test_refusal_is_reported_with_a_fallback_suggestion(self, config, products):
        client = fake_client(PLAN, stop_reason="refusal")
        with pytest.raises(QueryUnderstandingError, match="catalog_search"):
            await QueryUnderstanding(config.bedrock, client).understand(products, "x")

    async def test_empty_response_is_reported(self, config, products):
        client = fake_client(None, stop_reason="max_tokens")
        with pytest.raises(QueryUnderstandingError, match="max_tokens"):
            await QueryUnderstanding(config.bedrock, client).understand(products, "x")

    async def test_unparseable_plan_is_reported(self, config, products):
        client = fake_client("not json at all")
        with pytest.raises(QueryUnderstandingError, match="unusable plan"):
            await QueryUnderstanding(config.bedrock, client).understand(products, "x")
