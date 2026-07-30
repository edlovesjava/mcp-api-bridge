from __future__ import annotations

from mcp_api_bridge.paths import prune, render, resolve

BODY = {
    "data": {
        "items": [
            {"sku": "A1", "media": [{"url": "https://img/1"}], "links": {"pdp": "/p/a1"}},
            {"sku": "B2", "media": [], "links": {"pdp": "/p/b2"}},
        ],
        "totalCount": 2,
    }
}


class TestResolve:
    def test_dotted_path(self):
        assert resolve(BODY, "data.totalCount") == 2

    def test_index(self):
        assert resolve(BODY, "data.items[0].sku") == "A1"

    def test_nested_index(self):
        assert resolve(BODY, "data.items[0].media[0].url") == "https://img/1"

    def test_wildcard_maps_over_list(self):
        assert resolve(BODY, "data.items[*].sku") == ["A1", "B2"]

    def test_wildcard_skips_missing_keys(self):
        # The second item has no media, so it drops out rather than yielding None.
        assert resolve(BODY, "data.items[*].links.pdp") == ["/p/a1", "/p/b2"]

    def test_missing_key_returns_default(self):
        assert resolve(BODY, "data.nope.deeper", default="fallback") == "fallback"

    def test_index_out_of_range(self):
        assert resolve(BODY, "data.items[9].sku") is None

    def test_empty_path(self):
        assert resolve(BODY, None, default=1) == 1

    def test_scalar_traversal_stops(self):
        assert resolve(BODY, "data.totalCount.nope") is None


class TestRender:
    def test_single_placeholder_preserves_type(self):
        assert render("{page}", {"page": 3}) == 3
        assert isinstance(render("{page}", {"page": 3}), int)

    def test_mixed_template_interpolates(self):
        assert render("/v2/products/{id}", {"id": "A1"}) == "/v2/products/A1"

    def test_missing_placeholder_yields_none(self):
        assert render("{sort}", {"sort": None}) is None
        assert render("/x/{id}", {}) is None

    def test_static_value_passes_through(self):
        assert render("web", {}) == "web"
        assert render(5, {}) == 5

    def test_dict_drops_unfilled_entries(self):
        rendered = render(
            {"q": "{query}", "sort": "{sort}", "channel": "web"},
            {"query": "boots", "sort": None},
        )
        assert rendered == {"q": "boots", "channel": "web"}


def test_prune_drops_empty_values():
    assert prune({"a": 1, "b": None, "c": "", "d": [], "e": False}) == {"a": 1, "e": False}
