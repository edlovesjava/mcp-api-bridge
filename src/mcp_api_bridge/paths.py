"""Path resolution and request templating.

Two small primitives the config layer leans on:

* `resolve(data, "a.b[0].c")` — read a value out of a nested response body.
  A `[*]` segment maps over a list.
* `render(template, ctx)` — substitute `{query}` / `{page}` / … into a
  configured request value, preserving the original type when the template
  is a single placeholder.
"""

from __future__ import annotations

import re
from typing import Any

_SEGMENT = re.compile(r"^([^\[\]]*)((?:\[(?:\d+|\*)\])*)$")
_INDEX = re.compile(r"\[(\d+|\*)\]")

_MISSING = object()


def resolve(data: Any, path: str | None, default: Any = None) -> Any:
    """Read `path` out of `data`, returning `default` if any segment is absent.

    Supports dotted keys, numeric indices, and `[*]` to map over a list:
    `results[*].sku` returns a list of skus.
    """
    if not path:
        return default
    current: Any = data
    for segment in path.split("."):
        match = _SEGMENT.match(segment)
        if match is None:
            return default
        key, indices = match.group(1), match.group(2)
        if key:
            current = _get_key(current, key)
            if current is _MISSING:
                return default
        for token in _INDEX.findall(indices):
            current = _get_index(current, token)
            if current is _MISSING:
                return default
    return default if current is _MISSING else current


def _get_key(current: Any, key: str) -> Any:
    if isinstance(current, list):
        # Mapping a key over a list produced by an earlier `[*]`.
        mapped = [_get_key(item, key) for item in current]
        return [m for m in mapped if m is not _MISSING]
    if isinstance(current, dict):
        return current.get(key, _MISSING)
    return _MISSING


def _get_index(current: Any, token: str) -> Any:
    if not isinstance(current, list):
        return _MISSING
    if token == "*":
        return current
    idx = int(token)
    return current[idx] if idx < len(current) else _MISSING


_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def render(template: Any, ctx: dict[str, Any]) -> Any:
    """Substitute `{name}` placeholders in `template` from `ctx`.

    A template that is exactly one placeholder returns the context value with
    its type intact (so `page: "{page}"` stays an int). Mixed templates are
    string-interpolated. Any unresolved placeholder makes the whole value
    `None`, which callers drop — that is how optional params disappear when
    the caller did not supply them.
    """
    if isinstance(template, dict):
        rendered = {k: render(v, ctx) for k, v in template.items()}
        return {k: v for k, v in rendered.items() if v is not None}
    if isinstance(template, list):
        rendered_list = [render(v, ctx) for v in template]
        return [v for v in rendered_list if v is not None]
    if not isinstance(template, str):
        return template

    whole = _PLACEHOLDER.fullmatch(template)
    if whole:
        return ctx.get(whole.group(1))

    out = template
    for name in _PLACEHOLDER.findall(template):
        value = ctx.get(name)
        if value is None:
            return None
        out = out.replace("{" + name + "}", str(value))
    return out


def prune(mapping: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None or an empty string/list."""
    return {k: v for k, v in mapping.items() if v is not None and v != "" and v != []}
