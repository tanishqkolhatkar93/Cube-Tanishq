"""Optional HTTP front door for the orchestrator (useful for a deployed demo).

  uvicorn orchestration.api:app --port 8100
  POST /workflows                 {"org_id": "org_demo_alpha", "unit_id": "UNIT-0002"}   -> Workflow State (runs it)
  GET  /workflows/{id}            -> Workflow State
  GET  /workflows/{id}/evidence   -> the workflow plus all its evidence records
  POST /workflows/{id}/resume     -> continue after a halt / decision / failure
  POST /workflows/{id}/overrides  {"record_id": "...", "new_verdict": "PASS", "actor": "...", "reason": "..."}
  GET  /health                    -> orchestrator and every agent in the flow
  GET  /                          -> same-origin operations console
Resource routes require a Principal in ASGI scope from trusted authentication middleware.
No client header or body value establishes that identity. Unconfigured access fails closed.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
import re
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]


def load_local_env(path: Path = ROOT / ".env") -> None:
    """Load local demo env without overriding real process env."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = re.sub(r"\s+#.*$", "", value).strip()
        if key and key not in os.environ:
            os.environ[key] = value


load_local_env()

from shared.utils import sample_data

from .clients import HttpClient, client_for, load_manifest
from .orchestrator import apply_override, bundle, default_flow_path, discover_inputs, flow_stages, load_flow, resume, run_workflow
from .store import EvidenceConflict, FileStore, StoreIntegrityError, identifier

app = FastAPI(title="CUBE Round 3 orchestrator")
FLOW = os.environ.get("ORCH_FLOW") or default_flow_path()
STORE = FileStore()
FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"
SESSION_COOKIE = "cube_operations_session"
SESSION_MAX_AGE = 8 * 60 * 60
SESSION_LOCK = threading.Lock()
SESSIONS: dict[str, tuple["Principal", float]] = {}
TENANT_TOKEN_ENV = {
    "org_demo_alpha": "ORG_ALPHA_TOKEN",
    "org_demo_bravo": "ORG_BRAVO_TOKEN",
}
DEFAULT_INPUT_ROOT = Path(__file__).resolve().parents[1] / "data" / "input"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$")


