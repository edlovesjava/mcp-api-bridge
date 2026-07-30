"""Filter value resolution: names in, opaque ids out.

The catalog service keys its best filters by id — `regionId`, `performerId`,
`venueId` — and no model can produce those from a user's words. Two strategies
close that gap, and both are exercised here against the shipped two-service
config:

* `lookup`  — closed sets (regions, categories) fetched once. The only option
              for regions, whose endpoint has no name search at all.
* `resolve` — open sets (performers, venues) matched via a sibling catalog's
              text search.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from mcp_api_bridge.bedrock import describe_vocabulary
from mcp_api_bridge.catalog import CatalogClient, CatalogError
from mcp_api_bridge.config import load_config

CATALOG = "http://catalog-service.corp.int.vividseats-staging.com/api"
SEARCH = "https://catalog-search-service.corp.int.vividseats-staging.com"

REGIONS = {
    "page": 1,
    "total": 3,
    "items": [
        {"id": 5, "name": "Chicago", "listName": "Chicago, IL"},
        {"id": 12, "name": "Boston", "listName": "Boston, MA"},
        {"id": 21, "name": "New York", "listName": "New York, NY"},
    ],
}

CATEGORIES = {
    "page": 1,
    "total": 3,
    "items": [
        {"id": 2, "name": "Concerts"},
        {"id": 3, "name": "Sports"},
        {"id": 4, "name": "Theater"},
    ],
}

PRODUCTIONS = {
    "page": 1,
    "total": 1,
    "items": [
        {
            "id": 777,
            "name": "Taylor Swift | The Eras Tour",
            "localDate": "2026-09-04T19:00:00",
            "organicUrl": "https://vividseats.test/p/777",
            "venue": {"name": "Soldier Field", "city": "Chicago", "state": "IL"},
            "minPrice": 210.0,
            "listingCount": 812,
        }
    ],
}


@pytest.fixture
def config():
    return load_config("config/vividseats.example.yaml")


@pytest.fixture
def productions(config):
    return config.api("productions")


@pytest.fixture
async def client(config):
    async with CatalogClient(config) as c:
        yield c


def mock_reference_tables() -> None:
    respx.get(f"{CATALOG}/v1/regions").mock(return_value=httpx.Response(200, json=REGIONS))
    respx.get(f"{CATALOG}/v1/categories").mock(return_value=httpx.Response(200, json=CATEGORIES))


def mock_performer(name: str = "Taylor Swift", identifier: int = 9134) -> respx.Route:
    return respx.get(f"{SEARCH}/api/v1/performers").mock(
        return_value=httpx.Response(
            200,
            json={"page": 1, "total": 1, "items": [{"id": identifier, "name": name}]},
        )
    )


class TestClosedSetLookup:
    @respx.mock
    async def test_name_becomes_the_upstream_id(self, client, productions):
        mock_reference_tables()
        route = respx.get(f"{CATALOG}/v1/productions").mock(
            return_value=httpx.Response(200, json=PRODUCTIONS)
        )

        result = await client.search(productions, "eras tour", filters={"region": "Chicago"})

        params = dict(httpx.QueryParams(route.calls.last.request.url.query.decode()))
        assert params["regionId"] == "5"
        assert result.resolutions["region"] == {"name": "Chicago", "id": "5"}

    @respx.mock
    async def test_matching_is_case_insensitive(self, client, productions):
        mock_reference_tables()
        respx.get(f"{CATALOG}/v1/productions").mock(
            return_value=httpx.Response(200, json=PRODUCTIONS)
        )
        result = await client.search(productions, "x", filters={"region": "  cHiCaGo "})
        assert result.filters_applied["region"] == "5"

    @respx.mock
    async def test_alias_column_is_accepted(self, client, productions):
        mock_reference_tables()
        respx.get(f"{CATALOG}/v1/productions").mock(
            return_value=httpx.Response(200, json=PRODUCTIONS)
        )
        # `listName` is declared as an alias, so the "Chicago, IL" spelling works.
        result = await client.search(productions, "x", filters={"region": "Chicago, IL"})
        assert result.filters_applied["region"] == "5"

    @respx.mock
    async def test_the_table_is_fetched_once_and_cached(self, client, productions):
        mock_reference_tables()
        respx.get(f"{CATALOG}/v1/productions").mock(
            return_value=httpx.Response(200, json=PRODUCTIONS)
        )
        for _ in range(3):
            await client.search(productions, "x", filters={"region": "Boston"})
        assert respx.routes[0].call_count == 1

    @respx.mock
    async def test_an_id_passes_straight_through(self, client, productions):
        # A caller that already holds the id should not pay for a lookup.
        regions = respx.get(f"{CATALOG}/v1/regions").mock(
            return_value=httpx.Response(200, json=REGIONS)
        )
        route = respx.get(f"{CATALOG}/v1/productions").mock(
            return_value=httpx.Response(200, json=PRODUCTIONS)
        )
        result = await client.search(productions, "x", filters={"region": 5})
        params = dict(httpx.QueryParams(route.calls.last.request.url.query.decode()))
        assert params["regionId"] == "5"
        assert not regions.called
        assert result.filters_applied["region"] == "5"

    @respx.mock
    async def test_unknown_name_suggests_close_matches(self, client, productions):
        mock_reference_tables()
        with pytest.raises(CatalogError, match="no value named 'Chigago'") as exc:
            await client.search(productions, "x", filters={"region": "Chigago"})
        assert "Did you mean: Chicago" in str(exc.value)

    @respx.mock
    async def test_unknown_name_without_a_near_match_lists_the_set(self, client, productions):
        mock_reference_tables()
        with pytest.raises(CatalogError, match="Known values include") as exc:
            await client.search(productions, "x", filters={"region": "Atlantis"})
        assert "Chicago" in str(exc.value)

    @respx.mock
    async def test_a_set_too_large_to_be_closed_is_rejected(self, client, productions):
        respx.get(f"{CATALOG}/v1/regions").mock(
            return_value=httpx.Response(
                200,
                json={"items": [{"id": i, "name": f"r{i}"} for i in range(3000)]},
            )
        )
        with pytest.raises(CatalogError, match="not a closed set"):
            await client.search(productions, "x", filters={"region": "r1"})

    @respx.mock
    async def test_bad_field_mapping_is_reported(self, client, productions):
        respx.get(f"{CATALOG}/v1/regions").mock(
            return_value=httpx.Response(200, json={"items": [{"pk": 1, "label": "Chicago"}]})
        )
        with pytest.raises(CatalogError, match="produced no name/id pairs"):
            await client.search(productions, "x", filters={"region": "Chicago"})


class TestOpenSetResolve:
    @respx.mock
    async def test_performer_name_resolves_via_the_search_catalog(self, client, productions):
        performers = mock_performer()
        route = respx.get(f"{CATALOG}/v1/productions").mock(
            return_value=httpx.Response(200, json=PRODUCTIONS)
        )

        result = await client.search(
            productions, "eras tour", filters={"performer": "Taylor Swift"}
        )

        # The sibling catalog was searched by name...
        resolver_params = dict(httpx.QueryParams(performers.calls.last.request.url.query.decode()))
        assert resolver_params["query"] == "Taylor Swift"
        # ...and the id it returned was sent upstream.
        params = dict(httpx.QueryParams(route.calls.last.request.url.query.decode()))
        assert params["performerId"] == "9134"
        assert result.resolutions["performer"] == {"name": "Taylor Swift", "id": "9134"}

    @respx.mock
    async def test_the_resolved_choice_is_reported_for_ambiguous_names(self, client, productions):
        # "Chicago" is a band, a city, and a musical — the caller needs to see
        # which one was picked.
        mock_performer(name="Chicago (band)", identifier=42)
        respx.get(f"{CATALOG}/v1/productions").mock(
            return_value=httpx.Response(200, json=PRODUCTIONS)
        )
        result = await client.search(productions, "x", filters={"performer": "Chicago"})
        assert result.resolutions["performer"]["name"] == "Chicago (band)"

    @respx.mock
    async def test_no_match_names_the_catalog_to_search_directly(self, client, productions):
        respx.get(f"{SEARCH}/api/v1/performers").mock(
            return_value=httpx.Response(200, json={"items": [], "total": 0})
        )
        with pytest.raises(CatalogError, match="nothing in catalog 'performers' matched"):
            await client.search(productions, "x", filters={"performer": "Nobody At All"})

    @respx.mock
    async def test_lookup_and_resolve_compose_in_one_search(self, client, productions):
        mock_reference_tables()
        mock_performer()
        route = respx.get(f"{CATALOG}/v1/productions").mock(
            return_value=httpx.Response(200, json=PRODUCTIONS)
        )

        result = await client.search(
            productions,
            "eras tour",
            filters={"performer": "Taylor Swift", "region": "Chicago", "category": "Concerts"},
            sort="date",
        )

        params = dict(httpx.QueryParams(route.calls.last.request.url.query.decode()))
        assert params["performerId"] == "9134"
        assert params["regionId"] == "5"
        assert params["categoryId"] == "2"
        assert params["sortBy"] == "DATE"
        assert set(result.resolutions) == {"performer", "region", "category"}
        assert result.items[0].title == "Taylor Swift | The Eras Tour"
        assert result.items[0].attributes["venue"] == "Soldier Field"


class TestVocabulary:
    @respx.mock
    async def test_closed_sets_are_published_as_values(self, client, productions):
        mock_reference_tables()
        vocabulary = await client.vocabulary(productions)
        # Original casing, primary names only — aliases are accepted on input
        # but would only bloat the prompt.
        assert vocabulary["region"] == ["Boston", "Chicago", "New York"]
        assert vocabulary["category"] == ["Concerts", "Sports", "Theater"]
        # Open sets have no enumerable vocabulary.
        assert "performer" not in vocabulary

    @respx.mock
    async def test_prompt_carries_real_names_not_ids(self, client, productions):
        mock_reference_tables()
        prompt = describe_vocabulary(productions, await client.vocabulary(productions))
        assert "Boston, Chicago, New York" in prompt
        assert "Concerts, Sports, Theater" in prompt

    @respx.mock
    async def test_prompt_tells_the_model_not_to_invent_ids_for_open_sets(
        self, client, productions
    ):
        mock_reference_tables()
        prompt = describe_vocabulary(productions, await client.vocabulary(productions))
        performer_line = next(ln for ln in prompt.splitlines() if ln.startswith("- performer"))
        assert "Do not invent an id" in performer_line

    @respx.mock
    async def test_an_unreachable_lookup_degrades_instead_of_failing(self, client, productions):
        respx.get(f"{CATALOG}/v1/regions").mock(return_value=httpx.Response(503))
        respx.get(f"{CATALOG}/v1/categories").mock(
            return_value=httpx.Response(200, json=CATEGORIES)
        )
        vocabulary = await client.vocabulary(productions)
        # Categories still describe themselves; regions are simply absent.
        assert "category" in vocabulary
        assert "region" not in vocabulary


class TestConfigValidation:
    def test_shipped_config_loads(self, config):
        assert [a.name for a in config.apis] == ["productions", "performers", "venues"]
        assert config.api("productions").get_item is not None

    def test_lookup_and_resolve_are_mutually_exclusive(self):
        from pydantic import ValidationError

        from mcp_api_bridge.config import FilterConfig

        with pytest.raises(ValidationError, match="not both"):
            FilterConfig(
                param="x",
                lookup={"path": "/a"},
                resolve={"api": "b"},
            )

    def test_static_values_are_rejected_on_a_resolving_filter(self):
        from pydantic import ValidationError

        from mcp_api_bridge.config import FilterConfig

        with pytest.raises(ValidationError, match="redundant"):
            FilterConfig(param="x", values=["a"], lookup={"path": "/a"})
