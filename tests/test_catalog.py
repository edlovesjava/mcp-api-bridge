from __future__ import annotations

import httpx
import pytest
import respx

from mcp_api_bridge.catalog import CatalogClient, CatalogError
from mcp_api_bridge.config import BridgeConfig

SEARCH_BODY = {
    "data": {
        "items": [
            {
                "sku": "A1",
                "name": "Trail Runner",
                "links": {"pdp": "https://catalog.test/p/a1"},
                "media": [{"url": "https://img/a1"}],
                "brand": {"name": "Acme"},
                "pricing": {"current": 79.99},
            }
        ],
        "totalCount": 1,
    }
}


@pytest.fixture
async def client(config: BridgeConfig):
    async with CatalogClient(config) as c:
        yield c


@respx.mock
async def test_search_builds_request_and_maps_response(client, products):
    route = respx.get("https://catalog.test/v2/search").mock(
        return_value=httpx.Response(200, json=SEARCH_BODY)
    )

    result = await client.search(
        products,
        "trail runners",
        filters={"category": "footwear", "price_max": 80},
        sort="price_low_to_high",
        page=2,
        page_size=5,
    )

    request = route.calls.last.request
    params = dict(httpx.QueryParams(request.url.query.decode()))
    assert params == {
        "q": "trail runners",
        "page": "2",
        "size": "5",
        "sort": "price_asc",  # mapped from the logical sort name
        "channel": "web",  # static param survives
        "categoryId": "footwear",
        "priceTo": "80.0",  # coerced to the declared number type
    }
    assert request.headers["Authorization"] == "Bearer secret-token"

    assert result.total == 1
    assert result.page == 2
    item = result.items[0]
    assert item.id == "A1"
    assert item.title == "Trail Runner"
    assert item.image == "https://img/a1"
    assert item.attributes == {"brand": "Acme", "price": 79.99}


@respx.mock
async def test_omitted_sort_drops_the_param(client, products):
    route = respx.get("https://catalog.test/v2/search").mock(
        return_value=httpx.Response(200, json=SEARCH_BODY)
    )
    await client.search(products, "boots")
    assert "sort" not in dict(httpx.QueryParams(route.calls.last.request.url.query.decode()))


@respx.mock
async def test_csv_style_filter_joins_values(client, products):
    route = respx.get("https://catalog.test/v2/search").mock(
        return_value=httpx.Response(200, json=SEARCH_BODY)
    )
    await client.search(products, "boots", filters={"brand": ["Acme", "Globex"]})
    params = dict(httpx.QueryParams(route.calls.last.request.url.query.decode()))
    assert params["brand"] == "Acme,Globex"


@respx.mock
async def test_boolean_filter_coercion(client, products):
    route = respx.get("https://catalog.test/v2/search").mock(
        return_value=httpx.Response(200, json=SEARCH_BODY)
    )
    await client.search(products, "boots", filters={"in_stock": "yes"})
    params = dict(httpx.QueryParams(route.calls.last.request.url.query.decode()))
    assert params["availableOnly"] == "true"


@respx.mock
async def test_post_search_sends_body_filters_and_zero_indexed_page(client, parts):
    route = respx.post("https://parts.test/search").mock(
        return_value=httpx.Response(200, json={"results": [{"partNumber": "P1"}], "count": 1})
    )

    result = await client.search(
        parts, "bearing", filters={"manufacturer": "Acme"}, page=1, page_size=25
    )

    import json

    body = json.loads(route.calls.last.request.content)
    assert body == {
        "text": "bearing",
        "offset": 0,  # first_page: 0 — caller's page 1 is upstream page 0
        "limit": 25,
        "manufacturer": "Acme",
    }
    assert route.calls.last.request.headers["X-API-Key"] == "secret-key"
    assert result.items[0].id == "P1"


@respx.mock
async def test_get_item(client, products):
    respx.get("https://catalog.test/v2/products/A1").mock(
        return_value=httpx.Response(200, json={"data": {"sku": "A1", "name": "Trail Runner"}})
    )
    item = await client.get_item(products, "A1")
    assert item is not None
    assert item.id == "A1"
    assert item.title == "Trail Runner"


async def test_get_item_unsupported_is_a_clear_error(client, parts):
    with pytest.raises(CatalogError, match="does not configure a `get_item` endpoint"):
        await client.get_item(parts, "P1")


async def test_unknown_filter_lists_the_valid_ones(client, products):
    with pytest.raises(CatalogError, match="no filter named 'colour'") as exc:
        await client.search(products, "boots", filters={"colour": "red"})
    assert "category" in str(exc.value)


async def test_unknown_sort_lists_the_valid_ones(client, products):
    with pytest.raises(CatalogError, match="unknown sort 'cheapest'") as exc:
        await client.search(products, "boots", sort="cheapest")
    assert "price_low_to_high" in str(exc.value)


async def test_filter_value_outside_enum_is_rejected(client, products):
    with pytest.raises(CatalogError, match="does not accept 'toys'"):
        await client.search(products, "boots", filters={"category": "toys"})


async def test_filter_type_mismatch_is_rejected(client, products):
    with pytest.raises(CatalogError, match="expects a number"):
        await client.search(products, "boots", filters={"price_max": "cheap"})


@respx.mock
async def test_upstream_5xx_is_retried_then_surfaced(client, products):
    route = respx.get("https://catalog.test/v2/search").mock(
        return_value=httpx.Response(503, text="upstream down")
    )
    with pytest.raises(CatalogError, match="returned 503"):
        await client.search(products, "boots")
    # defaults.max_retries is 1, so two attempts total.
    assert route.call_count == 2


@respx.mock
async def test_upstream_4xx_is_not_retried(client, products):
    route = respx.get("https://catalog.test/v2/search").mock(
        return_value=httpx.Response(404, text="no such endpoint")
    )
    with pytest.raises(CatalogError, match="returned 404"):
        await client.search(products, "boots")
    assert route.call_count == 1


@respx.mock
async def test_retry_recovers(client, products):
    route = respx.get("https://catalog.test/v2/search").mock(
        side_effect=[
            httpx.Response(503, text="try again"),
            httpx.Response(200, json=SEARCH_BODY),
        ]
    )
    result = await client.search(products, "boots")
    assert route.call_count == 2
    assert result.items[0].id == "A1"


@respx.mock
async def test_non_json_body_is_a_clear_error(client, products):
    respx.get("https://catalog.test/v2/search").mock(
        return_value=httpx.Response(200, text="<html>maintenance</html>")
    )
    with pytest.raises(CatalogError, match="non-JSON body"):
        await client.search(products, "boots")


@respx.mock
async def test_connection_error_is_surfaced_after_retries(client, products):
    respx.get("https://catalog.test/v2/search").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(CatalogError, match="unreachable after 2 attempts"):
        await client.search(products, "boots")


async def test_missing_credential_names_the_variable(config, products, monkeypatch):
    monkeypatch.delenv("TEST_CATALOG_TOKEN", raising=False)
    async with CatalogClient(config) as c:
        with pytest.raises(Exception, match="TEST_CATALOG_TOKEN"):
            await c.search(products, "boots")
