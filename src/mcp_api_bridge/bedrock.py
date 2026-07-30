"""Bedrock-powered query understanding.

Shoppers type "waterproof hiking boots for wide feet under $150". Catalog
search APIs want `q=hiking boots` plus `waterproof=true`, `width=wide`,
`priceTo=150`. This module does that translation with Claude on Bedrock,
constrained to the `QueryPlan` schema so the result is always parseable.

The filter vocabulary in the prompt is generated from the API's own config,
so a catalog gains query understanding the moment its filters are described —
no prompt editing required.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from anthropic import AsyncAnthropicBedrockMantle
from pydantic import ValidationError

from .config import ApiConfig, BedrockConfig
from .models import QueryPlan

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You turn a shopper's natural-language query into a structured search plan for \
a product catalog API. You do not answer the query or describe products — you \
only decide how to search for them.

Rules:
- Use only the filter names and sort names listed below. Never invent one.
- Move a constraint into `filters` only when the query states it plainly. A \
filter you guessed at is worse than no filter, because it silently hides \
matching results.
- Strip anything you turned into a filter out of `keywords`, so the keyword \
search is not fighting the filter.
- `keywords` is never empty. If the query is entirely filters, fall back to \
the head noun the shopper used.
- Keep `expansions` to genuine domain synonyms the catalog might index under. \
Do not pad the list.
"""

_NO_FILTERS = "This catalog exposes no structured filters — put everything in `keywords`."
_NO_SORTS = "This catalog exposes no sort options — always return null for `sort`."


def describe_vocabulary(api: ApiConfig) -> str:
    """Render an API's filters and sorts as prompt context."""
    lines: list[str] = [f"Catalog: {api.name}"]
    if api.description:
        lines.append(f"Purpose: {api.description}")

    lines.append("")
    if api.search.filters:
        lines.append("Available filters (name — type — meaning):")
        for name, spec in api.search.filters.items():
            entry = f"- {name} — {spec.type}"
            if spec.description:
                entry += f" — {spec.description}"
            if spec.values:
                entry += f" — allowed values: {', '.join(spec.values)}"
            lines.append(entry)
    else:
        lines.append(_NO_FILTERS)

    lines.append("")
    if api.search.sorts:
        lines.append("Available sorts:")
        for sort in api.search.sorts:
            entry = f"- {sort.name}"
            if sort.description:
                entry += f" — {sort.description}"
            lines.append(entry)
    else:
        lines.append(_NO_SORTS)

    return "\n".join(lines)


def _strict_schema(model: type[QueryPlan]) -> dict[str, Any]:
    """Produce a JSON Schema the structured-outputs API will accept.

    Pydantic emits objects without `additionalProperties: false`, which
    structured outputs requires on every object, and leaves optional fields
    out of `required`. Fix both, recursively.
    """
    schema = model.model_json_schema()

    def tighten(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                properties = node.get("properties")
                if properties is not None:
                    node["additionalProperties"] = False
                    node["required"] = list(properties)
            for value in node.values():
                tighten(value)
        elif isinstance(node, list):
            for value in node:
                tighten(value)

    tighten(schema)
    return schema


class QueryUnderstanding:
    """Translates natural-language queries into `QueryPlan`s via Bedrock."""

    def __init__(self, config: BedrockConfig, client: Any | None = None) -> None:
        self._config = config
        self._client = client or AsyncAnthropicBedrockMantle(aws_region=config.region)
        self._schema = _strict_schema(QueryPlan)

    @property
    def model_id(self) -> str:
        return self._config.model_id

    def build_system_prompt(self, api: ApiConfig) -> str:
        system = SYSTEM_PROMPT
        if self._config.guidance:
            system += "\n" + self._config.guidance
        return system + "\n\n" + describe_vocabulary(api)

    async def understand(self, api: ApiConfig, query: str) -> QueryPlan:
        """Return a structured plan for `query` against `api`."""
        response = await self._client.messages.create(
            model=self._config.model_id,
            max_tokens=self._config.max_tokens,
            system=self.build_system_prompt(api),
            thinking={"type": "adaptive"},
            output_config={
                "effort": self._config.effort,
                "format": {"type": "json_schema", "schema": self._schema},
            },
            messages=[{"role": "user", "content": query}],
        )

        if response.stop_reason == "refusal":
            raise QueryUnderstandingError(
                "Bedrock declined to plan this query. Search it verbatim with "
                "catalog_search instead."
            )

        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise QueryUnderstandingError(
                f"Bedrock returned no plan (stop_reason={response.stop_reason!r}). "
                "Search the query verbatim with catalog_search instead."
            )

        try:
            plan = QueryPlan.model_validate_json(text)
        except (ValidationError, json.JSONDecodeError) as exc:
            raise QueryUnderstandingError(f"Bedrock returned an unusable plan: {exc}") from exc

        return _drop_unknown(plan, api)


class QueryUnderstandingError(RuntimeError):
    """Bedrock could not produce a usable plan for a query."""


def _drop_unknown(plan: QueryPlan, api: ApiConfig) -> QueryPlan:
    """Discard filters and sorts the catalog does not actually expose.

    Structured outputs constrain the shape, not the vocabulary — `filters` is
    an open dict, so a hallucinated key would reach the catalog client and
    raise. Dropping it here degrades to a slightly broader search instead of
    a failed one.
    """
    known_filters = set(api.search.filters)
    kept = [s for s in plan.filters if s.name in known_filters]
    if dropped := {s.name for s in plan.filters} - {s.name for s in kept}:
        log.info("dropping filters not exposed by %s: %s", api.name, ", ".join(sorted(dropped)))

    sort = plan.sort
    if sort is not None and sort not in {s.name for s in api.search.sorts}:
        log.info("dropping sort %r not exposed by %s", sort, api.name)
        sort = None

    return plan.model_copy(update={"filters": kept, "sort": sort})
