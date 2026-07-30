"""Derive bridge config from an OpenAPI document.

Hand-transcribing a real search API is not viable — the Vivid Seats catalog
spec declares 49 query parameters on `searchProductions` alone, across three
sibling endpoints. This module reads the spec and emits the same YAML a human
would have written, so the wire contract comes from the API team's own
document and the human is left with only the parts OpenAPI cannot express.

What the spec provides:
    base URL, method, path, parameter names, types, enums, descriptions,
    paging defaults, response envelope shape, auth scheme.

What it cannot, and a human must supply:
    which parameter is the free-text query vs paging vs sort (guessed here by
    name, always reported), which response field is the title vs image, and
    friendly sort names. See `Derivation.notes` / `.gaps` for what each import
    guessed and what it left blank.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Parameter names that play a structural role rather than being a filter.
# Ordered by preference: the first match in a spec wins.
ROLE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "query": ("query", "q", "text", "searchText", "keyword", "keywords", "term"),
    "page": ("page", "pageNumber", "offset", "from"),
    "page_size": ("pageSize", "size", "limit", "perPage", "count"),
    "sort": ("sortBy", "sort", "orderBy", "order"),
}

# Response-field roles, matched against item schema leaves in priority order.
FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "id": ("id", "sku", "identifier", "partNumber", "code"),
    "title": ("name", "title", "displayName", "label"),
    "description": ("description", "longDescription", "summary", "subtitle"),
    "url": ("organicUrl", "webPath", "url", "link", "href", "links.pdp", "detailUrl"),
    "image": ("imageUrl", "image", "staticMapUrl", "thumbnail", "media[0].url"),
}

TOTAL_CANDIDATES = ("total", "totalCount", "count", "totalResults", "numFound")

JSON_TO_FILTER_TYPE = {
    "string": "string",
    "number": "number",
    "integer": "integer",
    "boolean": "boolean",
}


class OpenAPIError(RuntimeError):
    """The spec could not be read, or does not contain what was asked for."""


def clean_text(raw: str | None) -> str:
    """Flatten a spec description into prompt-safe prose.

    In-house specs routinely carry HTML in `description` — 81 of the 150
    parameter descriptions in the Vivid Seats catalog spec do. Those strings
    become the model's filter vocabulary, so the markup has to go before it
    reaches a prompt.
    """
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>|</br>", " ", raw)
    text = re.sub(r"<li\b[^>]*>", " • ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


@dataclass
class Derivation:
    """A config block plus an account of how it was arrived at."""

    config: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def load_spec(path: str | Path) -> dict[str, Any]:
    """Read a JSON or YAML OpenAPI document with `$ref`s resolved."""
    resolved = Path(path)
    if not resolved.is_file():
        raise OpenAPIError(f"spec not found: {resolved}")
    text = resolved.read_text()
    try:
        raw = json.loads(text) if resolved.suffix == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise OpenAPIError(f"{resolved} is not valid JSON/YAML: {exc}") from exc

    version = raw.get("openapi") or raw.get("swagger")
    if not version:
        raise OpenAPIError(f"{resolved} has no `openapi` or `swagger` version field")
    if str(version).startswith("2"):
        raise OpenAPIError(
            f"{resolved} is Swagger {version}. Convert it to OpenAPI 3.x first "
            "(e.g. with swagger2openapi); this importer reads 3.x only."
        )
    return raw


def deref(node: Any, root: dict[str, Any], _depth: int = 0) -> Any:
    """Follow local `$ref` pointers one hop at a time.

    Refs are resolved on demand rather than materialized up front: real specs
    are cyclic (here `Production` → `Venue` → `Production`), so eagerly
    inlining them either recurses forever or needs proxy objects that then
    break serialization. Every traversal below is depth-bounded, so resolving
    lazily is both simpler and safe.
    """
    while isinstance(node, dict) and "$ref" in node and _depth < 20:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return node
        target: Any = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                return node
            target = target[part]
        node = target
        _depth += 1
    return node


def find_operation(spec: dict[str, Any], operation_id: str) -> tuple[str, str, dict[str, Any]]:
    """Locate an operation by `operationId`, returning `(method, path, operation)`."""
    available = []
    for path, raw_item in (spec.get("paths") or {}).items():
        item = deref(raw_item, spec)
        for method, op in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            if op.get("operationId") == operation_id:
                return method.upper(), path, op
            available.append(op.get("operationId") or f"{method.upper()} {path}")
    raise OpenAPIError(
        f"no operation with operationId {operation_id!r}. "
        f"Available: {', '.join(sorted(filter(None, available)))}"
    )


def list_operations(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Summarize every operation, for the `--list` view."""
    out = []
    for path, raw_item in (spec.get("paths") or {}).items():
        item = deref(raw_item, spec)
        for method, op in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            out.append(
                {
                    "operation_id": op.get("operationId"),
                    "method": method.upper(),
                    "path": path,
                    "summary": clean_text(op.get("summary")),
                    "parameters": len(op.get("parameters") or []),
                }
            )
    return out


