"""The config-driven REST bridge.

Takes a normalized search request, builds the upstream HTTP call from the
API's config, executes it, and maps the response back into `SearchResult` /
`CatalogItem`. No knowledge of any particular catalog lives here.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .auth import apply_auth
from .config import ApiConfig, BridgeConfig, EndpointConfig, FilterConfig, SearchEndpointConfig
from .models import CatalogItem, SearchResult
from .paths import prune, render, resolve

log = logging.getLogger(__name__)

# Upstream 5xx and connection failures are worth another attempt; 4xx are not.
_RETRY_STATUS = {429, 500, 502, 503, 504}


class CatalogError(RuntimeError):
    """An upstream catalog call failed in a way the caller should hear about."""


class UnknownFilterError(CatalogError):
    def __init__(self, name: str, api: ApiConfig) -> None:
        known = ", ".join(sorted(api.search.filters)) or "(none configured)"
        super().__init__(
            f"catalog {api.name!r} has no filter named {name!r}. Available filters: {known}"
        )


class CatalogClient:
    """Executes catalog requests described by config.

    One client fronts every configured API; the connection pool is shared and
    per-API timeouts are applied per request.
    """

    def __init__(self, config: BridgeConfig, http: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._http = http or httpx.AsyncClient(follow_redirects=True)
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> CatalogClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # --- search ------------------------------------------------------------

    async def search(
        self,
        api: ApiConfig,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        sort: str | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> SearchResult:
        endpoint = api.search
        size = page_size or self._config.page_size_for(api)
        filters = filters or {}

        upstream_page = endpoint.first_page + (max(page, 1) - 1)
        sort_value = self._resolve_sort(endpoint, sort)

        ctx = {
            "query": query,
            "page": upstream_page,
            "page_size": size,
            "sort": sort_value,
        }
        query_params, body_params = self._render_filters(api, filters)

        payload = await self._request(
            api,
            endpoint,
            ctx,
            extra_query=query_params,
            extra_body=body_params,
        )

        mapping = endpoint.response
        raw_items = resolve(payload, mapping.items_path, default=payload)
        if not isinstance(raw_items, list):
            raw_items = [raw_items] if raw_items else []

        total = resolve(payload, mapping.total_path)
        return SearchResult(
            api=api.name,
            query=query,
            items=[self._map_item(item, endpoint) for item in raw_items],
            total=total if isinstance(total, int) else None,
            page=page,
            page_size=size,
            filters_applied=filters,
            sort=sort,
        )

    async def get_item(self, api: ApiConfig, item_id: str) -> CatalogItem | None:
        endpoint = api.get_item
        if endpoint is None:
            raise CatalogError(
                f"catalog {api.name!r} does not configure a `get_item` endpoint; "
                "use catalog_search instead"
            )
        payload = await self._request(api, endpoint, {"id": item_id})
        raw = resolve(payload, endpoint.response.item_path, default=payload)
        if raw is None:
            return None
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        return self._map_item(raw, endpoint) if raw is not None else None

    # --- request construction ---------------------------------------------

    def _resolve_sort(self, endpoint: SearchEndpointConfig, sort: str | None) -> str | None:
        if sort is None:
            return None
        for configured in endpoint.sorts:
            if configured.name == sort:
                return configured.value
        known = ", ".join(s.name for s in endpoint.sorts) or "(none configured)"
        raise CatalogError(f"unknown sort {sort!r}. Available sorts: {known}")

    def _render_filters(
        self, api: ApiConfig, filters: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        query: dict[str, Any] = {}
        body: dict[str, Any] = {}
        for name, value in filters.items():
            if value is None:
                continue
            spec = api.search.filters.get(name)
            if spec is None:
                raise UnknownFilterError(name, api)
            target = query if spec.location == "query" else body
            target[spec.param] = _coerce_filter(name, value, spec)
        return query, body

    async def _request(
        self,
        api: ApiConfig,
        endpoint: EndpointConfig,
        ctx: dict[str, Any],
        *,
        extra_query: dict[str, Any] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> Any:
        auth_headers, auth_query = apply_auth(api.auth, api.name)

        path = render(endpoint.path, ctx)
        if path is None:
            raise CatalogError(
                f"could not build a path for catalog {api.name!r}: "
                f"template {endpoint.path!r} has unfilled placeholders"
            )
        url = api.base_url.rstrip("/") + "/" + str(path).lstrip("/")

        params = prune(
            {**(render(endpoint.query, ctx) or {}), **(extra_query or {}), **auth_query}
        )
        headers = {**api.headers, **endpoint.headers, **auth_headers}

        json_body: dict[str, Any] | None = None
        if endpoint.method == "POST":
            json_body = prune({**(render(endpoint.body, ctx) or {}), **(extra_body or {})})
        elif extra_body:
            # A GET endpoint with body-located filters is a config mistake;
            # say so rather than silently dropping them.
            raise CatalogError(
                f"catalog {api.name!r} configures body filters on a GET endpoint; "
                "set the endpoint method to POST or move the filters to `location: query`"
            )

        timeout = self._config.timeout_for(api)
        attempts = self._config.retries_for(api) + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._http.request(
                    endpoint.method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=timeout,
                )
            except httpx.RequestError as exc:
                last_error = exc
                log.warning(
                    "catalog %s attempt %d/%d failed: %s", api.name, attempt, attempts, exc
                )
                continue

            if response.status_code in _RETRY_STATUS and attempt < attempts:
                log.warning(
                    "catalog %s returned %d, retrying (%d/%d)",
                    api.name,
                    response.status_code,
                    attempt,
                    attempts,
                )
                continue

            if response.is_error:
                raise CatalogError(
                    f"catalog {api.name!r} returned {response.status_code} for {url}: "
                    f"{_truncate(response.text)}"
                )
            try:
                return response.json()
            except ValueError as exc:
                raise CatalogError(
                    f"catalog {api.name!r} returned a non-JSON body from {url}: "
                    f"{_truncate(response.text)}"
                ) from exc

        raise CatalogError(
            f"catalog {api.name!r} unreachable after {attempts} attempts: {last_error}"
        ) from last_error

    # --- response mapping --------------------------------------------------

    def _map_item(self, raw: Any, endpoint: EndpointConfig) -> CatalogItem:
        mapping = endpoint.response
        if not isinstance(raw, dict):
            return CatalogItem(title=str(raw))

        fields: dict[str, Any] = {}
        for name, path in mapping.fields.items():
            value = resolve(raw, path)
            fields[name] = None if value is None else _stringify(value)

        attributes = {
            name: resolve(raw, path)
            for name, path in mapping.attributes.items()
            if resolve(raw, path) is not None
        }

        # With no field mapping configured, pass the upstream item through as
        # attributes rather than returning an empty shell.
        if not mapping.fields and not attributes:
            attributes = raw

        return CatalogItem(
            id=fields.get("id"),
            title=fields.get("title"),
            description=fields.get("description"),
            url=fields.get("url"),
            image=fields.get("image"),
            attributes=attributes,
        )


def _coerce_filter(name: str, value: Any, spec: FilterConfig) -> Any:
    if isinstance(value, list):
        coerced = [_coerce_scalar(name, v, spec) for v in value]
        return ",".join(str(v) for v in coerced) if spec.style == "csv" else coerced
    return _coerce_scalar(name, value, spec)


def _coerce_scalar(name: str, value: Any, spec: FilterConfig) -> Any:
    if spec.values and str(value) not in spec.values:
        raise CatalogError(
            f"filter {name!r} does not accept {value!r}. Allowed values: {', '.join(spec.values)}"
        )
    try:
        if spec.type == "integer":
            return int(value)
        if spec.type == "number":
            return float(value)
        if spec.type == "boolean":
            if isinstance(value, str):
                return value.strip().lower() in {"true", "1", "yes"}
            return bool(value)
    except (TypeError, ValueError) as exc:
        raise CatalogError(f"filter {name!r} expects a {spec.type}, got {value!r}") from exc
    return str(value)


def _stringify(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def _truncate(text: str, limit: int = 400) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"
