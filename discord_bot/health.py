"""Minimal health-check server for Render / Hugging Face Spaces.

The server lives in a daemon thread and reports the login state of the bot,
so a Discord rate-limit block is visible with a single curl and never
triggers a platform restart.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from flask import Flask, jsonify

logger = logging.getLogger(__name__)


class HealthState:
    """Mutable login state shared between the bot and the health thread."""

    __slots__ = ("instance_id", "login_state", "retry_at")

    def __init__(self, instance_id: str = "") -> None:
        self.instance_id = instance_id
        self.login_state = "starting"  # starting | logging_in | retrying | connected
        self.retry_at: float | None = None

    def set_state(self, state: str, retry_at: float | None = None) -> None:
        self.login_state = state
        self.retry_at = retry_at

    def snapshot(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "instance_id": self.instance_id,
            "login_state": self.login_state,
        }
        if self.retry_at is not None:
            payload["retry_in_sec"] = max(0, int(self.retry_at - time.time()))
        return payload


def create_health_app(state: HealthState) -> Flask:
    app = Flask(__name__)
    app.logger.setLevel(logging.WARNING)

    @app.route("/")
    def home() -> str:
        return "Bot is running"

    @app.route("/healthz")
    def healthz():
        return jsonify({"status": "ok", **state.snapshot()})

    return app


def start_health_server(port: int, state: HealthState) -> threading.Thread:
    """Start the health server in a daemon thread and return that thread."""

    app = create_health_app(state)

    def run() -> None:
        try:
            app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
        except Exception:
            logger.exception("Не удалось запустить health-сервер на порту %s", port)

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    thread = threading.Thread(target=run, name="health-server", daemon=True)
    thread.start()
    logger.info("Health-сервер запущен на порту %s", port)
    return thread
