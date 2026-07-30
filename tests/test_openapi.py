"""Tests for the OpenAPI importer, driven by the real catalog spec.

`catalog-search-service-api.json` is a genuine in-house spec: OpenAPI 3.1,
49 parameters on one operation, cyclic `$ref`s, HTML in descriptions, and no
declared security scheme. Testing against it rather than a tidy fixture is the
point — every one of those traits broke a first draft of this importer.
"""

from __future__ import annotations

import pytest
import respx
import httpx
import yaml

from mcp_api_bridge.catalog import CatalogClient
from mcp_api_bridge.config import BridgeConfig
from mcp_api_bridge.openapi import (
    OpenAPIError,
    clean_text,
    derive_api,
    find_operation,
    list_operations,
    load_spec,
    render_config,
)

SPEC_PATH = "catalog-search-service-api.json"


@pytest.fixture(scope="module")
def spec() -> dict:
    return load_spec(SPEC_PATH)


class TestSpecLoading:
    def test_reads_openapi_31(self, spec):
        assert spec["openapi"].startswith("3.1")

    def test_lists_every_operation(self, spec):
        ops = {o["operation_id"]: o for o in list_operations(spec)}
        assert set(ops) == {
            "getVenues",
            "search",
            "getSearchSuggestions",
            "searchProductions",
            "searchDistinctProductions",
            "getPerformers",
        }
        assert ops["searchProductions"]["parameters"] == 49

    def test_missing_operation_lists_alternatives(self, spec):
        with pytest.raises(OpenAPIError, match="no operation with operationId 'nope'") as exc:
            find_operation(spec, "nope")
        assert "searchProductions" in str(exc.value)

    def test_swagger_2_is_rejected_with_guidance(self, tmp_path):
        path = tmp_path / "old.yaml"
        path.write_text(yaml.safe_dump({"swagger": "2.0", "paths": {}}))
        with pytest.raises(OpenAPIError, match="Convert it to OpenAPI 3.x"):
            load_spec(path)

    def test_missing_version_field_is_rejected(self, tmp_path):
        path = tmp_path / "notaspec.yaml"
        path.write_text(yaml.safe_dump({"paths": {}}))
        with pytest.raises(OpenAPIError, match="no `openapi` or `swagger`"):
            load_spec(path)


class TestCleanText:
    def test_strips_the_html_this_spec_carries(self, spec):
        raw = next(
            p["description"]
            for p in spec["paths"]["/api/v1/productions"]["get"]["parameters"]
            if p["name"] == "regionId"
        )
        assert "<br/>" in raw and "<b>" in raw
        cleaned = clean_text(raw)
        assert "<" not in cleaned
        assert cleaned.startswith("Only return productions whose venue is located in this region")

    def test_collapses_whitespace_and_unescapes_entities(self):
        assert clean_text("a <b>b</b>&amp;c\n\n  d") == "a b&c d"

    def test_handles_none(self):
        assert clean_text(None) == ""


@pytest.fixture(scope="module")
def derived(spec):
    return derive_api(spec, name="productions", search_operation="searchProductions")


class TestDeriveProductions:
    def test_base_url_and_method(self, derived):
        assert derived.config["base_url"].startswith("https://catalog-search-service")
        assert derived.config["search"]["method"] == "GET"
        assert derived.config["search"]["path"] == "/api/v1/productions"

    def test_role_params_matched_by_name(self, derived):
        assert derived.config["search"]["query"] == {
            "query": "{query}",
            "page": "{page}",
            "pageSize": "{page_size}",
            "sortBy": "{sort}",
        }

    def test_first_page_taken_from_the_schema_default(self, derived):
        assert derived.config["search"]["first_page"] == 1
        assert any("first_page=1 taken from" in n for n in derived.notes)

    def test_page_size_default_imported(self, derived):
        assert derived.config["page_size"] == 25

    def test_role_params_are_not_also_filters(self, derived):
        filters = derived.config["search"]["filters"]
        assert not {"query", "page", "pageSize", "sortBy"} & set(filters)

    def test_filter_descriptions_are_html_free(self, derived):
        for name, spec_entry in derived.config["search"]["filters"].items():
            assert "<" not in spec_entry["description"], name

    def test_enum_becomes_allowed_values(self, derived):
        assert derived.config["search"]["filters"]["homeOrAway"]["values"] == [
            "ALL",
            "HOME",
            "AWAY",
        ]

    def test_array_params_repeat(self, derived):
        assert derived.config["search"]["filters"]["months"]["style"] == "repeat"
        assert derived.config["search"]["filters"]["months"]["type"] == "integer"

    def test_sorts_come_from_the_sort_enum(self, derived):
        names = [s["name"] for s in derived.config["search"]["sorts"]]
        assert names == ["date", "rank", "none", "name", "rankdate"]

    def test_response_envelope_resolved_through_refs(self, derived):
        response = derived.config["search"]["response"]
        assert response["items_path"] == "items"
        assert response["total_path"] == "total"
        assert response["fields"]["id"] == "id"
        assert response["fields"]["title"] == "name"

    def test_cyclic_refs_do_not_hang(self, derived):
        # Production -> Venue -> ... is cyclic; reaching here at all is the assertion.
        assert derived.config["search"]["response"]["attributes"]

    def test_wide_endpoint_is_flagged(self, derived):
        assert len(derived.config["search"]["filters"]) > 40
        assert any("re-run with --include" in g for g in derived.gaps)

    def test_absent_security_scheme_is_reported_not_guessed(self, derived):
        assert derived.config["auth"] == {"type": "none"}
        assert any("declares no securitySchemes" in n for n in derived.notes)

    def test_staging_base_url_is_flagged(self, derived):
        assert any("looks non-production" in g for g in derived.gaps)

    def test_blank_sort_descriptions_are_flagged(self, derived):
        assert any("sort descriptions are blank" in g for g in derived.gaps)


