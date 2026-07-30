"""MCP server exposing the catalog bridge.

Five tools, in the order a caller would discover them:

* `list_catalogs`     — what catalogs exist and what vocabulary they accept
* `catalog_search`    — search with explicit filters (no model in the loop)
* `catalog_get_item`  — fetch one item by id
* `understand_query`  — Bedrock: natural language -> structured plan
* `smart_search`      — understand_query, then catalog_search

Only the last two touch Bedrock. If it is unreachable the first three keep
working, which matters because the catalog is the system of record and the
query understanding is an accelerant.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from .bedrock import QueryUnderstanding, QueryUnderstandingError
from .catalog import CatalogClient, CatalogError
from .config import ApiConfig, BridgeConfig, ConfigError, load_config
from .models import CatalogItem, QueryPlan, SearchResult, UnderstoodQuery

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Search in-house product catalogs.

Call `list_catalogs` first — it returns each catalog's filter names, allowed \
values, and sorts. With that vocabulary in hand, `catalog_search` is the \
direct path: you translate the user's request into filters yourself and skip \
a model round-trip.

Use `smart_search` when you are passing through a raw end-user string you \
have not interpreted, or when the query is long and compound. It runs the \
same search, but has Bedrock plan the filters first and returns that plan \
alongside the results.
"""


# --- tool response shapes --------------------------------------------------


class FilterInfo(BaseModel):
    name: str
    type: str
    description: str
    allowed_values: list[str] | None = None
    accepts_name: bool = Field(
        default=False,
        description=(
            "True when this filter is keyed by an opaque id but accepts a human "
            "name, which the bridge resolves for you. Pass the name; never an id "
            "you guessed."
        ),
    )


class SortInfo(BaseModel):
    name: str
    description: str


class CatalogInfo(BaseModel):
    name: str
    description: str
    filters: list[FilterInfo]
    sorts: list[SortInfo]
    default_page_size: int
    supports_get_item: bool


class CatalogList(BaseModel):
    catalogs: list[CatalogInfo]
    default_catalog: str | None = Field(
        default=None,
        description="The catalog used when a tool's `api` argument is omitted.",
    )


class SmartSearchResult(BaseModel):
    plan: QueryPlan
    results: SearchResult
    model_id: str


# --- wiring ----------------------------------------------------------------


class Bridge:
    """Holds the long-lived clients the tools share."""

    def __init__(
        self,
        config: BridgeConfig,
        catalog: CatalogClient | None = None,
        understanding: QueryUnderstanding | None = None,
    ) -> None:
        self.config = config
        self.catalog = catalog or CatalogClient(config)
        self._understanding = understanding

    @property
    def understanding(self) -> QueryUnderstanding:
        """Build the Bedrock client on first use.

        Deferred so that a deployment which only ever calls `catalog_search`
        never needs AWS credentials resolved.
        """
        if self._understanding is None:
            self._understanding = QueryUnderstanding(self.config.bedrock)
        return self._understanding

    def resolve(self, api: str | None) -> ApiConfig:
        try:
            return self.config.api(api)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc

    async def aclose(self) -> None:
        await self.catalog.aclose()


def _describe(
    api: ApiConfig, config: BridgeConfig, values: dict[str, list[str]] | None = None
) -> CatalogInfo:
    values = values or {}
    return CatalogInfo(
        name=api.name,
        description=api.description,
        filters=[
            FilterInfo(
                name=name,
                type=spec.type,
                description=spec.description,
                allowed_values=values.get(name) or spec.values,
                accepts_name=spec.resolves_names,
            )
            for name, spec in api.search.filters.items()
        ],
        sorts=[SortInfo(name=s.name, description=s.description) for s in api.search.sorts],
        default_page_size=config.page_size_for(api),
        supports_get_item=api.get_item is not None,
    )


