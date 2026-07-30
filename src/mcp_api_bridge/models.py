"""Normalized shapes returned by the bridge's MCP tools.

Upstream catalogs disagree about field names, envelopes, and pagination.
Everything crossing the MCP boundary is flattened into these types so a
caller can work against one shape regardless of which catalog answered.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CatalogItem(BaseModel):
    id: str | None = None
    title: str | None = None
    description: str | None = None
    url: str | None = None
    image: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class SearchResult(BaseModel):
    api: str
    query: str
    items: list[CatalogItem]
    total: int | None = None
    page: int
    page_size: int
    filters_applied: dict[str, Any] = Field(default_factory=dict)
    sort: str | None = None
    resolutions: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "What each name-valued filter resolved to, e.g. "
            '{"performer": {"name": "Taylor Swift", "id": "9134"}}. Ambiguous '
            "names resolve to a best guess — check this before trusting results."
        ),
    )


class FilterSelection(BaseModel):
    """One filter the query implies.

    Values are carried as strings and coerced to the filter's declared type by
    the catalog client, which already owns that knowledge — the model does not
    need to guess whether a price is an int or a float.
    """

    name: str = Field(
        description=("A filter name from the list in the system prompt. Never invent one.")
    )
    values: list[str] = Field(
        description=(
            "One or more values for this filter; several values mean 'any of "
            "these'. Write numbers as bare digits ('80', not '$80' or '80 USD') "
            "and booleans as 'true'/'false'."
        )
    )


class QueryPlan(BaseModel):
    """Structured reading of a natural-language query.

    This is the schema Bedrock is constrained to. Field descriptions are part
    of the prompt the model sees — they are load-bearing, not documentation.
    """

    keywords: str = Field(
        description=(
            "The search terms to send to the catalog's keyword field, with "
            "filter-like phrases removed. For 'red running shoes under $80' "
            "this is 'running shoes' because colour and price become filters. "
            "Never empty; fall back to the user's own words if nothing else."
        )
    )
    filters: list[FilterSelection] = Field(
        default_factory=list,
        description=(
            "The filters this query implies, using only the names listed in "
            "the system prompt, and only where the query clearly calls for "
            "them. Omit anything you are guessing at — a wrong filter silently "
            "hides matching results, while a missing one only widens the search."
        ),
    )
    sort: str | None = Field(
        default=None,
        description=(
            "One of the sort names listed in the system prompt, when the "
            "query expresses an ordering preference ('cheapest', 'newest'). "
            "Null otherwise."
        ),
    )
    expansions: list[str] = Field(
        default_factory=list,
        description=(
            "Up to 4 alternative phrasings or synonyms worth searching if the "
            "primary keywords return too little. Domain terms, not restatements."
        ),
    )
    intent: str = Field(
        default="",
        description=(
            "One short sentence on what the user is actually looking for, for "
            "a human reading the trace. No preamble."
        ),
    )
    ambiguities: list[str] = Field(
        default_factory=list,
        description=(
            "Anything genuinely ambiguous that changed how you read the query. "
            "Empty when the query is clear — do not invent doubt."
        ),
    )

    def filter_map(self) -> dict[str, Any]:
        """Flatten the selections into the `{name: value}` form search takes.

        Single-valued filters unwrap to a scalar so the upstream request looks
        the same whether the filter came from a model or a caller.
        """
        return {
            selection.name: (
                selection.values[0] if len(selection.values) == 1 else selection.values
            )
            for selection in self.filters
            if selection.values
        }


class UnderstoodQuery(BaseModel):
    api: str
    original_query: str
    plan: QueryPlan
    model_id: str
