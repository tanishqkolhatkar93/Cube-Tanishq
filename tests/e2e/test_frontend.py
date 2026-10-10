import asyncio

import httpx

from orchestration.api import SESSION_COOKIE, app


def test_operations_console_and_tenant_sessions(monkeypatch, tmp_path):
    monkeypatch.setenv("ORG_ALPHA_TOKEN", "alpha-test-token")
    monkeypatch.setenv("ORG_BRAVO_TOKEN", "bravo-test-token")
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "inputs"))

    async def check_routes():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            page = await client.get("/")
            script = await client.get("/app.js")
            stylesheet = await client.get("/styles.css")
            health = await client.get("/health")
            anonymous_session = await client.get("/auth/session")
            missing_workflow = await client.get("/workflows/not-a-workflow")
            create_without_auth = await client.post(
                "/workflows", json={"org_id": "org_demo_alpha", "unit_id": "UNIT-0014"}
            )
            invalid_login = await client.post(
                "/auth/session", json={"org_id": "org_demo_alpha", "token": "wrong-token"}
            )
            login = await client.post(
                "/auth/session", json={"org_id": "org_demo_alpha", "token": "alpha-test-token"}
            )
            session = await client.get("/auth/session")
            session_id = login.cookies.get(SESSION_COOKIE)
            cross_tenant_create = await client.post(
                "/workflows", json={"org_id": "org_demo_bravo", "unit_id": "UNIT-0014"}
            )
            missing_scoped_workflow = await client.get("/workflows/not-a-workflow")
            upload = await client.post(
                "/inputs/UNIT-UPLOAD/receiving?filename=front.jpg",
                content=b"not really an image",
                headers={"content-type": "image/jpeg"},
            )
            listed_inputs = await client.get("/inputs/UNIT-UPLOAD")
            unsafe_upload = await client.post(
                "/inputs/UNIT-UPLOAD/receiving?filename=../escape.jpg",
                content=b"x",
                headers={"content-type": "image/jpeg"},
            )
            deleted_input = await client.delete("/inputs/UNIT-UPLOAD/receiving/front.jpg")
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test",
                cookies={SESSION_COOKIE: session_id},
            ) as replay_client:
                replayed_session_before_logout = await replay_client.get("/auth/session")
                logout = await client.delete("/auth/session")
                expired_session = await client.get("/auth/session")
                replayed_session_after_logout = await replay_client.get("/auth/session")
            async with httpx.AsyncClient(
                transport=transport, base_url="https://test"
            ) as secure_client:
                secure_login = await secure_client.post(
                    "/auth/session",
                    json={"org_id": "org_demo_alpha", "token": "alpha-test-token"},
                )
            monkeypatch.setenv("ORG_BRAVO_TOKEN", "alpha-test-token")
            ambiguous_login = await client.post(
                "/auth/session", json={"org_id": "org_demo_bravo", "token": "alpha-test-token"}
            )

        assert page.status_code == 200
        assert "CUBE Operations" in page.text
        assert "Five agent managers" in page.text
        assert script.status_code == 200 and "async function request" in script.text
        assert "async function uploadFile" in script.text
        assert stylesheet.status_code == 200 and "prefers-reduced-motion" in stylesheet.text
        assert health.status_code == 200 and health.json()["status"] == "ok"
        assert set(health.json()["agents"]) == {"receiving", "prep", "pack", "returns", "recovery"}
        assert all(agent["agent_id"] for agent in health.json()["agents"].values())
        assert anonymous_session.status_code == 200 and not anonymous_session.json()["authenticated"]
        assert missing_workflow.status_code == 401
        assert missing_workflow.json()["detail"] == "authenticated tenant context required"
        assert create_without_auth.status_code == 401
        assert create_without_auth.json()["detail"] == "authenticated tenant context required"
        assert invalid_login.status_code == 401
        assert "token" not in login.json()
        assert login.status_code == 200
        assert login.json()["org_id"] == "org_demo_alpha"
        assert login.json()["actor"] == "org_demo_alpha:operator"
        cookie = login.headers["set-cookie"].lower()
        assert f"{SESSION_COOKIE}=" in cookie
        assert session_id != "alpha-test-token"
        assert f"max-age={8 * 60 * 60}" in cookie
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert session.status_code == 200 and session.json()["org_id"] == "org_demo_alpha"
        assert cross_tenant_create.status_code == 403
        assert missing_scoped_workflow.status_code == 404
        assert upload.status_code == 200
        assert upload.json()["inputs"][0]["ref"] == "UNIT-UPLOAD/receiving/front.jpg"
        assert listed_inputs.status_code == 200
        assert listed_inputs.json()["stages"]["receiving"][0]["sha256"]
        assert unsafe_upload.status_code == 422
        assert deleted_input.status_code == 200
        assert deleted_input.json()["inputs"] == []
        assert replayed_session_before_logout.json()["authenticated"]
        assert logout.status_code == 200
        assert expired_session.status_code == 200 and not expired_session.json()["authenticated"]
        assert not replayed_session_after_logout.json()["authenticated"]
        assert "secure" in secure_login.headers["set-cookie"].lower()
        assert ambiguous_login.status_code == 503

    asyncio.run(check_routes())