def _schema_type(schema: dict[str, Any]) -> tuple[str, bool]:
    """Return `(scalar_type, is_array)`, tolerating OpenAPI 3.1 type unions."""
    declared = schema.get("type")
    if isinstance(declared, list):  # 3.1 allows ["string", "null"]
        declared = next((t for t in declared if t != "null"), "string")
    if declared == "array":
        items = schema.get("items") or {}
        inner, _ = _schema_type(items)
        return inner, True
    return declared or "string", False


def _detect_auth(spec: dict[str, Any]) -> tuple[dict[str, Any], str]:
    schemes = ((spec.get("components") or {}).get("securitySchemes") or {}).items()
    for name, scheme in schemes:
        kind, http_scheme = scheme.get("type"), (scheme.get("scheme") or "").lower()
        if kind == "http" and http_scheme == "bearer":
            return {"type": "bearer", "token_env": "CHANGEME_TOKEN"}, (
                f"auth: securityScheme {name!r} is HTTP bearer — set `token_env`"
            )
        if kind == "http" and http_scheme == "basic":
            return {
                "type": "basic",
                "username_env": "CHANGEME_USER",
                "password_env": "CHANGEME_PASSWORD",
            }, f"auth: securityScheme {name!r} is HTTP basic — set the env vars"
        if kind == "apiKey":
            placement = (
                {"header": scheme.get("name", "X-API-Key")}
                if scheme.get("in") == "header"
                else {"query_param": scheme.get("name", "apiKey")}
            )
            return {"type": "api_key", "key_env": "CHANGEME_API_KEY", **placement}, (
                f"auth: securityScheme {name!r} is an apiKey — set `key_env`"
            )
    return {"type": "none"}, (
        "auth: the spec declares no securitySchemes, so `none` was written. "
        "If a gateway or network policy fronts this API, that is invisible here — "
        "check before deploying."
    )


def _find_items_path(
    schema: dict[str, Any], root: dict[str, Any]
) -> tuple[str | None, dict[str, Any] | None]:
    """Locate the result array inside a response envelope, breadth-first."""
    queue: list[tuple[dict[str, Any], str, int]] = [(deref(schema, root), "", 0)]
    while queue:
        node, prefix, depth = queue.pop(0)
        if depth > 4:
            continue
        for key, raw_value in (node.get("properties") or {}).items():
            value = deref(raw_value, root)
            path = f"{prefix}.{key}" if prefix else key
            declared, is_array = _schema_type(value)
            if is_array:
                return path, deref(value.get("items") or {}, root)
            if declared == "object" or value.get("properties"):
                queue.append((value, path, depth + 1))
    return None, None


def _leaf_paths(
    schema: dict[str, Any], root: dict[str, Any], prefix: str = "", depth: int = 0
) -> list[str]:
    """Enumerate scalar leaves of an item schema, as bridge path expressions."""
    if depth > 2:
        return []
    schema = deref(schema, root)
    out: list[str] = []
    for key, raw_value in (schema.get("properties") or {}).items():
        value = deref(raw_value, root)
        path = f"{prefix}.{key}" if prefix else key
        declared, is_array = _schema_type(value)
        if is_array:
            items = deref(value.get("items") or {}, root)
            if items.get("properties"):
                out.extend(_leaf_paths(items, root, f"{path}[0]", depth + 1))
        elif value.get("properties"):
            out.extend(_leaf_paths(value, root, path, depth + 1))
        elif declared in JSON_TO_FILTER_TYPE:
            out.append(path)
    return out


def _match_field_roles(leaves: list[str]) -> dict[str, str]:
    available = set(leaves)
    fields: dict[str, str] = {}
    for role, candidates in FIELD_CANDIDATES.items():
        for candidate in candidates:
            if candidate in available:
                fields[role] = candidate
                break
    return fields