class TestCuration:
    def test_include_restricts_the_vocabulary(self, spec):
        derived = derive_api(
            spec,
            name="productions",
            search_operation="searchProductions",
            include=["startDate", "endDate", "city"],
        )
        assert set(derived.config["search"]["filters"]) == {"startDate", "endDate", "city"}
        assert len(derived.skipped) > 40
        # Curated sets are not nagged about.
        assert not any("re-run with --include" in g for g in derived.gaps)

    def test_exclude_drops_named_params(self, spec):
        derived = derive_api(
            spec,
            name="venues",
            search_operation="getVenues",
            exclude=["wsUserId", "searchAnalyticsUserToken"],
        )
        assert "wsUserId" not in derived.config["search"]["filters"]
        assert "regionId" in derived.config["search"]["filters"]

    def test_attribute_cap_is_respected(self, spec):
        derived = derive_api(
            spec, name="p", search_operation="searchProductions", max_attributes=3
        )
        assert len(derived.config["search"]["response"]["attributes"]) == 3


@pytest.fixture(scope="module")
def generated_config(spec) -> BridgeConfig:
    derivations = [
        derive_api(
            spec,
            name="productions",
            search_operation="searchProductions",
            include=["startDate", "endDate", "city", "months"],
        ),
        derive_api(
            spec,
            name="performers",
            search_operation="getPerformers",
            include=["activeFilter"],
        ),
    ]
    return BridgeConfig.model_validate(yaml.safe_load(render_config(derivations)))


class TestRenderedConfigIsUsable:
    def test_generated_yaml_validates_as_bridge_config(self, generated_config):
        config = generated_config
        assert [a.name for a in config.apis] == ["productions", "performers"]

    def test_provenance_header_is_a_yaml_comment(self, spec):
        rendered = render_config([derive_api(spec, name="v", search_operation="getVenues")])
        assert rendered.startswith("# Generated by `mcp-api-bridge import-openapi`")
        assert yaml.safe_load(rendered)["version"] == 1

    @respx.mock
    async def test_generated_config_drives_a_correct_request(self, generated_config):
        config = generated_config
        route = respx.get(
            "https://catalog-search-service.corp.int.vividseats-staging.com/api/v1/productions"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "page": 1,
                    "total": 1,
                    "items": [
                        {
                            "id": 4242,
                            "name": "Taylor Swift",
                            "organicUrl": "https://vividseats.test/p/4242",
                            "minPrice": 88.0,
                        }
                    ],
                },
            )
        )

        async with CatalogClient(config) as client:
            result = await client.search(
                config.api("productions"),
                "taylor swift",
                filters={"city": "Chicago", "startDate": "2026-08-01"},
                sort="date",
                page=1,
            )

        params = dict(httpx.QueryParams(route.calls.last.request.url.query.decode()))
        assert params["query"] == "taylor swift"
        assert params["page"] == "1"
        assert params["pageSize"] == "25"
        assert params["sortBy"] == "DATE"  # logical name mapped back to the enum value
        assert params["city"] == "Chicago"
        assert params["startDate"] == "2026-08-01"

        assert result.total == 1
        assert result.items[0].id == "4242"
        assert result.items[0].title == "Taylor Swift"
        assert result.items[0].url == "https://vividseats.test/p/4242"


class TestGetItemDerivation:
    def test_path_parameter_is_renamed_to_id(self, spec):
        derived = derive_api(
            spec,
            name="idx",
            search_operation="getPerformers",
            item_operation="search",
        )
        # `search` takes {indexName}; the importer rewrites the single path param to {id}.
        assert derived.config["get_item"]["path"] == "/api/v1/search/index/{id}"
