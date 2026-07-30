from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from mcp.server.mcpserver.exceptions import ToolError

from mcp_api_bridge.bedrock import QueryUnderstanding
from mcp_api_bridge.catalog import CatalogClient
from mcp_api_bridge.server import Bridge, build_server

from .test_bedrock import PLAN, fake_client

SEARCH_BODY = {
    "data": {
        "items": [{"sku": "A1", "name": "Trail Runner", "pricing": {"current": 79.99}}],
        "totalCount": 1,
    }
}


def structured(result) -> dict:
    """Pull the structured payload out of a CallToolResult."""
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    return json.loads(result.content[0].text)


@pytest.fixture
async def bridge(config):
    catalog = CatalogClient(config)
    understanding = QueryUnderstanding(config.bedrock, fake_client(PLAN))
    b = Bridge(config, catalog=catalog, understanding=understanding)
    try:
        yield b
    finally:
        await b.aclose()


@pytest.fixture
def server(bridge):
    return build_server(bridge=bridge)


async def test_all_tools_are_registered(server):
    names = {t.name for t in await server.list_tools()}
    assert names == {
        "list_catalogs",
        "catalog_search",
        "catalog_get_item",
        "understand_query",
        "smart_search",
    }


async def test_tools_document_themselves(server):
    tools = {t.name: t for t in await server.list_tools()}
    # The description is what a client model reads to pick a tool — it must
    # actually say something.
    for name, tool in tools.items():
        assert tool.description and len(tool.description) > 40, name


async def test_list_catalogs_exposes_the_filter_vocabulary(server):
    payload = structured(await server.call_tool("list_catalogs", {}))
    catalogs = {c["name"]: c for c in payload["catalogs"]}
    assert set(catalogs) == {"products", "parts"}

    filters = {f["name"]: f for f in catalogs["products"]["filters"]}
    assert filters["category"]["allowed_values"] == ["footwear", "apparel"]
    assert filters["price_max"]["type"] == "number"
    assert catalogs["products"]["supports_get_item"] is True
    assert catalogs["parts"]["supports_get_item"] is False
    # Two catalogs configured, so there is no implicit default.
    assert payload["default_catalog"] is None


@respx.mock
async def test_catalog_search_returns_normalized_results(server):
    respx.get("https://catalog.test/v2/search").mock(
        return_value=httpx.Response(200, json=SEARCH_BODY)
    )
    payload = structured(
        await server.call_tool(
            "catalog_search",
            {"query": "trail", "api": "products", "filters": {"category": "footwear"}},
        )
    )
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == "A1"
    assert payload["filters_applied"] == {"category": "footwear"}


async def test_ambiguous_api_is_an_actionable_error(server):
    with pytest.raises(ToolError, match="pass `api` explicitly"):
        await server.call_tool("catalog_search", {"query": "trail"})


async def test_unknown_filter_is_an_actionable_error(server):
    with pytest.raises(ToolError, match="no filter named 'colour'"):
        await server.call_tool(
            "catalog_search",
            {"query": "trail", "api": "products", "filters": {"colour": "red"}},
        )


@respx.mock
async def test_catalog_get_item(server):
    respx.get("https://catalog.test/v2/products/A1").mock(
        return_value=httpx.Response(200, json={"data": {"sku": "A1", "name": "Trail Runner"}})
    )
    payload = structured(
        await server.call_tool("catalog_get_item", {"item_id": "A1", "api": "products"})
    )
    assert payload["title"] == "Trail Runner"


async def test_understand_query_returns_the_plan_without_searching(server):
    payload = structured(
        await server.call_tool(
            "understand_query",
            {"query": "running shoes under $80", "api": "products"},
        )
    )
    assert payload["plan"]["keywords"] == "running shoes"
    assert payload["model_id"] == "anthropic.claude-opus-5"
    assert payload["original_query"] == "running shoes under $80"


@respx.mock
async def test_smart_search_applies_the_planned_filters(server):
    route = respx.get("https://catalog.test/v2/search").mock(
        return_value=httpx.Response(200, json=SEARCH_BODY)
    )
    payload = structured(
        await server.call_tool(
            "smart_search", {"query": "running shoes under $80", "api": "products"}
        )
    )

    params = dict(httpx.QueryParams(route.calls.last.request.url.query.decode()))
    # The plan's keywords are searched, not the raw query, and the price
    # constraint became a filter rather than keyword noise.
    assert params["q"] == "running shoes"
    assert params["categoryId"] == "footwear"
    assert params["priceTo"] == "80.0"
    assert params["sort"] == "price_asc"

    assert payload["plan"]["expansions"] == ["trainers", "sneakers"]
    assert payload["results"]["items"][0]["id"] == "A1"


@respx.mock
async def test_smart_search_reports_bedrock_failure_and_points_at_the_fallback(config):
    async with CatalogClient(config) as catalog:
        broken = QueryUnderstanding(config.bedrock, fake_client(PLAN, stop_reason="refusal"))
        server = build_server(bridge=Bridge(config, catalog=catalog, understanding=broken))
        with pytest.raises(ToolError, match="catalog_search still works"):
            await server.call_tool("smart_search", {"query": "something", "api": "products"})


@respx.mock
async def test_catalog_search_works_without_any_bedrock_client(config):
    """The non-Bedrock tools must not require AWS credentials to be resolvable."""
    respx.get("https://catalog.test/v2/search").mock(
        return_value=httpx.Response(200, json=SEARCH_BODY)
    )

    def explode(*_args, **_kwargs):
        raise AssertionError("Bedrock client must not be constructed for catalog_search")

    async with CatalogClient(config) as catalog:
        # No `understanding` passed — the Bridge must not build one eagerly.
        server = build_server(bridge=Bridge(config, catalog=catalog))

        import mcp_api_bridge.server as server_module

        original = server_module.QueryUnderstanding
        server_module.QueryUnderstanding = explode  # type: ignore[assignment]
        try:
            payload = structured(
                await server.call_tool("catalog_search", {"query": "trail", "api": "products"})
            )
        finally:
            server_module.QueryUnderstanding = original  # type: ignore[assignment]

    assert payload["items"][0]["id"] == "A1"


async def test_single_catalog_config_reports_a_default(config):
    single = config.model_copy(update={"apis": [config.api("products")]})
    async with CatalogClient(single) as catalog:
        server = build_server(
            bridge=Bridge(single, catalog=catalog, understanding=SimpleNamespace())
        )
        payload = structured(await server.call_tool("list_catalogs", {}))
        assert payload["default_catalog"] == "products"
