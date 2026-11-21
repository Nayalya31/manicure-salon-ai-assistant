from __future__ import annotations

import hmac
import secrets
from datetime import timedelta
from functools import wraps
from typing import Any, Callable

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from assistant import AssistantUnavailable, ManicureAssistant
from config import settings
from notifier import TelegramNotifier
from storage import Storage


settings.ensure_paths()
storage = Storage(settings.database_path)
storage.init()
assistant = ManicureAssistant(settings)
notifier = TelegramNotifier(settings, storage, assistant)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=settings.secret_key,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=settings.app_env == "production",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=32 * 1024,
)


@app.before_request
def ensure_session() -> None:
    session.permanent = True
    session.setdefault("csrf_token", secrets.token_urlsafe(24))


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
    )
    if settings.app_env == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def _csrf_ok() -> bool:
    return hmac.compare_digest(
        str(session.get("csrf_token", "")), str(request.headers.get("X-CSRF-Token", ""))
    )


def _conversation() -> dict[str, Any] | None:
    conversation_id = session.get("conversation_id")
    return storage.get_conversation(conversation_id) if conversation_id else None


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() or request.remote_addr or "unknown"


def admin_required(view: Callable):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@app.get("/")
def index():
    kb = assistant.knowledge()
    return render_template(
        "index.html",
        knowledge=kb,
        model=settings.openai_model,
        timezone=settings.salon_timezone,
        csrf_token=session["csrf_token"],
    )


@app.get("/healthz")
def healthz():
    with storage.connect() as conn:
        conn.execute("SELECT 1").fetchone()
    return jsonify(status="ok", ai_configured=bool(settings.openai_api_key))


@app.post("/api/conversations")
def new_conversation():
    if not _csrf_ok():
        return jsonify(error="Сессия устарела. Обновите страницу."), 403
    conversation_id = storage.create_conversation()
    session["conversation_id"] = conversation_id
    return jsonify(
        conversation_id=conversation_id,
        greeting=(
            "Здравствуйте! Я учебный ИИ-ассистент вымышленного салона «Лак & Логика». "
            "Отвечу по демонстрационным услугам и помогу собрать тестовую заявку. "
            "Какой вопрос разберём?"
        ),
    )


@app.post("/api/chat")
def chat():
    if not _csrf_ok():
        return jsonify(error="Сессия устарела. Обновите страницу."), 403
    conversation = _conversation()
    if not conversation:
        return jsonify(error="Начните новый диалог."), 409
    if conversation.get("submitted_lead_id"):
        return jsonify(error="Эта заявка уже отправлена. Начните новый диалог."), 409
    if not storage.allow_rate(f"chat:{_client_ip()}:{conversation['id']}", settings.rate_limit_per_minute):
        return jsonify(error="Слишком много сообщений. Подождите минуту и попробуйте снова."), 429
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message", "")).strip()
    if not message:
        return jsonify(error="Введите сообщение."), 400
    if len(message) > settings.max_message_length:
        return jsonify(error=f"Сообщение длиннее {settings.max_message_length} символов."), 400
    try:
        reply, state, awaiting_confirmation = assistant.process(
            message,
            conversation["state"],
            storage.history(conversation["id"], settings.max_history_messages),
        )
    except AssistantUnavailable as exc:
        return jsonify(error=str(exc), retryable=True), 503
    storage.save_turn(conversation["id"], message, reply, state, awaiting_confirmation)
    result = {
        "reply": reply,
        "awaiting_confirmation": awaiting_confirmation,
        "state": state,
    }
    if awaiting_confirmation:
        result["summary"] = assistant.summary(state)
    return jsonify(result)


@app.post("/api/submit")
def submit():
    if not _csrf_ok():
        return jsonify(error="Сессия устарела. Обновите страницу."), 403
    conversation = _conversation()
    if not conversation:
        return jsonify(error="Диалог не найден."), 404
    errors = assistant.validate_for_submit(conversation["state"])
    if errors or not conversation["awaiting_confirmation"]:
        return jsonify(error=errors[0] if errors else "Сначала подтвердите состав заявки в диалоге."), 422
    payload = request.get_json(silent=True) or {}
    idempotency_key = str(payload.get("idempotency_key", "")).strip()
    if not idempotency_key or len(idempotency_key) > 100:
        return jsonify(error="Некорректный ключ подтверждения."), 400
    lead, created = storage.submit_lead(conversation["id"], idempotency_key)
    # Try immediately; persistent outbox will retry independently on failure.
    notifier.process_due()
    refreshed = next((item for item in storage.list_leads() if item["id"] == lead["id"]), lead)
    return jsonify(
        lead_id=lead["id"],
        created=created,
        notification_status=refreshed["notification_status"],
        message="Заявка принята. Время посещения требует подтверждения администратором.",
    )


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if not hmac.compare_digest(
            str(session.get("csrf_token", "")), str(request.form.get("csrf_token", ""))
        ):
            return render_template("admin_login.html", error="Сессия устарела. Обновите страницу."), 403
        if not storage.allow_rate(f"admin-login:{_client_ip()}", 8):
            return render_template("admin_login.html", error="Слишком много попыток. Подождите минуту."), 429
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if hmac.compare_digest(username, settings.admin_username) and hmac.compare_digest(
            password, settings.admin_password
        ):
            session["admin_authenticated"] = True
            return redirect(url_for("admin_leads"))
        error = "Неверный логин или пароль."
    return render_template("admin_login.html", error=error)


@app.get("/admin/leads")
@admin_required
def admin_leads():
    return render_template("admin_leads.html", leads=storage.list_leads(), assistant=assistant)


@app.post("/admin/logout")
@admin_required
def admin_logout():
    if not hmac.compare_digest(
        str(session.get("csrf_token", "")), str(request.form.get("csrf_token", ""))
    ):
        return "Forbidden", 403
    session.pop("admin_authenticated", None)
    return redirect(url_for("index"))


notifier.start()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, debug=settings.app_env != "production")
