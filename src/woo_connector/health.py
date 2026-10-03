"""Unauthenticated liveness probe. Returns nothing about the store or the caller."""

from starlette.requests import Request
from starlette.responses import JSONResponse

from woo_connector import __version__


async def healthz(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "version": __version__})
