from __future__ import annotations

from discord_bot.health import HealthState, create_health_app


def test_healthz_reports_the_login_state() -> None:
    state = HealthState(instance_id="abc123")
    client = create_health_app(state).test_client()

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "instance_id": "abc123",
        "login_state": "starting",
    }


def test_healthz_shows_a_pending_retry() -> None:
    state = HealthState(instance_id="abc123")
    state.set_state("retrying", retry_at=__import__("time").time() + 42)
    client = create_health_app(state).test_client()

    payload = client.get("/healthz").get_json()

    assert payload["login_state"] == "retrying"
    assert 0 < payload["retry_in_sec"] <= 42


def test_home_is_plain_text_and_state_updates() -> None:
    state = HealthState()
    client = create_health_app(state).test_client()

    assert client.get("/").get_data(as_text=True) == "Bot is running"

    state.set_state("connected")
    assert client.get("/healthz").get_json()["login_state"] == "connected"
