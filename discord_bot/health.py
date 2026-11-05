"""Minimal health-check server for Render / Hugging Face Spaces."""

from __future__ import annotations

import logging
import threading

from flask import Flask, jsonify

logger = logging.getLogger(__name__)


def create_health_app() -> Flask:
    app = Flask(__name__)
    app.logger.setLevel(logging.WARNING)

    @app.route("/")
    def home() -> str:
        return "Bot is running"

    @app.route("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    return app


def start_health_server(port: int) -> threading.Thread:
    """Start the health server in a daemon thread and return that thread."""

    app = create_health_app()

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
