# Deployment

One service runs everything: the orchestrator API, all five agents (in-process) and the operations console. UI and API share an origin, so the HttpOnly `SameSite=Strict` session cookie works with no CORS and no proxy rewrites.

```
Browser ──HTTPS──▶  Render web service  (Docker image, non-root)
                    └─ uvicorn orchestration.api:app
                       ├─ /            operations console (frontend/)
                       ├─ /auth/*      tenant sessions (cookie)
                       ├─ /workflows*  run · evidence · resume · override
                       ├─ /inputs/*    stage uploads
                       └─ /health      status, build (version, commit, store, demo stages), agents
        optional: Supabase (durable workflows/evidence, Returns reference photos)
```

## What the image does out of the box (verified)

Built and run in Docker with **no `.env`, no API key and no Supabase**:

| Check | Result |
|---|---|
| `/health` | `ok`, all five agents `ok`, build commit shown, demo stages listed |
| Console | served at `/` |
| `GET /workflows/x` without a session | `401` |
| Login behind an HTTPS proxy header | session cookie is `HttpOnly; SameSite=Strict; Secure` |
| Docker `HEALTHCHECK` | `healthy`; process runs as non-root user `app` |
| UNIT-0014 (FBA, returned) | `BLOCKED / CLAIM_RECOMMENDED`, 5 of 5 stages handled |
| UNIT-9003, 9004, 9005 | `COMPLETED / CLEAN`, 5 of 5 stages handled |
| Repeat a run in the same container | same result (replay is stable) |
| Another tenant reading a workflow | `404` |

### It ships in demo mode, and says so

The image sets `RECEIVING_DEMO_MODE`, `PREP_DEMO_MODE`, `PACK_DEMO_MODE` and `RETURNS_DEMO_MODE` to `1`: the agents use their local deterministic providers on the bundled sample data. **No live model is called.** `/health` lists these stages under `build.demo_mode` and the console labels each affected agent "demo mode". Do not present a demo-mode run as live model output.

To use live models, set the `*_DEMO_MODE` variables to `0` and provide `GEMINI_API_KEY` and `GEMINI_FREE_TIER_CONFIRMED=1` as host secrets. That path is not exercised by the container checks above.

### Runtime state starts empty

`.dockerignore` excludes the mutable request-store databases that are tracked under `data/input/` (`pack.sqlite3`, `prep-requests.sqlite3`, `recovery.sqlite3`, `returns_live/state/`). They hold fingerprints that embed timestamps from earlier local runs, so shipping them makes Prep, Pack and Recovery fail with `request_content_conflict` on a fresh deployment. The agents recreate them. The product database `returns_live/source.sqlite3` is source data and does ship.

## Deploy to Render (about 5 minutes)

1. Push this repo to GitHub.
2. Render → **New → Blueprint** → select the repo. `render.yaml` is picked up.
3. Apply. Render builds the image and generates `ORG_ALPHA_TOKEN` and `ORG_BRAVO_TOKEN`.
4. Open the service → **Environment**, copy the token for the organisation you want.
5. Visit the URL, choose the organisation, paste the token, sign in.

The `Dockerfile` is portable to other hosts; the `Procfile` covers Heroku-style platforms (set the demo and config variables yourself there).

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `ORG_ALPHA_TOKEN`, `ORG_BRAVO_TOKEN` | yes | Per-tenant sign-in secrets. Unique, not starting with `replace-with-`. Unset means that tenant gets 503 (fails closed). |
| `PORT` | set by host | Listen port (default 8100). |
| `APP_VERSION`, `GIT_COMMIT` | no | Shown on `/health` and in the console footer. `RENDER_GIT_COMMIT` is used automatically on Render. |
| `*_DEMO_MODE` | baked in | `1` in the image. Set to `0` for live models. |
| `GEMINI_API_KEY`, `GEMINI_FREE_TIER_CONFIRMED` | live only | Vision model for Receiving/Returns. Required for reference comparison. |
| `STORE_BACKEND` | no | `file` (default) or `supabase`. |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | with `supabase` | Server-side only. The server refuses to start if `STORE_BACKEND=supabase` and these are missing. |
| `RETURNS_REFERENCE_COMPARE` | no | `1` turns on reference-photo comparison for Returns. |
| `REFERENCE_BACKEND`, `RETURNS_REFERENCE_DIR` | with comparison | `dir` (default, `data/reference`) or `supabase`. |
| `ORCH_DEMO_PRINCIPAL` | **never in production** | Skips sign-in for local demos. |

Do **not** set `INPUT_DIR` or `OUT_DIR` casually: the agents' request stores are keyed to the default locations, and a different path makes them reject replays.

## Durable storage (optional): Supabase

Run `supabase/migrations/0001_cube_store.sql`, then set `STORE_BACKEND=supabase`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`. Workflows and evidence then survive redeploys and restarts. This backend is tested against a protocol-level fake; verify it against a real project with the checklist in `docs/architecture/database.md` before relying on it. Free projects pause after 7 days idle: keep a keep-alive running and check the dashboard before judging.

Without it (`file` backend) the free tier's disk is ephemeral: runs and uploads are lost on redeploy or restart. The bundled sample data replays deterministically, so the demo units always work again.

## Verify a deployment

1. `GET /health` returns `"status":"ok"`, all five agents `ok`, and `build.commit` matching your latest push.
2. Sign in, run **UNIT-0014**: expect 5 of 5 stages handled (outcome `CLAIM_RECOMMENDED`, status `BLOCKED` awaiting human review).
3. `GET /workflows/anything` without signing in returns **401**.

CI builds the image and runs the same health, console and 401 checks on every push (`docker` job in `.github/workflows/ci.yml`).

## Run the image locally

```sh
docker build -t cube-operations .
docker run --rm -p 8100:8100 \
  -e ORG_ALPHA_TOKEN=local-alpha-secret-change-me \
  -e ORG_BRAVO_TOKEN=local-bravo-secret-change-me \
  cube-operations
# open http://localhost:8100
```

## Limits to know before a demo

- **Free tier sleeps** after about 15 minutes idle; the first request can take around a minute. Open the URL a few minutes before presenting.
- **Single instance only.** Sessions and the workflow lock are process-local.
- **Sessions are in memory:** a restart signs everyone out.
- **Live vision comparison takes roughly 20 to 40 seconds per unit** on the free Gemini tier; the console shows a running state.
- **Rotate tokens** if they are ever shared in chat, screenshots or a recording.
