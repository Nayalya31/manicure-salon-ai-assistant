from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "development")
    secret_key: str = os.getenv("SECRET_KEY", "development-only-change-me")
    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "change-me")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080")
    database_path: str = os.getenv("DATABASE_PATH", "data/manicure_assistant.db")
    salon_timezone: str = os.getenv("SALON_TIMEZONE", "Asia/Novosibirsk")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-6-luna")
    openai_timeout_seconds: int = _int("OPENAI_TIMEOUT_SECONDS", 35)
    openai_max_output_tokens: int = _int("OPENAI_MAX_OUTPUT_TOKENS", 650)
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    operator_chat_id: str = os.getenv("OPERATOR_CHAT_ID", "")
    telegram_test_prefix: str = os.getenv("TELEGRAM_TEST_PREFIX", "ТЕСТ ПОРТФОЛИО")
    rate_limit_per_minute: int = _int("RATE_LIMIT_PER_MINUTE", 12)
    max_message_length: int = _int("MAX_MESSAGE_LENGTH", 1200)
    max_history_messages: int = _int("MAX_HISTORY_MESSAGES", 16)
    notification_max_attempts: int = _int("NOTIFICATION_MAX_ATTEMPTS", 8)

    def ensure_paths(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)


settings = Settings()

