from __future__ import annotations

import html
import threading
import time
from typing import Any

import httpx

from assistant import ManicureAssistant
from config import Settings
from storage import Storage


class TelegramNotifier:
    def __init__(self, settings: Settings, storage: Storage, assistant: ManicureAssistant) -> None:
        self.settings = settings
        self.storage = storage
        self.assistant = assistant
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="telegram-outbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(5):
            self.process_due()

    def process_due(self) -> None:
        for lead in self.storage.claim_due_notifications():
            try:
                message_id = self.send(lead)
                self.storage.mark_notification_sent(lead["id"], message_id)
            except Exception as exc:  # outbox must survive all delivery errors
                self.storage.mark_notification_failed(
                    lead["id"], str(exc), self.settings.notification_max_attempts
                )

    def send(self, lead: dict[str, Any]) -> str:
        if not self.settings.telegram_bot_token or not self.settings.operator_chat_id:
            raise RuntimeError("Telegram не настроен")
        s = self.assistant.summary(lead["payload"])
        text = (
            f"<b>{html.escape(self.settings.telegram_test_prefix)}</b>\n"
            "💅 <b>Новая заявка из веб-чата</b>\n\n"
            f"<b>Услуга:</b> {html.escape(s['service'])}\n"
            f"<b>Дополнительно:</b> {html.escape(s['extras'])}\n"
            f"<b>Желаемая дата:</b> {html.escape(s['date'])}\n"
            f"<b>Часовой пояс:</b> {html.escape(s['timezone'])}\n"
            f"<b>Имя:</b> {html.escape(s['name'])}\n"
            f"<b>Контакт:</b> {html.escape(s['contact'])}\n\n"
            f"<code>{html.escape(lead['id'])}</code>\n"
            "Заявка учебная. Время требует подтверждения администратором."
        )
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        with httpx.Client(timeout=20) as client:
            response = client.post(
                url,
                json={
                    "chat_id": self.settings.operator_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            response.raise_for_status()
            data = response.json()
        if not data.get("ok"):
            raise RuntimeError("Telegram отклонил уведомление")
        return str(data["result"]["message_id"])
