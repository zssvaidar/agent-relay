"""Pod-local proxy to the Anthropic API that adds the API key.

Runs as its own container next to the agent sandbox, which points
``ANTHROPIC_BASE_URL`` here and never holds the key::

    uvicorn model_proxy:app --host 127.0.0.1 --port 8787

Only ``/v1/*`` is forwarded, to one fixed upstream; any credential the client
sends is dropped and replaced. Responses are streamed through unchanged, so
server-sent events work.
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


def create_app(
    api_key: str | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    """Proxy app; ``api_key`` defaults to ``ANTHROPIC_API_KEY`` (read once, at startup)."""
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    proxy = FastAPI(title="Model proxy", docs_url=None, redoc_url=None, openapi_url=None)
    # No timeout on reads: long generations stream for minutes.
    upstream = httpx.AsyncClient(
        base_url=UPSTREAM, transport=transport, timeout=httpx.Timeout(30, read=None)
    )

    @proxy.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok" if key else "no_api_key"}

    @proxy.api_route("/v1/{path:path}", methods=["GET", "POST"])
    async def forward(path: str, request: Request) -> StreamingResponse:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQUEST}
        outgoing = upstream.build_request(
            request.method,
            f"/v1/{path}",
            params=request.query_params,
            headers={**headers, "x-api-key": key},
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
