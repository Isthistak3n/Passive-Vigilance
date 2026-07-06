"""Tests for the mobile-node SurveySync client.

Runs a real tiny aiohttp server on an ephemeral port so the true HTTP path —
including the Bearer auth header and JSON round-trips — is exercised, then asserts
the client's fail-soft contract: an unreachable base node and an auth rejection
both return quietly (never raise), because a disconnected fixed node is the normal
field state, not an error.
"""

import asyncio

from aiohttp import web

from modules.survey_sync import SurveySync

TOKEN = "s3cr3t"


def _run(coro):
    return asyncio.run(coro)


class _FakeFixedNode:
    """A minimal stand-in for the fixed node's survey endpoints."""

    def __init__(self, token=TOKEN):
        self._token = token
        self.pushed = []  # (task_id, result) received via POST /api/survey

    def _authed(self, request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {self._token}"

    async def status(self, request):
        return web.json_response({"status": "ok"})

    async def tasking(self, request):
        if not self._authed(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        return web.json_response([
            {"task_id": "t1", "identity_key": "wifi-fp:abc", "status": "open"},
        ])

    async def survey(self, request):
        if not self._authed(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        body = await request.json()
        self.pushed.append((body.get("task_id"), body.get("result")))
        return web.json_response({"ok": True})


async def _serve(node):
    app = web.Application()
    app.router.add_get("/api/status", node.status)
    app.router.add_get("/api/tasking", node.tasking)
    app.router.add_post("/api/survey", node.survey)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    return runner, f"http://127.0.0.1:{port}"


def test_reachable_true_when_up():
    node = _FakeFixedNode()

    async def go():
        runner, url = await _serve(node)
        try:
            return await SurveySync(url, TOKEN).reachable()
        finally:
            await runner.cleanup()

    assert _run(go()) is True


def test_reachable_false_when_down():
    # Nothing listening on this port — must fail soft to False, not raise.
    sync = SurveySync("http://127.0.0.1:1", TOKEN, timeout=1.0)
    assert _run(sync.reachable()) is False


def test_pull_taskings_returns_list():
    node = _FakeFixedNode()

    async def go():
        runner, url = await _serve(node)
        try:
            return await SurveySync(url, TOKEN).pull_taskings()
        finally:
            await runner.cleanup()

    tasks = _run(go())
    assert isinstance(tasks, list) and tasks[0]["task_id"] == "t1"


def test_pull_taskings_bad_token_returns_none():
    node = _FakeFixedNode()

    async def go():
        runner, url = await _serve(node)
        try:
            return await SurveySync(url, "wrong-token").pull_taskings()
        finally:
            await runner.cleanup()

    assert _run(go()) is None


def test_push_findings_delivers_payload():
    node = _FakeFixedNode()

    async def go():
        runner, url = await _serve(node)
        try:
            sync = SurveySync(url, TOKEN)
            ok = await sync.push_findings(
                "t1", {"outcome": "resident", "home_ap": {"bssid": "a", "ssid": "N"},
                       "clusters": []},
                survey_node="mobile-1")
            return ok
        finally:
            await runner.cleanup()

    assert _run(go()) is True
    assert node.pushed and node.pushed[0][0] == "t1"


def test_push_findings_soft_fails_when_unreachable():
    sync = SurveySync("http://127.0.0.1:1", TOKEN, timeout=1.0)
    assert _run(sync.push_findings("t1", {})) is False


def test_not_configured_is_noop():
    sync = SurveySync("", TOKEN)
    assert sync.configured is False
    assert _run(sync.reachable()) is False
    assert _run(sync.pull_taskings()) is None
    assert _run(sync.push_findings("t1", {})) is False
