"""Declarative config for the catalog APIs the bridge fronts.

Everything the bridge knows about an in-house API — its URL, auth, how to
build a search request, and how to read the response back — lives in a YAML
file. Adding a catalog is a config change, not a code change.

Secrets are never stored here: `auth` blocks name the environment variable to
read at request time.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class ConfigError(RuntimeError):
    """Raised when the config file is missing, malformed, or inconsistent."""


# --- auth ------------------------------------------------------------------


class NoAuth(BaseModel):
    type: Literal["none"] = "none"


class BearerAuth(BaseModel):
    type: Literal["bearer"]
    token_env: str
    header: str = "Authorization"
    scheme: str = "Bearer"


class ApiKeyAuth(BaseModel):
    type: Literal["api_key"]
    key_env: str
    # Send the key as a header (default) or as a query parameter.
    header: str | None = "X-API-Key"
    query_param: str | None = None

    @model_validator(mode="after")
    def _one_placement(self) -> ApiKeyAuth:
        if bool(self.query_param) == bool(self.header):
            raise ValueError("api_key auth needs exactly one of `header` or `query_param`")
        return self


class BasicAuth(BaseModel):
    type: Literal["basic"]
    username_env: str
    password_env: str


AuthConfig = NoAuth | BearerAuth | ApiKeyAuth | BasicAuth


# --- request shaping -------------------------------------------------------


class LookupConfig(BaseModel):
    """Resolve a name to an id from a closed set fetched once at first use.

    For filters keyed by an opaque id over a small, slow-changing set —
    regions, categories, subcategories. The bridge loads the whole table,
    publishes the *names* as the filter's vocabulary, and translates back to
    the id on the way out. The model never sees or invents an id.

    This is the only workable strategy when the upstream lookup endpoint has
    no name search of its own: `GET /v1/regions` filters by IP and lat/long
    only, so matching "Chicago" means holding the list.
    """

    path: str
    method: Literal["GET", "POST"] = "GET"
    params: dict[str, Any] = Field(default_factory=dict)
    items_path: str = "items"
    id_field: str = "id"
    name_field: str = "name"
    # Additional columns to accept as names (e.g. a region's `listName`).
    alias_fields: list[str] = Field(default_factory=list)
    # Refuse to publish a vocabulary larger than this — a set this big is not
    # a closed set, and belongs behind `resolve` instead.
    max_items: int = 2000


class ResolveConfig(BaseModel):
    """Resolve a name to an id by searching another configured catalog.

    For filters keyed by an opaque id over an open set — performers, venues,
    productions — where no table can be preloaded. The model supplies the
    name it read in the query; the bridge searches the sibling catalog and
    substitutes the top hit's id.
    """

    api: str
    # Ambiguity is normal here ("Chicago" is a city, a band, and a musical),
    # so the resolved choice is always reported back to the caller.
    take: int = 1


class FilterConfig(BaseModel):
    """A logical filter, and how it renders into the upstream request.

    `description`, `type`, and `values` are what the Bedrock query-understanding
    step sees — they are the vocabulary it maps natural language onto, so write
    them for a reader who has never seen the upstream API.
    """

    param: str
    type: Literal["string", "number", "integer", "boolean"] = "string"
    description: str = ""
    values: list[str] | None = None
    # How to render a list value: repeat the param, or join with a separator.
    style: Literal["repeat", "csv"] = "repeat"
    # Where the filter goes. `query` covers the overwhelming majority of
    # in-house search APIs; `body` is for POST-based search endpoints.
    location: Literal["query", "body"] = "query"
    # At most one value-resolution strategy, for id-keyed filters.
    lookup: LookupConfig | None = None
    resolve: ResolveConfig | None = None

    @model_validator(mode="after")
    def _one_resolution_strategy(self) -> FilterConfig:
        if self.lookup and self.resolve:
            raise ValueError("a filter may declare `lookup` or `resolve`, not both")
        if (self.lookup or self.resolve) and self.values:
            raise ValueError(
                "`values` is redundant on a lookup/resolve filter — the resolved "
                "names are the vocabulary"
            )
        return self

    @property
    def resolves_names(self) -> bool:
        return self.lookup is not None or self.resolve is not None


class SortConfig(BaseModel):
    name: str
    value: str
    description: str = ""


class ResponseMapping(BaseModel):
    """How to pull normalized items out of an upstream response body.

    Paths are dotted with optional indices: `data.results[0].sku`. A `[*]`
    segment maps over a list.
    """

    items_path: str | None = None
    item_path: str | None = None
    total_path: str | None = None
    # Normalized field -> path in the upstream item.
    fields: dict[str, str] = Field(default_factory=dict)
    # Extra fields surfaced under `attributes` on each result.
    attributes: dict[str, str] = Field(default_factory=dict)


class EndpointConfig(BaseModel):
    method: Literal["GET", "POST"] = "GET"
    path: str
    # Templated query params. Values may reference {query}, {page},
    # {page_size}, {sort}, or {id}. Params rendering to None are dropped.
    query: dict[str, Any] = Field(default_factory=dict)
    body: dict[str, Any] | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    response: ResponseMapping = Field(default_factory=ResponseMapping)


class SearchEndpointConfig(EndpointConfig):
    filters: dict[str, FilterConfig] = Field(default_factory=dict)
    sorts: list[SortConfig] = Field(default_factory=list)
    # Some APIs are 0-indexed, some 1-indexed.
    first_page: int = 1


class ApiConfig(BaseModel):
    name: str
    description: str = ""
    base_url: str
    auth: AuthConfig = Field(default=NoAuth(), discriminator="type")
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float | None = None
    max_retries: int | None = None
    page_size: int | None = None
    search: SearchEndpointConfig
    get_item: EndpointConfig | None = None

    def sort_values(self) -> list[str]:
        return [s.name for s in self.search.sorts]


class Defaults(BaseModel):
    timeout_seconds: float = 20.0
    max_retries: int = 2
    page_size: int = 20


class BedrockConfig(BaseModel):
    """Bedrock settings for the query-understanding step."""

    region: str = Field(default_factory=lambda: os.environ.get("AWS_REGION", "us-east-1"))
    model_id: str = "anthropic.claude-opus-5"
    # Query understanding is a short, latency-sensitive hop in front of the
    # actual search, so it runs at low effort by default. Raise it if your
    # queries are long or the filter vocabulary is large.
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    max_tokens: int = 2048
    # Extra guidance appended to the query-understanding system prompt —
    # domain vocabulary, synonyms, house conventions.
    guidance: str = ""

    @model_validator(mode="after")
    def _env_overrides(self) -> BedrockConfig:
        if model_id := os.environ.get("BEDROCK_MODEL_ID"):
            self.model_id = model_id
        if effort := os.environ.get("BEDROCK_EFFORT"):
            self.effort = effort  # type: ignore[assignment]
        if region := os.environ.get("BEDROCK_REGION"):
            self.region = region
        return self


class BridgeConfig(BaseModel):
    version: int = 1
    defaults: Defaults = Field(default_factory=Defaults)
    bedrock: BedrockConfig = Field(default_factory=BedrockConfig)
    apis: list[ApiConfig]

    @model_validator(mode="after")
    def _unique_names(self) -> BridgeConfig:
        if not self.apis:
            raise ValueError("config must define at least one API under `apis`")
        names = [a.name for a in self.apis]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate API names: {', '.join(sorted(dupes))}")
        return self

    def api(self, name: str | None) -> ApiConfig:
        """Resolve an API by name, defaulting to the only one if unambiguous."""
        if name is None:
            if len(self.apis) == 1:
                return self.apis[0]
            raise ConfigError(
                "multiple catalogs are configured; pass `api` explicitly. "
                f"Available: {', '.join(a.name for a in self.apis)}"
            )
        for a in self.apis:
            if a.name == name:
                return a
        raise ConfigError(
            f"unknown catalog {name!r}. Available: {', '.join(a.name for a in self.apis)}"
        )

    def timeout_for(self, api: ApiConfig) -> float:
        return api.timeout_seconds or self.defaults.timeout_seconds

    def retries_for(self, api: ApiConfig) -> int:
        return api.max_retries if api.max_retries is not None else self.defaults.max_retries

    def page_size_for(self, api: ApiConfig) -> int:
        return api.page_size or self.defaults.page_size


DEFAULT_CONFIG_PATH = Path("config/catalog.yaml")


def load_config(path: str | Path | None = None) -> BridgeConfig:
    """Load and validate the bridge config.

    Resolution order: explicit `path`, then $MCP_API_BRIDGE_CONFIG, then
    ./config/catalog.yaml.
    """
    resolved = Path(path or os.environ.get("MCP_API_BRIDGE_CONFIG") or DEFAULT_CONFIG_PATH)
    if not resolved.is_file():
        raise ConfigError(
            f"config file not found: {resolved}. Copy config/catalog.example.yaml "
            "and point $MCP_API_BRIDGE_CONFIG at it."
        )
    try:
        raw = yaml.safe_load(resolved.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{resolved} is not valid YAML: {exc}") from exc
    try:
        return BridgeConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"{resolved} failed validation: {exc}") from exc
