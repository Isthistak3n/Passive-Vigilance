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
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent
_MAX_RECENT = 200

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
    ) -> None:
        self._host = host
        self._port = port
        self._orchestrator = orchestrator  # weak back-reference for /api/status
        # .env path the mode toggle writes to; overridable for tests.
        self._env_path = Path(env_path) if env_path else _DEFAULT_ENV_PATH

        self._gui_token: str = os.getenv("GUI_TOKEN", "").strip()

        self._clients: list[queue.Queue] = []
        self._clients_lock = threading.Lock()

        self._recent_wifi: list[dict] = []
        self._recent_aircraft: list[dict] = []
        self._recent_drone: list[dict] = []
        self._recent_alerts: list[dict] = []

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

        @app.before_request
        def check_auth():
            if not gui_token:
                return  # no auth configured — open access
            from flask import request as _req
            auth = _req.headers.get("Authorization", "")
            token_param = _req.args.get("token", "")
            if auth == f"Bearer {gui_token}" or token_param == gui_token:
                return  # authorized
            return jsonify({"error": "Unauthorized"}), 401

        @app.route("/")
        def index():
            return render_template("index.html")

        @app.route("/api/status")
        def api_status():
            orch = self._orchestrator
            if orch is None:
                return jsonify({"status": "no_orchestrator"})
            health = dict(getattr(orch, "_sensor_health", {}))
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
                "stats":         stats,
                "gps_fix":       fix,
                "scoring":       scoring,
            })

        @app.route("/api/wifi")
        def api_wifi():
            with self._data_lock:
                return jsonify(list(self._recent_wifi))

        @app.route("/api/aircraft")
        def api_aircraft():
            with self._data_lock:
                return jsonify(list(self._recent_aircraft))

        @app.route("/api/drone")
        def api_drone():
            with self._data_lock:
                return jsonify(list(self._recent_drone))

        @app.route("/api/alerts")
        def api_alerts():
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

        # TODO(remote-id): Add /api/remote_id endpoint and a Remote ID tab in
        # index.html once RemoteIDModule is wired into the SSE push_event stream.

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

    def push_event(self, event_type: str, data: dict) -> None:
        """Broadcast a sensor event to all connected SSE clients.

        Thread-safe — may be called from the asyncio thread or any other thread.

        Args:
            event_type: One of ``wifi``, ``aircraft``, ``drone``, ``alert``.
            data:       Event dict (must be JSON-serialisable).
        """
        payload_dict = {"type": event_type, **data}
        try:
            payload = json.dumps(payload_dict, default=str)
        except Exception as exc:
            logger.debug("GUI push_event serialisation error: %s", exc)
            return

        # Update recent-events cache
        with self._data_lock:
            if event_type == "wifi":
                self._recent_wifi.append(data)
                if len(self._recent_wifi) > _MAX_RECENT:
                    self._recent_wifi.pop(0)
            elif event_type == "aircraft":
                self._recent_aircraft.append(data)
                if len(self._recent_aircraft) > _MAX_RECENT:
                    self._recent_aircraft.pop(0)
            elif event_type == "drone":
                self._recent_drone.append(data)
                if len(self._recent_drone) > _MAX_RECENT:
                    self._recent_drone.pop(0)
            elif event_type == "alert":
                self._recent_alerts.append(data)
                if len(self._recent_alerts) > _MAX_RECENT:
                    self._recent_alerts.pop(0)

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