def build_server(
    config: BridgeConfig | None = None,
    *,
    bridge: Bridge | None = None,
) -> MCPServer:
    """Construct the MCP server. `bridge` is an injection point for tests."""
    if bridge is None:
        bridge = Bridge(config or load_config())
    active = bridge

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await active.aclose()

    server = MCPServer(
        name="mcp-api-bridge",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )

    @server.tool()
    async def list_catalogs() -> CatalogList:
        """List the configured catalogs and the vocabulary each one accepts.

        Returns every catalog's filter names, types, allowed values, and sort
        options. Call this before `catalog_search` so you filter with names the
        catalog actually exposes.

        Filters marked `accepts_name` are keyed by an opaque id upstream but
        take a human name here — pass "Chicago", not a region id. Where the
        value set is closed it is listed in `allowed_values`; where it is open
        (performers, venues) any name is accepted and resolved on the way
        through.
        """
        cfg = active.config
        return CatalogList(
            catalogs=[
                _describe(api, cfg, await active.catalog.vocabulary(api)) for api in cfg.apis
            ],
            default_catalog=cfg.apis[0].name if len(cfg.apis) == 1 else None,
        )

    @server.tool()
    async def catalog_search(
        query: str,
        api: str | None = None,
        filters: dict[str, Any] | None = None,
        sort: str | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> SearchResult:
        """Search a catalog with keywords and explicit structured filters.

        Args:
            query: Keyword terms only. Put constraints in `filters`, not here —
                a phrase like "under $80" left in the query fights the filter.
            api: Which catalog to search. Optional when only one is configured.
            filters: Filter names from `list_catalogs` mapped to values. A list
                value means "any of these". For `accepts_name` filters pass the
                human name — the bridge resolves it to the upstream id and
                reports what it chose in `resolutions`.
            sort: A sort name from `list_catalogs`. Omit for catalog default.
            page: 1-based page number.
            page_size: Results per page. Defaults to the catalog's configured size.
        """
        target = active.resolve(api)
        try:
            return await active.catalog.search(
                target,
                query,
                filters=filters,
                sort=sort,
                page=page,
                page_size=page_size,
            )
        except (CatalogError, ConfigError) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    async def catalog_get_item(item_id: str, api: str | None = None) -> CatalogItem | None:
        """Fetch a single catalog item by its id.

        Args:
            item_id: The catalog's own identifier, as returned in a search
                result's `id` field.
            api: Which catalog to read from. Optional when only one is configured.
        """
        target = active.resolve(api)
        try:
            return await active.catalog.get_item(target, item_id)
        except (CatalogError, ConfigError) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    async def understand_query(query: str, api: str | None = None) -> UnderstoodQuery:
        """Turn a natural-language query into a structured search plan, without searching.

        Uses Bedrock to split a shopper's phrasing into keywords, structured
        filters, a sort, and synonyms — restricted to the vocabulary the target
        catalog actually exposes. Use this when you want to inspect or adjust
        the plan before running it; use `smart_search` to do both at once.

        Args:
            query: The user's request, in their own words.
            api: Which catalog's vocabulary to plan against. Optional when only
                one is configured.
        """
        target = active.resolve(api)
        try:
            values = await active.catalog.vocabulary(target)
            plan = await active.understanding.understand(target, query, values)
        except QueryUnderstandingError as exc:
            raise ToolError(str(exc)) from exc
        return UnderstoodQuery(
            api=target.name,
            original_query=query,
            plan=plan,
            model_id=active.understanding.model_id,
        )

    @server.tool()
    async def smart_search(
        query: str,
        api: str | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> SmartSearchResult:
        """Interpret a natural-language query with Bedrock, then run the search.

        Returns both the plan and the results, so you can see which filters were
        inferred and re-run with `catalog_search` if the interpretation is off.
        If the results are thin, the plan's `expansions` are alternative terms
        worth retrying.

        Args:
            query: The user's request, in their own words — no need to strip
                constraints out first.
            api: Which catalog to search. Optional when only one is configured.
            page: 1-based page number.
            page_size: Results per page. Defaults to the catalog's configured size.
        """
        target = active.resolve(api)
        try:
            values = await active.catalog.vocabulary(target)
            plan = await active.understanding.understand(target, query, values)
        except QueryUnderstandingError as exc:
            raise ToolError(
                f"{exc} (catalog_search still works — pass the query and filters directly)"
            ) from exc

        try:
            results = await active.catalog.search(
                target,
                plan.keywords or query,
                filters=plan.filter_map(),
                sort=plan.sort,
                page=page,
                page_size=page_size,
            )
        except (CatalogError, ConfigError) as exc:
            raise ToolError(str(exc)) from exc

        return SmartSearchResult(
            plan=plan,
            results=results,
            model_id=active.understanding.model_id,
        )

    return server
