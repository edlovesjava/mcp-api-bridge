"""Credential application.

Secrets live in the environment, never in the config file. Each auth block
names the variable to read; this module reads it at request time and produces
the headers and query params to merge into the outgoing request.
"""

from __future__ import annotations

import base64
import os

from .config import ApiKeyAuth, AuthConfig, BasicAuth, BearerAuth, ConfigError, NoAuth


def _require_env(name: str, api: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(
            f"catalog {api!r} needs the {name} environment variable set, but it is empty"
        )
    return value


def apply_auth(auth: AuthConfig, api_name: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return `(headers, query_params)` carrying the configured credential."""
    if isinstance(auth, NoAuth):
        return {}, {}

    if isinstance(auth, BearerAuth):
        token = _require_env(auth.token_env, api_name)
        value = f"{auth.scheme} {token}".strip() if auth.scheme else token
        return {auth.header: value}, {}

    if isinstance(auth, ApiKeyAuth):
        key = _require_env(auth.key_env, api_name)
        if auth.query_param:
            return {}, {auth.query_param: key}
        assert auth.header  # guaranteed by config validation
        return {auth.header: key}, {}

    if isinstance(auth, BasicAuth):
        user = _require_env(auth.username_env, api_name)
        password = _require_env(auth.password_env, api_name)
        encoded = base64.b64encode(f"{user}:{password}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}, {}

    raise ConfigError(f"unsupported auth type for catalog {api_name!r}: {auth!r}")