def derive_api(
    spec: dict[str, Any],
    *,
    name: str,
    search_operation: str,
    item_operation: str | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    max_attributes: int = 10,
) -> Derivation:
    """Build one `apis:` entry from a search operation in `spec`.

    `include` is an allowlist of parameter names to expose as filters. On a
    wide endpoint it is the parameter that matters most: the filter set
    becomes the model's vocabulary, and a 45-filter prompt is both expensive
    and a large surface for the model to invent against.
    """
    notes: list[str] = []
    gaps: list[str] = []
    skipped: list[str] = []

    servers = spec.get("servers") or []
    if not servers:
        raise OpenAPIError("spec declares no `servers`; cannot determine base_url")
    base_url = servers[0]["url"].rstrip("/")
    if len(servers) > 1:
        notes.append(f"base_url: took servers[0] ({base_url}); spec lists {len(servers)} servers")
    if re.search(r"stag|dev|test|sandbox|localhost", base_url, re.I):
        gaps.append(f"base_url {base_url} looks non-production — confirm before deploying")

    auth, auth_note = _detect_auth(spec)
    notes.append(auth_note)

    method, path, op = find_operation(spec, search_operation)
    params = [deref(p, spec) for p in (op.get("parameters") or [])]

    # --- structural roles ------------------------------------------------
    by_name = {p["name"]: p for p in params}
    roles: dict[str, str] = {}
    for role, candidates in ROLE_CANDIDATES.items():
        for candidate in candidates:
            if candidate in by_name:
                roles[role] = candidate
                break
    for role in ("query", "page", "page_size"):
        if role not in roles:
            gaps.append(f"no parameter looked like the {role!r} role — set it by hand")
    if roles:
        notes.append(
            "roles matched by name: "
            + ", ".join(f"{r}={n}" for r, n in roles.items())
            + " — verify against the API docs"
        )

    query_block: dict[str, Any] = {}
    if "query" in roles:
        query_block[roles["query"]] = "{query}"
    if "page" in roles:
        query_block[roles["page"]] = "{page}"
    if "page_size" in roles:
        query_block[roles["page_size"]] = "{page_size}"
    if "sort" in roles:
        query_block[roles["sort"]] = "{sort}"

    first_page = 1
    if "page" in roles:
        default = (by_name[roles["page"]].get("schema") or {}).get("default")
        if isinstance(default, int):
            first_page = default
            notes.append(f"first_page={first_page} taken from the {roles['page']!r} default")
        else:
            gaps.append("page origin not declared in the schema — assumed 1-based")

    page_size = None
    if "page_size" in roles:
        default = (by_name[roles["page_size"]].get("schema") or {}).get("default")
        if isinstance(default, int):
            page_size = default

    # --- filters ---------------------------------------------------------
    role_params = set(roles.values())
    exclude_set = set(exclude or [])
    filters: dict[str, Any] = {}
    for p in params:
        pname = p["name"]
        if pname in role_params or pname in exclude_set:
            continue
        if include is not None and pname not in include:
            skipped.append(pname)
            continue
        if p.get("in") not in (None, "query"):
            skipped.append(pname)
            continue
        schema = p.get("schema") or {}
        scalar, is_array = _schema_type(schema)
        entry: dict[str, Any] = {
            "param": pname,
            "type": JSON_TO_FILTER_TYPE.get(scalar, "string"),
            "description": clean_text(p.get("description")),
        }
        enum = schema.get("enum") or (schema.get("items") or {}).get("enum")
        if enum:
            entry["values"] = [str(v) for v in enum]
        if is_array:
            entry["style"] = "repeat"
        if not entry["description"]:
            gaps.append(f"filter {pname!r} has no description in the spec — write one")
        filters[pname] = entry

    if include is None and len(filters) > 15:
        gaps.append(
            f"{len(filters)} filters were imported. Every one goes into the query-understanding "
            "prompt, so re-run with --include to expose only the ones a user would actually "
            "express in words."
        )

    # --- sorts -----------------------------------------------------------
    sorts: list[dict[str, str]] = []
    if "sort" in roles:
        enum = (by_name[roles["sort"]].get("schema") or {}).get("enum") or []
        for value in enum:
            sorts.append({"name": str(value).lower(), "value": str(value), "description": ""})
        if sorts:
            gaps.append(
                "sort descriptions are blank — the spec has only raw enum values. "
                "Say when each applies (e.g. 'use for cheapest') or the planner will guess."
            )

    # --- response --------------------------------------------------------
    response: dict[str, Any] = {}
    schema = _success_schema(op, spec)
    if schema is None:
        gaps.append("no JSON response schema on the 200 — response mapping left blank")
    else:
        items_path, item_schema = _find_items_path(schema, spec)
        if items_path:
            response["items_path"] = items_path
            for candidate in TOTAL_CANDIDATES:
                if candidate in (schema.get("properties") or {}):
                    response["total_path"] = candidate
                    break
        else:
            gaps.append("could not find a result array in the response envelope")
            item_schema = schema

        leaves = _leaf_paths(item_schema or {}, spec)
        fields = _match_field_roles(leaves)
        response["fields"] = fields
        missing = [r for r in FIELD_CANDIDATES if r not in fields]
        if missing:
            gaps.append(
                f"response fields {', '.join(missing)} were not matched by name — "
                f"pick from: {', '.join(leaves[:12])}{'…' if len(leaves) > 12 else ''}"
            )
        mapped = set(fields.values())
        response["attributes"] = (
            {leaf.split(".")[-1].replace("[0]", ""): leaf for leaf in leaves if leaf not in mapped}
            if max_attributes
            else {}
        )
        if len(response["attributes"]) > max_attributes:
            response["attributes"] = dict(list(response["attributes"].items())[:max_attributes])
            notes.append(
                f"attributes truncated to the first {max_attributes} unmapped leaves "
                f"of {len(leaves)} — prune or extend by hand"
            )

    api: dict[str, Any] = {
        "name": name,
        "description": clean_text(op.get("summary"))
        or clean_text((spec.get("info") or {}).get("title")),
        "base_url": base_url,
        "auth": auth,
        "search": {
            "method": method,
            "path": path,
            "query": query_block,
            "first_page": first_page,
            "filters": filters,
            "sorts": sorts,
            "response": response,
        },
    }
    if page_size:
        api["page_size"] = page_size

    if item_operation:
        api["get_item"] = _derive_get_item(spec, item_operation, gaps)

    return Derivation(config=api, notes=notes, gaps=gaps, skipped=skipped)


