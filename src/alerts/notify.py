"""Email (SMTP) and Telegram alerts. Credentials come from environment variables only."""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

import httpx

DIGEST_TYPES = {"price_drop", "new_offer", "new_products", "undercut", "you_out_of_stock"}
ONLY_WHEN_NEW = {"undercut", "you_out_of_stock"}  # these persist between runs; only report them the first time they appear


class NotifyError(Exception):
    pass


def alert_key(a: dict) -> str:
    return f"{a['type']}|{a['site']}|{a.get('product', '')}"


def build_digest(result: dict, source_name: str, analysis_id: str, previous_keys: set[str], base_url: str | None = None, limit: int = 12) -> tuple[str, str] | None:
    items = [a for a in result.get("alerts", []) if a["type"] in DIGEST_TYPES and not (a["type"] in ONLY_WHEN_NEW and alert_key(a) in previous_keys)]
    if not items:
        return None
    lines = [f"- {a['message']}" for a in items[:limit]]
    if len(items) > limit:
        lines.append(f"...and {len(items) - limit} more")
    body = f"Competitor price intelligence for {source_name}\n\n" + "\n".join(lines)
    if base_url:
        body += f"\n\nFull report: {base_url.rstrip('/')}/#a={analysis_id}"
    return f"{len(items)} competitor update(s) for {source_name}", body


def send_email(to: str, subject: str, body: str, env=None) -> None:
    env = env if env is not None else os.environ
    host = env.get("SMTP_HOST", "").strip()
    if not host:
        raise NotifyError("SMTP is not configured (set SMTP_HOST, SMTP_USER, SMTP_PASSWORD)")
    port = int(env.get("SMTP_PORT", "587") or 587)
    user, pwd = env.get("SMTP_USER", ""), env.get("SMTP_PASSWORD", "")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = env.get("SMTP_FROM") or user or "alerts@localhost", to, subject
    msg.set_content(body)
    try:
        with (smtplib.SMTP_SSL(host, port, timeout=20) if port == 465 else smtplib.SMTP(host, port, timeout=20)) as smtp:
            if port != 465:
                smtp.starttls()
            if user:
                smtp.login(user, pwd)
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise NotifyError(f"email failed: {type(exc).__name__}") from None


def send_telegram(chat_id: str, text: str, env=None, transport: httpx.BaseTransport | None = None) -> None:
    env = env if env is not None else os.environ
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise NotifyError("Telegram is not configured (set TELEGRAM_BOT_TOKEN)")
    try:
        r = httpx.Client(transport=transport, timeout=20).post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text[:4000]})
    except httpx.TransportError as exc:
        raise NotifyError(f"telegram failed: {type(exc).__name__}") from None  # never include the URL: it contains the bot token
    if r.status_code != 200 or not r.json().get("ok"):
        raise NotifyError(f"telegram failed: HTTP {r.status_code}")


def send_all(email: str | None, telegram_chat: str | None, subject: str, body: str, env=None, transport=None) -> list[dict]:
    out = []
    for channel, target, fn in (("email", email, lambda: send_email(email, subject, body, env)), ("telegram", telegram_chat, lambda: send_telegram(telegram_chat, f"{subject}\n\n{body}", env, transport))):
        if not target:
            continue
        try:
            fn()
            out.append({"channel": channel, "status": "sent", "error": None})
        except NotifyError as exc:
            out.append({"channel": channel, "status": "failed", "error": str(exc)})
    return out
