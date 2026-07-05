"""Optional Flask-based web GUI for Passive Vigilance.

Disabled by default (GUI_ENABLED=false). When enabled, starts a Flask server
in a daemon thread alongside the asyncio event loop. Zero overhead when off.

Thread model:
  - Flask runs in a daemon thread (never blocks the asyncio loop)
  - Each SSE client gets a threading.Queue; a threading.Lock protects the list
  - push_event() is safe to call from the asyncio thread or any other thread
"""

import json
import logging
import os
import queue
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent
_MAX_RECENT = 200

# Durable-history bounds (P5): how many events a panel rebuilds from on-disk
# session logs, and how many session directories to walk back through. Kept
# bounded so a long-running node's history reads stay cheap; env-overridable.
_HISTORY_LIMIT = int(os.getenv("GUI_HISTORY_LIMIT", "500"))
_HISTORY_MAX_SESSIONS = int(os.getenv("GUI_HISTORY_MAX_SESSIONS", "20"))

# Mode values the toggle accepts — must mirror main._VALID_NODE_MODES.
_VALID_MODES = ("fixed", "mobile")

# Default .env location: project root (gui/ -> repo root), resolved from this
# file so it matches how the rest of the app loads .env — not a hardcoded path.
_DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def read_node_mode(env_path) -> str:
    """Return the NODE_MODE value currently set in *env_path*, or "" if absent.

    Ignores commented-out lines (``# NODE_MODE=...``).
    """
    try:
        text = Path(env_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    for line in text.splitlines():
        if line.split("=", 1)[0].strip() == "NODE_MODE":
            return line.split("=", 1)[1].strip() if "=" in line else ""
    return ""


def write_node_mode(mode: str, env_path) -> None:
    """Surgically set NODE_MODE in *env_path*, preserving everything else.

    Only the ``NODE_MODE=`` assignment line is rewritten in place; if no such
    line exists it is appended. All other lines, comments, blank lines, ordering
    and secrets are left byte-for-byte untouched. The write is atomic
    (temp file in the same directory + ``os.rename``), matching the crash-safe
    pattern in :class:`modules.ignore_list.IgnoreList`.
    """
    path = Path(env_path)
    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = ""

    lines = original.splitlines(keepends=True)
    new_lines: list[str] = []
    replaced = False
    for line in lines:
        if line.split("=", 1)[0].strip() == "NODE_MODE":
            nl = "\n" if line.endswith("\n") else ""
            new_lines.append(f"NODE_MODE={mode}{nl}")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] = new_lines[-1] + "\n"
        new_lines.append(f"NODE_MODE={mode}\n")

    content = "".join(new_lines)
    dir_ = str(path.parent)
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.rename(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class GUIServer:
    """Lightweight Flask server exposing live sensor data.

    Call :meth:`start` once to launch the server in a daemon thread.
    Call :meth:`push_event` from the orchestrator to broadcast events to SSE clients.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        orchestrator=None,
        env_path=None,
        survey_store=None,
    ) -> None:
        self._host = host
        self._port = port
        self._orchestrator = orchestrator  # weak back-reference for /api/status
        # SurveyStore handle (recon pair). Present only on a node with SURVEY_ENABLED;
        # None -> the tasking/survey endpoints report the feature is off.
        self._survey_store = survey_store
        # .env path the mode toggle writes to; overridable for tests.
        self._env_path = Path(env_path) if env_path else _DEFAULT_ENV_PATH

        self._gui_token: str = os.getenv("GUI_TOKEN", "").strip()

        self._clients: list[queue.Queue] = []
        self._clients_lock = threading.Lock()

        self._recent_wifi: list[dict] = []
        self._recent_aircraft: list[dict] = []
        self._recent_drone: list[dict] = []
        self._recent_ais: list[dict] = []
        self._recent_acars: list[dict] = []
        self._recent_alerts: list[dict] = []
        self._recent_nearby: list[dict] = []
        self._recent_remote_id: list[dict] = []

        # NODE_MODE is only read at startup (a running node keeps its mode until
        # restarted), so resolve it once here and use it to pick which template
        # index() serves — the mobile template drops the Leaflet map entirely.
        self._node_mode = read_node_mode(self._env_path)

        self._data_lock = threading.Lock()

        self._thread: Optional[threading.Thread] = None
        self._app = self._build_app()

    # ------------------------------------------------------------------
    # Flask app
    # ------------------------------------------------------------------

    def _build_app(self):
        try:
            from flask import Flask, Response, jsonify, render_template, stream_with_context
        except ImportError:
            logger.error("Flask not installed — GUI disabled. Run: pip install flask")
            return None

        app = Flask(
            __name__,
            template_folder=str(_HERE / "templates"),
            static_folder=str(_HERE / "static"),
        )
        app.logger.setLevel(logging.WARNING)

        # Suppress Flask's default startup banner in our logger
        import logging as _logging
        _logging.getLogger("werkzeug").setLevel(_logging.WARNING)

        gui_token = self._gui_token
        _TOKEN_COOKIE = "pv_gui_token"

        @app.before_request
        def check_auth():
            if not gui_token:
                return  # no auth configured — open access
            from flask import request as _req
            auth = _req.headers.get("Authorization", "")
            token_param = _req.args.get("token", "")
            # Accept a cookie too. The dashboard's own sub-resources — the CSS/JS
            # under /static, the /api/* fetches, and the /stream EventSource — can
            # carry neither an Authorization header nor a ?token= query (a browser
            # attaches nothing to <script src>/<link href>, and EventSource has no
            # header API), so a token-gated node used to 401 its own assets and
            # render blank. The page load authenticates once with ?token=, which
            # sets the cookie below; every same-origin sub-request then carries it.
            cookie_token = _req.cookies.get(_TOKEN_COOKIE, "")
            if (auth == f"Bearer {gui_token}"
                    or token_param == gui_token
                    or cookie_token == gui_token):
                return  # authorized
            return jsonify({"error": "Unauthorized"}), 401

        @app.after_request
        def persist_token_cookie(resp):
            # When a request authenticates via ?token=, drop a session cookie so
            # the browser carries it on the sub-resource requests it can't put a
            # token on. Session cookie (no max-age) so it clears on browser close;
            # SameSite=Strict so it is never sent cross-site; not Secure because
            # the LAN dashboard is served over plain HTTP. No-op once already set.
            if gui_token:
                from flask import request as _req
                if (_req.args.get("token", "") == gui_token
                        and _req.cookies.get(_TOKEN_COOKIE) != gui_token):
                    resp.set_cookie(
                        _TOKEN_COOKIE, gui_token,
                        httponly=True, samesite="Strict",
                    )
            return resp

        @app.route("/")
        def index():
            template = "mobile.html" if self._node_mode == "mobile" else "index.html"
            # The HTML doc must never be served stale: app.js is `no-cache` (always
            # revalidated, so always current), and a browser that heuristically
            # cached an OLDER index.html would then pair stale markup with fresh JS —
            # a missing element the new JS expects can break the page. Force the doc
            # to revalidate too so markup and script never drift.
            resp = app.make_response(render_template(template))
            resp.headers["Cache-Control"] = "no-cache"
            return resp

        @app.route("/api/status")
        def api_status():
            orch = self._orchestrator
            if orch is None:
                return jsonify({"status": "no_orchestrator"})
            # Which sensors are actually active — so the dashboard can show a
            # disabled sensor (e.g. DroneRF off) as off rather than healthy.
            active = dict(getattr(orch, "_modules_active", {}))
            # sensor_health is initialised all-True and only flipped False when a
            # poll *raises*; a disabled module never polls, so it would report
            # "healthy" forever (AIS/ACARS showing online while off). Gate health by
            # modules_active so the raw API is honest — a sensor that isn't running
            # can't be healthy. A health key with no modules_active entry (e.g. "sdr")
            # is left as-is. The dashboard already greys inactive sensors via
            # modules_active, so this only corrects direct API consumers.
            health = {
                k: bool(v) and active.get(k, True) is not False
                for k, v in getattr(orch, "_sensor_health", {}).items()
            }
            stats = dict(getattr(orch, "_stats", {}))
            fix = getattr(orch, "_current_fix", None)
            # Scoring/baseline state for the header — guarded so a status()
            # failure never breaks /api/status.
            scoring = None
            engine = getattr(orch, "persistence", None)
            if engine is not None:
                try:
                    scoring = engine.status()
                except Exception:
                    scoring = None
            return jsonify({
                "session_id":   getattr(orch, "session_id", ""),
                "sensor_health": health,
                "modules_active": active,
                "stats":         stats,
                "gps_fix":       fix,
                "scoring":       scoring,
            })

        @app.route("/api/wifi")
        def api_wifi():
            # Rebuild from the on-disk session logs so a refresh/restart shows the
            # real history (deduped to one row per device), not the truncated
            # in-memory cache (P5). Fall back to the cache when no session dir.
            hist = self._history("events.jsonl", _HISTORY_LIMIT, dedup_key="mac")
            if hist is not None:
                return jsonify(hist)
            with self._data_lock:
                return jsonify(list(self._recent_wifi))

        @app.route("/api/nearby")
        def api_nearby():
            with self._data_lock:
                return jsonify(list(self._recent_nearby))

        @app.route("/api/aircraft")
        def api_aircraft():
            # Durable per-ICAO log (P5) so the table survives a refresh AND a
            # restart, with the orchestrator's live current-sky index overlaid so
            # aircraft still overhead show fresh position/age for the map (older
            # table entries decay off the map client-side by recency). Disk gives
            # the full retention window; the live index gives current state for
            # what's flying now. Fall back to live index, then the push cache.
            hist = self._history("aircraft.jsonl", _HISTORY_LIMIT, dedup_key="icao")
            orch = self._orchestrator
            live = []
            if orch is not None and hasattr(orch, "current_aircraft"):
                try:
                    live = orch.current_aircraft()
                except Exception as exc:
                    logger.debug("current_aircraft() failed: %s", exc)
            if hist is None:
                if live:
                    return jsonify(live)
                with self._data_lock:
                    return jsonify(list(self._recent_aircraft))
            merged = {r["icao"]: r for r in hist if r.get("icao")}
            for r in live:                 # live record is fresher for a present plane
                ic = r.get("icao")
                if ic:
                    merged[ic] = r
            return jsonify(list(merged.values()))

        @app.route("/api/drone")
        def api_drone():
            # Disk-backed history (P5); discrete events, no dedup.
            hist = self._history("drone.jsonl", _HISTORY_LIMIT)
            if hist is not None:
                return jsonify(hist)
            with self._data_lock:
                return jsonify(list(self._recent_drone))

        @app.route("/api/ais")
        def api_ais():
            # Disk-backed history (P5); deduped to the latest report per vessel.
            hist = self._history("ais.jsonl", _HISTORY_LIMIT, dedup_key="mmsi")
            if hist is not None:
                return jsonify(hist)
            with self._data_lock:
                return jsonify(list(self._recent_ais))

        @app.route("/api/acars")
        def api_acars():
            # Disk-backed history (P5); discrete decoded messages, no dedup.
            hist = self._history("acars.jsonl", _HISTORY_LIMIT)
            if hist is not None:
                return jsonify(hist)
            with self._data_lock:
                return jsonify(list(self._recent_acars))

        @app.route("/api/alerts")
        def api_alerts():
            # Disk-backed history (P5) — alerts now persist to alerts.jsonl, so the
            # Alerts tab survives a refresh and a restart. No dedup (each alert is a
            # discrete occurrence).
            hist = self._history("alerts.jsonl", _HISTORY_LIMIT)
            if hist is not None:
                return jsonify(hist)
            with self._data_lock:
                return jsonify(list(self._recent_alerts))

        @app.route("/api/mode", methods=["GET", "POST"])
        def api_mode():
            """Report (GET) or change (POST) the node's NODE_MODE in .env.

            GET returns the configured mode and whether the control is enabled
            (a GUI_TOKEN must be set for writes). POST is a control action: it
            requires GUI_TOKEN to be configured, validates the mode, writes .env
            surgically, and tells the operator a RESTART is required — NODE_MODE
            is only read at startup, so a running node keeps its current mode
            until restarted.
            """
            from flask import request as _req

            if _req.method == "GET":
                return jsonify({
                    "mode": read_node_mode(self._env_path),
                    "control_enabled": bool(gui_token),
                })

            # POST — control action. The before_request check_auth already
            # enforced the bearer/?token= check when a token IS configured. When
            # no token is configured, that check is open, so we must refuse here:
            # control actions must never be reachable unauthenticated.
            if not gui_token:
                return jsonify({
                    "error": "control actions require GUI_TOKEN to be configured",
                }), 403

            data = _req.get_json(silent=True) or {}
            mode = str(data.get("mode", "")).strip().lower()
            if mode not in _VALID_MODES:
                return jsonify({
                    "error": f"mode must be one of {' | '.join(_VALID_MODES)}",
                }), 400

            try:
                write_node_mode(mode, self._env_path)
            except Exception as exc:
                logger.error("Mode toggle: failed to write .env: %s", exc)
                return jsonify({"error": f"failed to write .env: {exc}"}), 500

            logger.info("Mode toggle: NODE_MODE set to '%s' (restart required)", mode)
            return jsonify({
                "mode": mode,
                "saved": True,
                "restart_required": True,
                "message": (
                    f"NODE_MODE saved as '{mode}'. RESTART REQUIRED to take "
                    "effect — the node only reads NODE_MODE at startup and will "
                    "keep running in its current mode until it is restarted."
                ),
            })

        @app.route("/api/tasking", methods=["GET", "POST"])
        def api_tasking():
            """Survey taskings for the recon pair (design §5.5).

            GET returns the open taskings — the watchlist the mobile node pulls.
            POST enqueues a tasking for a contact (the operator's "Task survey"
            action). Both are token-gated: the tasking list reveals what is being
            investigated (opsec), and enqueuing is a control action — so when no
            GUI_TOKEN is configured, ``check_auth`` runs open and we must refuse
            here, exactly like ``/api/mode`` POST.
            """
            from flask import request as _req

            if not gui_token:
                return jsonify({
                    "error": "survey endpoints require GUI_TOKEN to be configured",
                }), 403
            if self._survey_store is None:
                return jsonify({"error": "survey feature not enabled on this node"}), 404

            if _req.method == "GET":
                try:
                    return jsonify(self._survey_store.open_taskings())
                except Exception as exc:  # pragma: no cover - defensive
                    logger.error("api_tasking GET failed: %s", exc)
                    return jsonify({"error": "tasking read failed"}), 500

            data = _req.get_json(silent=True) or {}
            identity_key = str(data.get("identity_key", "")).strip()
            if not identity_key:
                return jsonify({"error": "identity_key is required"}), 400
            # A bare mac: key is not portable to another node (randomized MACs), so a
            # tasking on it could never match on the mobile node — refuse it rather
            # than issue a dead task.
            if identity_key.startswith("mac:"):
                return jsonify({
                    "error": "this contact has no rotation-stable fingerprint and "
                             "cannot be surveyed by another node",
                }), 422
            try:
                task_id = self._survey_store.enqueue_tasking(
                    identity_key,
                    designator=data.get("designator"),
                    reason=data.get("reason") or "operator",
                    evidence=data.get("evidence"),
                    origin_node=data.get("origin_node"),
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("api_tasking POST failed: %s", exc)
                return jsonify({"error": "tasking write failed"}), 500
            logger.info("Survey tasking enqueued: %s (%s)", task_id, identity_key)
            self.push_event("survey", {"kind": "tasking", "task_id": task_id,
                                       "identity_key": identity_key})
            return jsonify({"task_id": task_id, "saved": True})

        @app.route("/api/survey", methods=["GET", "POST"])
        def api_survey():
            """Survey findings for the recon pair.

            GET returns every tasking joined with its bed-down findings (the fixed
            node's Survey panel feed). POST is the mobile node's offload: it ingests
            findings for a tasking and marks it complete. Token-gated like
            ``/api/tasking``.
            """
            from flask import request as _req

            if not gui_token:
                return jsonify({
                    "error": "survey endpoints require GUI_TOKEN to be configured",
                }), 403
            if self._survey_store is None:
                return jsonify({"error": "survey feature not enabled on this node"}), 404

            if _req.method == "GET":
                try:
                    return jsonify(self._survey_store.taskings_with_findings())
                except Exception as exc:  # pragma: no cover - defensive
                    logger.error("api_survey GET failed: %s", exc)
                    return jsonify({"error": "survey read failed"}), 500

            data = _req.get_json(silent=True) or {}
            task_id = str(data.get("task_id", "")).strip()
            findings = data.get("findings")
            if not task_id or not isinstance(findings, list):
                return jsonify({"error": "task_id and a findings list are required"}), 400
            try:
                self._survey_store.ingest_findings(
                    task_id, findings, survey_node=data.get("survey_node"))
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("api_survey POST failed: %s", exc)
                return jsonify({"error": "survey write failed"}), 500
            logger.info("Survey findings ingested for %s (%d cluster(s))",
                        task_id, len(findings))
            self.push_event("survey", {"kind": "finding", "task_id": task_id,
                                       "count": len(findings)})
            return jsonify({"ingested": True, "task_id": task_id})

        @app.route("/api/remote_id")
        def api_remote_id():
            # Remote ID is an air contact, so it serves the live per-UAS current-sky
            # index (like /api/aircraft), not the disk-history lens. Fall back to the
            # cache if the orchestrator is unavailable.
            orch = self._orchestrator
            if orch is not None and hasattr(orch, "current_remote_id"):
                try:
                    return jsonify(orch.current_remote_id())
                except Exception as exc:
                    logger.debug("current_remote_id() failed, using cache: %s", exc)
            with self._data_lock:
                return jsonify(list(self._recent_remote_id))

        @app.route("/stream")
        def stream():
            client_queue: queue.Queue = queue.Queue(maxsize=500)
            with self._clients_lock:
                self._clients.append(client_queue)

            def generate():
                try:
                    # Send a heartbeat immediately so the client knows we're alive
                    yield "data: {\"type\":\"heartbeat\"}\n\n"
                    while True:
                        try:
                            payload = client_queue.get(timeout=20)
                        except queue.Empty:
                            yield "data: {\"type\":\"heartbeat\"}\n\n"
                            continue
                        if payload is None:
                            break
                        yield f"data: {payload}\n\n"
                finally:
                    with self._clients_lock:
                        try:
                            self._clients.remove(client_queue)
                        except ValueError:
                            pass

            return Response(
                stream_with_context(generate()),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        return app

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def app(self):
        """The underlying Flask application, or None if Flask is not installed."""
        return self._app

    def start(self) -> bool:
        """Start Flask in a daemon thread. Returns False if Flask is not installed."""
        if self._app is None:
            return False
        self._thread = threading.Thread(
            target=self._serve_with_retry, daemon=True, name="gui-flask"
        )
        self._thread.start()
        logger.info("GUI server starting on http://%s:%d", self._host, self._port)
        return True

    def _serve_with_retry(self) -> None:
        """Run Flask, retrying the port bind before giving up.

        On a fast service restart (``Restart=always``) the previous process may
        still be releasing the port, so the first bind can fail with "address in
        use". Without a retry the daemon thread dies silently and the GUI is
        simply gone while the node keeps reporting healthy. This retries with a
        short backoff and, if it still cannot bind, logs a clear ERROR instead of
        vanishing. Retry count/delay are env-overridable.
        """
        attempts = max(1, int(os.getenv("GUI_BIND_RETRIES", "5")))
        delay = float(os.getenv("GUI_BIND_RETRY_SECONDS", "2"))
        for attempt in range(1, attempts + 1):
            try:
                self._app.run(
                    host=self._host,
                    port=self._port,
                    threaded=True,
                    use_reloader=False,
                    debug=False,
                )
                return  # run() only returns on a clean shutdown
            except OSError as exc:
                if attempt >= attempts:
                    logger.error(
                        "GUI server could not bind %s:%d after %d attempts: %s — "
                        "GUI unavailable this session",
                        self._host, self._port, attempts, exc,
                    )
                    return
                logger.warning(
                    "GUI bind attempt %d/%d failed (%s) — retrying in %.1fs",
                    attempt, attempts, exc, delay,
                )
                time.sleep(delay)

    def _remember(self, cache: list[dict], data: dict, key: "str | None") -> None:
        """Add ``data`` to a recent-events ``cache``, de-duplicated by identity.

        When ``key`` is given and ``data`` carries that key, an entry already in
        the cache with the same identity value is REPLACED in place — so a plane
        re-seen every poll, or a device re-seen every scan, occupies exactly one
        cache slot (its latest state) instead of accumulating a copy per
        sighting. When ``key`` is ``None`` (or absent from ``data``) the event is
        a discrete occurrence and is simply appended. The cache is bounded at
        ``_MAX_RECENT`` by evicting the oldest entry. Caller holds ``_data_lock``.
        """
        if key is not None:
            ident = data.get(key)
            if ident is not None:
                for i, existing in enumerate(cache):
                    if existing.get(key) == ident:
                        cache[i] = data
                        return
        cache.append(data)
        if len(cache) > _MAX_RECENT:
            cache.pop(0)

    # ------------------------------------------------------------------
    # Durable history (P5) — rebuild panels from on-disk session logs
    # ------------------------------------------------------------------

    def _sessions_root(self) -> "Path | None":
        """The ``data/sessions`` root, derived from the orchestrator's session dir.

        Returns ``None`` when there is no orchestrator or no session dir to read
        (tests, or a node that hasn't started a session) — the caller then falls
        back to the in-memory cache.
        """
        orch = self._orchestrator
        sdir = getattr(orch, "_session_dir", None) if orch is not None else None
        if sdir is None:
            return None
        try:
            return Path(sdir).parent
        except Exception:
            return None

    @staticmethod
    def _read_jsonl_tail(path: "Path", max_lines: int) -> list:
        """Up to the last *max_lines* parsed JSON objects from a ``.jsonl`` file.

        Memory is bounded by *max_lines* (a ``deque`` keeps only the tail).
        Tolerant of a torn final line — a power cut can leave a partial last
        record — and of any malformed line, which is skipped. Only ever called on
        a GUI load/refresh, never on the SSE hot path.
        """
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = deque(fh, maxlen=max_lines)
        except OSError:
            return []
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # torn or malformed line — skip
        return out

    def _history(self, filename: str, limit: int,
                 dedup_key: "str | None" = None) -> "list | None":
        """Rebuild a panel's history from on-disk session logs, newest-first.

        Walks session directories newest-first (bounded by
        ``_HISTORY_MAX_SESSIONS``), reading each session's *filename* until
        *limit* events are gathered — so a page refresh, a reconnect, or a service
        restart rebuilds the real history instead of the in-memory cache (empty
        after a restart, truncated at ``_MAX_RECENT`` otherwise). With *dedup_key*
        set, keeps only the latest record per identity (e.g. one row per MAC).
        Returns events oldest -> newest, or ``None`` when no session root is
        resolvable (caller falls back to the cache).
        """
        root = self._sessions_root()
        if root is None:
            return None
        try:
            sessions = sorted(
                (d for d in root.iterdir() if d.is_dir()),
                key=lambda d: d.name, reverse=True,
            )[:_HISTORY_MAX_SESSIONS]
        except OSError:
            return None

        events: list = []
        seen: set = set()
        for sess in sessions:
            if len(events) >= limit:
                break
            # newest-first within the file so dedup keeps the latest sighting
            for rec in reversed(self._read_jsonl_tail(sess / filename, limit)):
                if dedup_key is not None:
                    ident = rec.get(dedup_key)
                    if ident is not None:
                        if ident in seen:
                            continue
                        seen.add(ident)
                events.append(rec)
                if len(events) >= limit:
                    break
        events.reverse()  # oldest -> newest for the client
        return events

    def push_event(self, event_type: str, data: dict) -> None:
        """Broadcast a sensor event to all connected SSE clients.

        Thread-safe — may be called from the asyncio thread or any other thread.

        Args:
            event_type: One of ``wifi``, ``aircraft``, ``drone``, ``alert``,
                ``nearby``, ``survey`` (a nudge to refetch /api/survey).
            data:       Event dict (must be JSON-serialisable).
        """
        payload_dict = {"type": event_type, **data}
        try:
            payload = json.dumps(payload_dict, default=str)
        except Exception as exc:
            logger.debug("GUI push_event serialisation error: %s", exc)
            return

        # Update recent-events cache. Types with a stable identity (aircraft
        # ICAO, WiFi MAC) are de-duplicated: a re-sighting of the same entity
        # replaces its cached entry in place instead of appending a duplicate,
        # so /api/aircraft and /api/wifi return one row per entity (one track)
        # rather than one row per sighting. Keyless types just append.
        with self._data_lock:
            if event_type == "wifi":
                self._remember(self._recent_wifi, data, "mac")
            elif event_type == "aircraft":
                self._remember(self._recent_aircraft, data, "icao")
            elif event_type == "drone":
                self._remember(self._recent_drone, data, None)
            elif event_type == "ais":
                self._remember(self._recent_ais, data, "mmsi")
            elif event_type == "acars":
                self._remember(self._recent_acars, data, None)
            elif event_type == "alert":
                self._remember(self._recent_alerts, data, None)
            elif event_type == "nearby":
                # Only mobile nodes serve the Nearby tab; skip the cache on a
                # fixed node so the 200-slot _recent_nearby isn't occupied by
                # events no client reads. (SSE broadcast below still works — the
                # fixed-node app.js simply ignores the "nearby" type.)
                if self._node_mode == "mobile":
                    self._remember(self._recent_nearby, data, "mac")
            elif event_type == "remote_id":
                self._remember(self._recent_remote_id, data, "uas_id")

        # Broadcast to all SSE clients
        with self._clients_lock:
            dead: list[queue.Queue] = []
            for client_queue in self._clients:
                try:
                    client_queue.put_nowait(payload)
                except queue.Full:
                    dead.append(client_queue)
            for client_queue in dead:
                try:
                    self._clients.remove(client_queue)
                except ValueError:
                    pass

    def stop(self) -> None:
        """Signal all SSE clients to disconnect (sends None sentinel)."""
        with self._clients_lock:
            for client_queue in self._clients:
                try:
                    client_queue.put_nowait(None)
                except queue.Full:
                    pass
            self._clients.clear()
        logger.info("GUI server stopped")
