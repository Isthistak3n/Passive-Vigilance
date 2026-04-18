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
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent
_MAX_RECENT = 200


class GUIServer:
    """Lightweight Flask server exposing live sensor data.

    Call :meth:`start` once to launch the server in a daemon thread.
    Call :meth:`push_event` from the orchestrator to broadcast events to SSE clients.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 5000,
        orchestrator=None,
    ) -> None:
        self._host = host
        self._port = port
        self._orchestrator = orchestrator  # weak back-reference for /api/status

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
            return jsonify({
                "session_id":   getattr(orch, "session_id", ""),
                "sensor_health": health,
                "stats":         stats,
                "gps_fix":       fix,
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

    def start(self) -> bool:
        """Start Flask in a daemon thread. Returns False if Flask is not installed."""
        if self._app is None:
            return False

        def _run():
            self._app.run(
                host=self._host,
                port=self._port,
                threaded=True,
                use_reloader=False,
                debug=False,
            )

        self._thread = threading.Thread(target=_run, daemon=True, name="gui-flask")
        self._thread.start()
        logger.info("GUI server started on http://%s:%d", self._host, self._port)
        return True

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
