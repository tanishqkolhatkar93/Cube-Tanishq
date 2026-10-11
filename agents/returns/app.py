"""Existing /health and /run API, with explicit trusted Returns configuration."""
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .adapter import Adapter
from .reference_config import from_env as reference_from_env
from .request_store import Rejected


def configured_adapter():
    filename = os.environ.get("RETURNS_CONFIG")
    state = os.environ.get("RETURNS_STATE_DIR")
    if not filename or not state:
        raise RuntimeError("returns_configuration_required")
    path = Path(filename).resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    reference_store, reference_comparer = reference_from_env()
    return Adapter(config, path.parent, Path(state).resolve(),
                   reference_store=reference_store, reference_comparer=reference_comparer)


def handle(request):
    return configured_adapter().handle(request)


app = FastAPI(title="Returns Manager")


@app.get("/health")
def health():
    try:
        configured_adapter()
        status = "ok"
    except Exception:
        status = "degraded"
    return {"status": status, "stage": "returns", "version": "1", "contract_version": "1.0"}


@app.post("/run")
async def run(request: Request):
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        return JSONResponse({"error": "invalid_json"}, status_code=422)
    try:
        return await run_in_threadpool(handle, body)
    except Rejected as exc:
        return JSONResponse({"error": exc.code}, status_code=exc.status)
    except Exception:
        return JSONResponse({"error": "returns_unavailable"}, status_code=503)
