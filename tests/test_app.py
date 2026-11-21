from __future__ import annotations

import importlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    import config
    import storage
    import assistant
    import notifier
    import app

    importlib.reload(config)
    importlib.reload(storage)
    importlib.reload(assistant)
    importlib.reload(notifier)
    module = importlib.reload(app)
    module.app.config.update(TESTING=True)
    return module, module.app.test_client()


def _csrf(client):
    client.get("/")
    with client.session_transaction() as sess:
        return sess["csrf_token"]


def test_submission_is_idempotent_and_persistent(client, monkeypatch):
    module, web = client
    csrf = _csrf(web)
    response = web.post("/api/conversations", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    future = (datetime.now(ZoneInfo(module.settings.salon_timezone)) + timedelta(days=3)).replace(
        hour=15, minute=0, second=0, microsecond=0
    )
    state = {
        "service_id": "manicure_coating",
        "removal": True,
        "strengthening": False,
        "design": False,
        "desired_start": future.isoformat(),
        "name": "Тест",
        "contact": "@test_guest",
    }
    with web.session_transaction() as sess:
        conversation_id = sess["conversation_id"]
    module.storage.save_turn(conversation_id, "данные", "проверьте", state, True)
    monkeypatch.setattr(module.notifier, "process_due", lambda: None)
    headers = {"X-CSRF-Token": csrf}
    first = web.post("/api/submit", json={"idempotency_key": "same-key"}, headers=headers)
    second = web.post("/api/submit", json={"idempotency_key": "same-key"}, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json["created"] is True
    assert second.json["created"] is False
    assert len(module.storage.list_leads()) == 1
    reloaded = module.Storage(module.settings.database_path)
    reloaded.init()
    assert len(reloaded.list_leads()) == 1


def test_two_dialogues_are_isolated(client):
    module, _ = client
    one = module.storage.create_conversation()
    two = module.storage.create_conversation()
    module.storage.save_turn(one, "Я Анна", "Принято", {"name": "Анна"}, False)
    assert module.storage.get_conversation(one)["state"]["name"] == "Анна"
    assert module.storage.get_conversation(two)["state"] == {}


def test_server_rejects_incomplete_submission(client):
    module, web = client
    csrf = _csrf(web)
    web.post("/api/conversations", headers={"X-CSRF-Token": csrf})
    response = web.post(
        "/api/submit", json={"idempotency_key": "key"}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 422


def test_ai_failure_does_not_persist_fake_turn(client, monkeypatch):
    module, web = client
    csrf = _csrf(web)
    web.post("/api/conversations", headers={"X-CSRF-Token": csrf})
    from assistant import AssistantUnavailable

    monkeypatch.setattr(
        module.assistant, "process", lambda *args: (_ for _ in ()).throw(AssistantUnavailable("нет связи"))
    )
    response = web.post(
        "/api/chat", json={"message": "Привет"}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 503
    with web.session_transaction() as sess:
        conversation_id = sess["conversation_id"]
    assert module.storage.history(conversation_id, 10) == []


def test_telegram_failure_keeps_saved_lead_for_retry(client, monkeypatch):
    module, web = client
    csrf = _csrf(web)
    web.post("/api/conversations", headers={"X-CSRF-Token": csrf})
    future = (datetime.now(ZoneInfo(module.settings.salon_timezone)) + timedelta(days=2)).replace(
        hour=14, minute=0, second=0, microsecond=0
    )
    state = {
        "service_id": "manicure_no_coating",
        "removal": False,
        "strengthening": False,
        "design": False,
        "desired_start": future.isoformat(),
        "name": "Тест",
        "contact": "@test_guest",
    }
    with web.session_transaction() as sess:
        conversation_id = sess["conversation_id"]
    module.storage.save_turn(conversation_id, "готово", "проверьте", state, True)
    lead, _ = module.storage.submit_lead(conversation_id, "telegram-failure")
    monkeypatch.setattr(module.notifier, "send", lambda _lead: (_ for _ in ()).throw(RuntimeError("offline")))
    module.notifier.process_due()
    saved = next(item for item in module.storage.list_leads() if item["id"] == lead["id"])
    assert saved["notification_status"] == "retry"
    assert saved["notification_attempts"] == 1
    assert saved["payload"]["contact"] == "@test_guest"
