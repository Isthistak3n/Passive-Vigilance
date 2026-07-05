"""survey_sync — the mobile node's client for exchanging survey data with the fixed
node (docs/design-and-roadmap.md §5.5, the recon pair).

Transport is **store-and-forward over the base LAN**: while the mobile node is out
surveying it is out of contact; it pulls open taskings and pushes computed findings
only when the fixed node is reachable again (back at base). Every call **fails soft**
— an unreachable fixed node is the normal field state, not an error, so the mobile
node keeps surveying and retries next cycle.

Auth is the fixed node's ``GUI_TOKEN`` presented as a Bearer header, matching the
GUI server's ``check_auth``. All I/O is async ``aiohttp`` (mirrors
:mod:`modules.kismet`); the orchestrator's guarded sync task awaits it off the poll
hot path so a slow/stalled base node never blocks capture.
"""
from __future__ import annotations

import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class SurveySync:
    """Async client the mobile node uses to reach the fixed node's survey endpoints.

    Args:
        fixed_url: Base URL of the fixed node's GUI server (e.g. ``http://10.0.0.5:8080``).
        token:     The fixed node's ``GUI_TOKEN`` (Bearer). Required — the tasking and
                   survey endpoints are token-gated control actions.
        timeout:   Per-request timeout in seconds.
    """

    def __init__(self, fixed_url: Optional[str], token: Optional[str] = None,
                 timeout: float = 10.0) -> None:
        self._base = (fixed_url or "").rstrip("/")
        self._token = (token or "").strip()
        self._timeout = float(timeout)

    @property
    def configured(self) -> bool:
        """True only when a fixed-node URL is set — otherwise sync is a no-op and the
        node simply runs as a plain mobile node."""
        return bool(self._base)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def reachable(self) -> bool:
        """Cheap probe: is the fixed node's GUI answering AND is our token accepted?

        Hits ``/api/status`` WITH the Bearer token — when the fixed node has a
        ``GUI_TOKEN`` set, its ``check_auth`` gates every endpoint, so an unauthenticated
        probe would 401 even though the node is up. Presenting the token means a 200
        confirms both reachability and authorization (a 401/403 or any transport error
        both read as "can't sync now" -> retry next cycle)."""
        if not self._base:
            return False
        url = f"{self._base}/api/status"
        try:
            async with aiohttp.ClientSession(headers=self._headers()) as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=self._timeout)
                ) as resp:
                    return resp.status == 200
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            logger.debug("Survey fixed node not reachable (%s): %s", url, exc)
            return False

    async def pull_taskings(self) -> Optional[list]:
        """GET the fixed node's open taskings. Returns the list, or ``None`` on any
        failure (caller keeps whatever it already has and retries next cycle)."""
        if not self._base:
            return None
        url = f"{self._base}/api/tasking"
        try:
            async with aiohttp.ClientSession(headers=self._headers()) as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=self._timeout)
                ) as resp:
                    if resp.status == 401 or resp.status == 403:
                        logger.warning(
                            "Survey pull rejected (HTTP %d) — check SURVEY_TOKEN "
                            "matches the fixed node's GUI_TOKEN", resp.status)
                        return None
                    if resp.status != 200:
                        logger.debug("Survey pull got HTTP %d", resp.status)
                        return None
                    data = await resp.json()
                    return data if isinstance(data, list) else None
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as exc:
            logger.debug("Survey pull failed (%s): %s", url, exc)
            return None

    async def push_findings(self, task_id: str, result: dict,
                            survey_node: Optional[str] = None) -> bool:
        """POST a computed survey result (located home AP + device clusters + outcome)
        for one tasking to the fixed node. Returns True on success; False (soft) on any
        failure so the caller can retry next cycle."""
        if not self._base:
            return False
        url = f"{self._base}/api/survey"
        payload = {
            "task_id": task_id,
            "survey_node": survey_node,
            "result": result,
        }
        try:
            async with aiohttp.ClientSession(headers=self._headers()) as session:
                async with session.post(
                    url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                ) as resp:
                    if resp.status in (401, 403):
                        logger.warning(
                            "Survey push rejected (HTTP %d) — check SURVEY_TOKEN",
                            resp.status)
                        return False
                    if resp.status != 200:
                        logger.debug("Survey push got HTTP %d", resp.status)
                        return False
                    return True
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            logger.debug("Survey push failed (%s): %s", url, exc)
            return False