def same_secret(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


@app.get("/health")
def health() -> dict:
    agents = {}
    for stage in flow_stages(load_flow(FLOW)):
        client = client_for(stage)
        manifest = load_manifest(stage)
        try:
            report = client.health() if isinstance(client, HttpClient) else {"status": "ok", "mode": "inproc"}
            agents[stage] = {
                **report,
                "agent_id": manifest["agent_id"],
                "implementation": manifest["implementation"],
                "owner": manifest["owner"],
            }
        except Exception as exc:
            agents[stage] = {
                "status": "down",
                "error": str(exc)[:200],
                "agent_id": manifest["agent_id"],
                "implementation": manifest["implementation"],
                "owner": manifest["owner"],
            }
    ok = all(a["status"] == "ok" for a in agents.values())
    return {"status": "ok" if ok else "degraded", "flow": load_flow(FLOW)["flow_id"], "agents": agents}


@dataclass(frozen=True)
class Principal:
    org_id: str
    actor: str

    def __post_init__(self):
        identifier(self.org_id)
        if not isinstance(self.actor, str) or not self.actor.strip() or any(ord(c) < 32 for c in self.actor):
            raise ValueError("invalid authenticated actor")


def require_principal(request: Request) -> Principal:
    # Set ONLY by trusted server-side middleware after authenticating the caller.
    principal = request.scope.get("orchestration.principal")
    if not isinstance(principal, Principal):
        raise HTTPException(401, "authenticated tenant context required")
    return principal


def safe_stage(stage: str) -> str:
    if stage not in set(flow_stages(load_flow(FLOW))):
        raise HTTPException(404, "stage not found in configured flow")
    return stage


def safe_filename(filename: str) -> str:
    if not isinstance(filename, str) or not SAFE_FILENAME.fullmatch(filename):
        raise HTTPException(422, "safe filename required")
    if filename in {".", ".."} or any(part in filename for part in ("/", "\\", ":", "%")):
        raise HTTPException(422, "safe filename required")
    return filename


def input_root() -> Path:
    return Path(os.environ.get("INPUT_DIR", DEFAULT_INPUT_ROOT)).resolve()


def input_folder(unit_id: str, stage: str) -> Path:
    unit = identifier(unit_id)
    stage = safe_stage(stage)
    root = input_root()
    folder = (root / unit / stage).resolve()
    if not folder.is_relative_to(root):
        raise HTTPException(422, "invalid input path")
    return folder
    
# --- Demo principal: OFF by default; set ONLY by the operator via env (server-side
# trusted configuration, not a client header). Production replaces this with real
# authentication middleware. Misconfigured -> stays fail-closed.
_DEMO_PRINCIPAL = os.environ.get("ORCH_DEMO_PRINCIPAL")  # e.g. "org_demo_alpha:op_demo"

@app.middleware("http")
async def _demo_principal_middleware(request, call_next):
    if _DEMO_PRINCIPAL and "orchestration.principal" not in request.scope:
        org, _, actor = _DEMO_PRINCIPAL.partition(":")
        try:
            request.scope["orchestration.principal"] = Principal(org, actor or "demo-operator")
        except ValueError:
            pass
    return await call_next(request)


def configured_tenant_token(org_id: str) -> str:
    env_name = TENANT_TOKEN_ENV[org_id]
    token = os.environ.get(env_name, "")
    if not token or token.startswith("replace-with-"):
        raise HTTPException(503, f"tenant access is not configured ({env_name})")
    for other_org, other_env_name in TENANT_TOKEN_ENV.items():
        other_token = os.environ.get(other_env_name, "")
        if (
            other_org != org_id
            and other_token
            and not other_token.startswith("replace-with-")
            and same_secret(token, other_token)
        ):
            raise HTTPException(503, "tenant access tokens must be unique")
    return token


@app.middleware("http")
async def authenticate_tenant_session(request: Request, call_next):
    presented = request.cookies.get(SESSION_COOKIE)
    if presented:
        with SESSION_LOCK:
            session = SESSIONS.get(presented)
            if session and session[1] <= time.time():
                SESSIONS.pop(presented, None)
                session = None
        if session:
            request.scope["orchestration.principal"] = session[0]
    return await call_next(request)


@app.post("/auth/session")
def create_session(body: dict, request: Request, response: Response) -> dict:
    org_id, token = body.get("org_id"), body.get("token")
    if not isinstance(org_id, str) or org_id not in TENANT_TOKEN_ENV:
        raise HTTPException(401, "invalid organization credentials")
    expected = configured_tenant_token(org_id)
    if not isinstance(token, str) or not same_secret(token, expected):
        raise HTTPException(401, "invalid organization credentials")
    actor = f"{org_id}:operator"
    session_id = secrets.token_urlsafe(32)
    expires_at = time.time() + SESSION_MAX_AGE
    with SESSION_LOCK:
        now = time.time()
        expired_sessions = [key for key, value in SESSIONS.items() if value[1] <= now]
        for key in expired_sessions:
            SESSIONS.pop(key, None)
        SESSIONS[session_id] = (Principal(org_id=org_id, actor=actor), expires_at)
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    return {"org_id": org_id, "actor": actor, "expires_in": SESSION_MAX_AGE}


@app.get("/auth/session")
def current_session(request: Request) -> dict:
    principal = request.scope.get("orchestration.principal")
    if not isinstance(principal, Principal):
        return {"authenticated": False}
    return {"authenticated": True, "org_id": principal.org_id, "actor": principal.actor}


@app.delete("/auth/session")
def delete_session(request: Request, response: Response) -> dict:
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        with SESSION_LOCK:
            SESSIONS.pop(session_id, None)
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    return {"signed_out": True}


@app.exception_handler(StoreIntegrityError)
async def corrupt_store(request, exc):
    return JSONResponse({"detail": "stored data failed integrity validation"}, status_code=503)


@app.exception_handler(EvidenceConflict)
async def conflicting_store(request, exc):
    return JSONResponse({"detail": "stored evidence conflicts with this operation"}, status_code=409)


@app.post("/workflows")
def create(body: dict, principal: Principal = Depends(require_principal)) -> dict:
    org, subject = body.get("org_id"), body.get("subject_id") or body.get("unit_id")
    if org != principal.org_id:
        raise HTTPException(403, "org does not match authenticated scope")
    try:
        identifier(subject)
    except ValueError:
        raise HTTPException(422, "valid unit_id or subject_id required") from None
    case = {"org_id": org, "unit_id": subject, "route": body.get("route") or sample_data.route(subject, org),
            "returned": body.get("returned", sample_data.has("returns", subject, org))}
    return run_workflow(case, load_flow(FLOW), STORE.for_org(org, actor=principal.actor))


def _get(workflow_id: str, store) -> dict:
    try:
        wf = store.load_workflow(workflow_id)
    except ValueError:
        wf = None
    if wf is None:
        raise HTTPException(404, "workflow not found")
    return wf


@app.get("/workflows/{workflow_id}")
def get(workflow_id: str, principal: Principal = Depends(require_principal)) -> dict:
    return _get(workflow_id, STORE.for_org(principal.org_id, actor=principal.actor))


@app.get("/workflows/{workflow_id}/evidence")
def evidence(workflow_id: str, principal: Principal = Depends(require_principal)) -> dict:
    store = STORE.for_org(principal.org_id, actor=principal.actor)
    return bundle(_get(workflow_id, store), store)


@app.post("/workflows/{workflow_id}/resume")
def resume_workflow(workflow_id: str, principal: Principal = Depends(require_principal)) -> dict:
    store = STORE.for_org(principal.org_id, actor=principal.actor)
    _get(workflow_id, store)
    return resume(workflow_id, load_flow(FLOW), store)


@app.post("/workflows/{workflow_id}/overrides")
def override(workflow_id: str, body: dict, principal: Principal = Depends(require_principal)) -> dict:
    store = STORE.for_org(principal.org_id, actor=principal.actor)
    _get(workflow_id, store)
    if body.get("actor", principal.actor) != principal.actor:
        raise HTTPException(403, "actor does not match authenticated principal")
    try:
        return apply_override(workflow_id, store, record_id=body.get("record_id", ""),
                              new_verdict=body.get("new_verdict", ""), actor=principal.actor,
                              reason=body.get("reason", ""), new_outcome=body.get("new_outcome"))
    except ValueError:
        raise HTTPException(422, "invalid override") from None


@app.get("/inputs/{unit_id}")
def list_inputs(unit_id: str, principal: Principal = Depends(require_principal)) -> dict:
    unit = identifier(unit_id)
    stages = flow_stages(load_flow(FLOW))
    return {
        "org_id": principal.org_id,
        "unit_id": unit,
        "stages": {stage: discover_inputs(unit, stage) for stage in stages},
    }


@app.post("/inputs/{unit_id}/{stage}")
async def upload_input(unit_id: str, stage: str, request: Request, filename: str,
                       principal: Principal = Depends(require_principal)) -> dict:
    name = safe_filename(filename)
    folder = input_folder(unit_id, stage)
    body = await request.body()
    if not body:
        raise HTTPException(422, "file body required")
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large")
    folder.mkdir(parents=True, exist_ok=True)
    target = (folder / name).resolve()
    if target.parent != folder:
        raise HTTPException(422, "invalid input path")
    target.write_bytes(body)
    return {
        "org_id": principal.org_id,
        "unit_id": identifier(unit_id),
        "stage": safe_stage(stage),
        "inputs": discover_inputs(identifier(unit_id), safe_stage(stage)),
    }


@app.delete("/inputs/{unit_id}/{stage}/{filename}")
def delete_input(unit_id: str, stage: str, filename: str, principal: Principal = Depends(require_principal)) -> dict:
    name = safe_filename(filename)
    folder = input_folder(unit_id, stage)
    target = (folder / name).resolve()
    if target.parent != folder:
        raise HTTPException(422, "invalid input path")
    try:
        target.unlink()
    except FileNotFoundError:
        raise HTTPException(404, "input not found") from None
    return {
        "org_id": principal.org_id,
        "unit_id": identifier(unit_id),
        "stage": safe_stage(stage),
        "inputs": discover_inputs(identifier(unit_id), safe_stage(stage)),
    }


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
