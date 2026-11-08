"""Pod-local proxy to the Anthropic API that adds the model credential.

Runs as its own container next to the agent sandbox, whose ``claude -p`` runs
point ``ANTHROPIC_BASE_URL`` here and never hold the credential::

    uvicorn model_proxy:app --host 127.0.0.1 --port 8787

The credential is a Claude subscription token (``CLAUDE_CODE_OAUTH_TOKEN``, from
``claude setup-token``), sent as a Bearer token with the OAuth beta flag, or
else an API key (``ANTHROPIC_API_KEY``). Only ``/v1/*`` is forwarded, to one
fixed upstream; any credential the client sends is dropped and replaced.
Responses are streamed through unchanged, so server-sent events work.
"""

from __future__ import annotations

import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

UPSTREAM = os.getenv("MODEL_PROXY_UPSTREAM", "https://api.anthropic.com")
# Request headers never forwarded: the client's own auth, and hop-by-hop ones.
DROP_REQUEST = frozenset(
    {"authorization", "x-api-key", "host", "content-length", "connection", "accept-encoding"}
)
DROP_RESPONSE = frozenset({"content-length", "content-encoding", "transfer-encoding", "connection"})
OAUTH_BETA = "oauth-2025-04-20"


def auth_headers(headers: dict[str, str], api_key: str, oauth_token: str) -> dict[str, str]:
    """Return ``headers`` with the pod's credential added (OAuth token preferred)."""
    if not oauth_token:
        return {**headers, "x-api-key": api_key}
    betas = [b.strip() for b in headers.pop("anthropic-beta", "").split(",") if b.strip()]
    if OAUTH_BETA not in betas:
        betas.append(OAUTH_BETA)
    return {**headers, "authorization": f"Bearer {oauth_token}", "anthropic-beta": ",".join(betas)}


def create_app(
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    oauth_token: str | None = None,
) -> FastAPI:
    """Proxy app; credentials default to ``CLAUDE_CODE_OAUTH_TOKEN`` / ``ANTHROPIC_API_KEY``.

    Both are read once, at startup.
    """
    token = oauth_token if oauth_token is not None else os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    proxy = FastAPI(title="Model proxy", docs_url=None, redoc_url=None, openapi_url=None)
    # No timeout on reads: long generations stream for minutes.
    upstream = httpx.AsyncClient(
        base_url=UPSTREAM, transport=transport, timeout=httpx.Timeout(30, read=None)
    )

    @proxy.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok" if token or key else "no_credential"}

    @proxy.api_route("/v1/{path:path}", methods=["GET", "POST"])
    async def forward(path: str, request: Request) -> StreamingResponse:
        headers = {
            k.lower(): v for k, v in request.headers.items() if k.lower() not in DROP_REQUEST
        }
        outgoing = upstream.build_request(
            request.method,
            f"/v1/{path}",
            params=request.query_params,
            headers=auth_headers(headers, key, token),
            content=await request.body(),
        )
        response = await upstream.send(outgoing, stream=True)
        return StreamingResponse(
            response.aiter_bytes(),  # decoded, so content-encoding is dropped
            status_code=response.status_code,
            headers={k: v for k, v in response.headers.items() if k.lower() not in DROP_RESPONSE},
            background=BackgroundTask(response.aclose),
        )

    return proxy


app = create_app()
