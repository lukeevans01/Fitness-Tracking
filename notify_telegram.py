#!/usr/bin/env python3
"""Outbound Telegram notifications — a second transport for the daily/Sunday messages.

Deliberately send-only. Inbound replies still arrive by email via process_replies.py;
this module exists so the same rendered text also lands as a phone notification.

Sending is best-effort by design: a Telegram failure must never fail a run whose email
already went out, so every entry point returns a bool and never raises.

Env vars:
  TELEGRAM_BOT_TOKEN   bot token from @BotFather
  TELEGRAM_CHAT_ID     the chat to post to (your own user id for a private chat)

Both must be set or sending is skipped. As with send_daily.py we shell out to curl
rather than using urllib.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

API_BASE = "https://api.telegram.org"

# Telegram rejects messages over 4096 characters, so long bodies are split. The margin
# leaves room for the part counter appended to each chunk.
MAX_MESSAGE_CHARS = 4096
CHUNK_CHARS = 3900

TIMEOUT_SECONDS = 20


def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN") or ""


def _chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID") or ""


def is_configured() -> bool:
    """True when both the bot token and chat id are present."""
    return bool(_token() and _chat_id())


def split_message(text: str, limit: int = CHUNK_CHARS) -> "list[str]":
    """Split `text` into chunks within `limit`, preferring line boundaries.

    A single line longer than the limit is hard-split, so the result is always within
    the limit regardless of input.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: "list[str]" = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks


def inline_keyboard(rows: "list[list[tuple[str, str]]]") -> dict:
    """Build reply_markup from rows of (label, callback_data).

    Telegram caps callback_data at 64 bytes, so keep it terse: "wk:B", "fb:2026-08-17:done".
    """
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in rows
        ]
    }


def _api(method: str, payload: dict) -> "tuple[bool, dict]":
    """Call a Bot API method with curl. Returns (ok, parsed_body)."""
    url = f"{API_BASE}/bot{_token()}/{method}"
    try:
        result = subprocess.run(
            [
                "curl", "-s", "-w", "\nHTTP_STATUS:%{http_code}\n",
                "-X", "POST", url,
                "-H", "Content-Type: application/json",
                "--data-binary", "@-",
            ],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(f"[telegram] {method} timed out", file=sys.stderr)
        return False, {}
    except Exception as exc:
        print(f"[telegram] {method} failed: {exc}", file=sys.stderr)
        return False, {}

    ok = "HTTP_STATUS:200" in result.stdout
    if not ok:
        # The token is in the URL, so never echo the request - only Telegram's reply.
        print(f"[telegram] {method} failed: {result.stdout.strip()[:400]}", file=sys.stderr)
    body = {}
    try:
        body = json.loads(result.stdout.split("HTTP_STATUS:")[0].strip() or "{}")
    except json.JSONDecodeError:
        pass
    return ok, body


def answer_callback_query(callback_id: str, text: str = "") -> bool:
    """Stop the button spinner. Must happen quickly or Telegram shows a timeout."""
    if not is_configured() or not callback_id:
        return False
    ok, _ = _api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})
    return ok


def clear_buttons(message_id: "int | None") -> bool:
    """Remove the inline keyboard from a message, so an option cannot be tapped twice."""
    if not is_configured() or not message_id:
        return False
    ok, _ = _api("editMessageReplyMarkup", {
        "chat_id": _chat_id(), "message_id": message_id, "reply_markup": {"inline_keyboard": []},
    })
    return ok


def _post_send_message(text: str, reply_markup: "dict | None" = None) -> bool:
    payload = {
        "chat_id": _chat_id(),
        "text": text,
        # The plan text has no links worth unfurling, and previews add noise.
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    url = f"{API_BASE}/bot{_token()}/sendMessage"
    try:
        result = subprocess.run(
            [
                "curl", "-s", "-w", "\nHTTP_STATUS:%{http_code}\n",
                "-X", "POST", url,
                "-H", "Content-Type: application/json",
                # Body over stdin so a long session never hits an argv limit.
                "--data-binary", "@-",
            ],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print("[telegram] send timed out", file=sys.stderr)
        return False
    except Exception as exc:  # curl missing, etc. Never fatal.
        print(f"[telegram] send failed: {exc}", file=sys.stderr)
        return False

    ok = "HTTP_STATUS:200" in result.stdout
    if not ok:
        # The token is in the URL, so never echo the request — only Telegram's reply.
        print(f"[telegram] send failed: {result.stdout.strip()[:400]}", file=sys.stderr)
        if result.stderr:
            print(f"[telegram] curl stderr: {result.stderr.strip()[:200]}", file=sys.stderr)
    return ok


def send_message(text: str, reply_markup: "dict | None" = None) -> bool:
    """Send `text` to the configured chat, splitting if needed.

    Returns True only if every chunk was accepted. Returns False (without raising) when
    unconfigured, so callers can treat Telegram as optional.
    """
    if not is_configured():
        print("[telegram] skipped: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.")
        return False

    chunks = split_message(text)
    if not chunks:
        return False

    total = len(chunks)
    all_ok = True
    for i, chunk in enumerate(chunks, 1):
        body = chunk if total == 1 else f"{chunk}\n\n({i}/{total})"
        # Buttons go on the last chunk only, so they sit at the bottom of the thread.
        markup = reply_markup if i == total else None
        if not _post_send_message(body, markup):
            all_ok = False
            # Stop after a failure rather than spamming the rest of a broken send.
            break
    if all_ok:
        print(f"[telegram] sent ({total} message{'s' if total > 1 else ''}).")
    return all_ok


def notify(subject: str, text: str, reply_markup: "dict | None" = None) -> bool:
    """Send a subject-prefixed message, mirroring how the email is titled."""
    heading = (subject or "").strip()
    body = (text or "").strip()
    return send_message(f"{heading}\n\n{body}" if heading else body, reply_markup)
