"""Entra ID bearer-token auth for Foundry-hosted agent endpoints.

The prompt agent and hosted agents (responses/invocations) are exposed
through Entra ID-protected Foundry endpoints (scope
``https://ai.azure.com/.default``). The custom agents' Container Apps are
anonymous and needs none of this.
"""

from __future__ import annotations

import os
import time

import httpx

_SCOPE = "https://ai.azure.com/.default"
_STATIC_TOKEN_ENV = "AZURE_AI_ACCESS_TOKEN"


class EntraTokenAuth(httpx.Auth):
    """``httpx.Auth`` that attaches a cached Entra ID bearer token.

    Also usable for non-httpx transports (e.g. websockets) via :meth:`header`.
    """

    def __init__(self, scope: str = _SCOPE) -> None:
        # A pre-issued token lets environments without an interactive/managed
        # identity login (e.g. the ACA sandbox benchmark runner) authenticate.
        # It is not refreshed, so it expires with the issuing credential.
        self._static_token = os.environ.get(_STATIC_TOKEN_ENV, "").strip() or None
        self._credential = None
        if self._static_token is None:
            from azure.identity import DefaultAzureCredential  # local import: optional dep

            self._credential = DefaultAzureCredential()
        self._scope = scope
        self._token = None

    def _get_token(self) -> str:
        if self._static_token is not None:
            return self._static_token
        if self._token is None or self._token.expires_on <= time.time() + 60:
            self._token = self._credential.get_token(self._scope)
        return self._token.token

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {self._get_token()}"
        yield request

    def header(self) -> dict[str, str]:
        """Authorization header dict for transports that don't use httpx.Auth."""
        return {"Authorization": f"Bearer {self._get_token()}"}