def _success_schema(op: dict[str, Any], root: dict[str, Any]) -> dict[str, Any] | None:
    for code in ("200", "201", "default"):
        response = deref((op.get("responses") or {}).get(code) or {}, root)
        for media, body in (response.get("content") or {}).items():
            if "json" in media and body.get("schema"):
                return deref(body["schema"], root)
    return None


def _derive_get_item(spec: dict[str, Any], operation_id: str, gaps: list[str]) -> dict[str, Any]:
    method, path, op = find_operation(spec, operation_id)
    path_params = [
        deref(p, spec)["name"]
        for p in (op.get("parameters") or [])
        if deref(p, spec).get("in") == "path"
    ]
    if len(path_params) == 1 and path_params[0] != "id":
        path = path.replace("{" + path_params[0] + "}", "{id}")
    elif not path_params:
        gaps.append(
            f"get_item operation {operation_id!r} has no path parameter to fill with the id"
        )

    block: dict[str, Any] = {"method": method, "path": path, "response": {}}
    schema = _success_schema(op, spec)
    if schema:
        item_schema, item_path = schema, None
        for key, raw_value in (schema.get("properties") or {}).items():
            value = deref(raw_value, spec)
            if value.get("properties"):
                item_schema, item_path = value, key
                break
        if item_path:
            block["response"]["item_path"] = item_path
        block["response"]["fields"] = _match_field_roles(_leaf_paths(item_schema, spec))
    return block


def render_config(
    derivations: list[Derivation],
    *,
    defaults: dict[str, Any] | None = None,
    bedrock: dict[str, Any] | None = None,
) -> str:
    """Render derived APIs as a bridge config file, with a provenance header."""
    document: dict[str, Any] = {"version": 1}
    if defaults:
        document["defaults"] = defaults
    if bedrock:
        document["bedrock"] = bedrock
    document["apis"] = [d.config for d in derivations]

    header = [
        "# Generated by `mcp-api-bridge import-openapi`. Review before use.",
        "#",
        "# The wire contract below came from the spec. What the spec cannot express —",
        "# which parameter is the free-text query, which response field is the title,",
        "# when each sort applies — was guessed by name or left blank. Those are the",
        "# lines worth reading closely.",
        "#",
    ]
    for d in derivations:
        if d.gaps:
            header.append(f"# {d.config['name']}:")
            header.extend(f"#   - {g}" for g in d.gaps)
    header.append("")
    return "\n".join(header) + yaml.safe_dump(
        document, sort_keys=False, width=100, allow_unicode=True
    )
