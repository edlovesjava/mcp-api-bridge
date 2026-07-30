from __future__ import annotations

import pytest
import yaml

from mcp_api_bridge.config import BridgeConfig, ConfigError, load_config

from .conftest import SEARCH_CONFIG


def test_example_config_is_valid():
    """The shipped example must actually load — it is the onboarding path."""
    raw = yaml.safe_load(open("config/catalog.example.yaml").read())
    config = BridgeConfig.model_validate(raw)
    assert [a.name for a in config.apis] == ["product_catalog", "parts_catalog"]
    assert config.apis[1].search.first_page == 0


def test_api_lookup_by_name(config):
    assert config.api("parts").base_url == "https://parts.test"


def test_unknown_api_lists_the_available_ones(config):
    with pytest.raises(ConfigError, match="unknown catalog 'nope'") as exc:
        config.api("nope")
    assert "products" in str(exc.value)


def test_omitted_api_is_ambiguous_when_several_are_configured(config):
    with pytest.raises(ConfigError, match="pass `api` explicitly"):
        config.api(None)


def test_omitted_api_resolves_when_only_one_is_configured():
    single = dict(SEARCH_CONFIG, apis=[SEARCH_CONFIG["apis"][0]])
    assert BridgeConfig.model_validate(single).api(None).name == "products"


def test_duplicate_api_names_are_rejected():
    dupe = dict(SEARCH_CONFIG, apis=[SEARCH_CONFIG["apis"][0], SEARCH_CONFIG["apis"][0]])
    with pytest.raises(Exception, match="duplicate API names"):
        BridgeConfig.model_validate(dupe)


def test_empty_api_list_is_rejected():
    with pytest.raises(Exception, match="at least one API"):
        BridgeConfig.model_validate({"version": 1, "apis": []})


def test_api_key_auth_requires_exactly_one_placement():
    broken = {
        "version": 1,
        "apis": [
            {
                "name": "x",
                "base_url": "https://x.test",
                "auth": {
                    "type": "api_key",
                    "key_env": "K",
                    "header": "X-Key",
                    "query_param": "key",
                },
                "search": {"path": "/s"},
            }
        ],
    }
    with pytest.raises(Exception, match="exactly one of"):
        BridgeConfig.model_validate(broken)


def test_per_api_overrides_win_over_defaults(config):
    products, parts = config.api("products"), config.api("parts")
    assert config.page_size_for(products) == 10  # from defaults
    assert config.timeout_for(products) == 5.0
    assert config.retries_for(products) == 1

    parts.page_size = 50
    assert config.page_size_for(parts) == 50


def test_bedrock_env_overrides(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-5")
    monkeypatch.setenv("BEDROCK_EFFORT", "medium")
    monkeypatch.setenv("BEDROCK_REGION", "eu-west-1")
    config = BridgeConfig.model_validate(SEARCH_CONFIG)
    assert config.bedrock.model_id == "anthropic.claude-sonnet-5"
    assert config.bedrock.effort == "medium"
    assert config.bedrock.region == "eu-west-1"


def test_missing_config_file_points_at_the_example(tmp_path):
    with pytest.raises(ConfigError, match="catalog.example.yaml"):
        load_config(tmp_path / "absent.yaml")


def test_malformed_yaml_is_reported(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("apis: [\n  - name: x\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


def test_invalid_config_is_reported_with_the_path(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "apis": [{"name": "x"}]}))
    with pytest.raises(ConfigError, match="failed validation"):
        load_config(path)


def test_load_config_reads_the_env_var(tmp_path, monkeypatch):
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(SEARCH_CONFIG))
    monkeypatch.setenv("MCP_API_BRIDGE_CONFIG", str(path))
    assert load_config().api("products").name == "products"
