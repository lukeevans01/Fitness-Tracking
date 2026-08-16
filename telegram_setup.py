#!/usr/bin/env python3
"""One-off helper to find your Telegram chat id and prove the bot can message you.

Run it, paste your bot token when prompted (it is not echoed, and never stored or
printed), and it will tell you exactly what to do next.

    python3 telegram_setup.py

The token is read from the TELEGRAM_BOT_TOKEN env var if set, otherwise prompted for.
Nothing is written to disk and the token never appears in output or shell history.
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys

API_BASE = "https://api.telegram.org"


def _curl_json(url: str, payload: "dict | None" = None) -> "tuple[int, dict]":
    """POST/GET `url` via curl and return (http_status, parsed_body)."""
    cmd = ["curl", "-s", "-w", "\nHTTP_STATUS:%{http_code}", url]
    kwargs: dict = {"capture_output": True, "text": True, "timeout": 20}
    if payload is not None:
        cmd += ["-X", "POST", "-H", "Content-Type: application/json", "--data-binary", "@-"]
        kwargs["input"] = json.dumps(payload)

    result = subprocess.run(cmd, **kwargs)
    raw = result.stdout
    status = 0
    if "HTTP_STATUS:" in raw:
        raw, _, tail = raw.rpartition("HTTP_STATUS:")
        try:
            status = int(tail.strip())
        except ValueError:
            status = 0
    try:
        body = json.loads(raw.strip() or "{}")
    except json.JSONDecodeError:
        body = {"_unparsed": raw.strip()[:400]}
    return status, body


def _explain_failure(status: int, body: dict) -> None:
    code = body.get("error_code", status)
    desc = (body.get("description") or "").strip()
    print(f"\n  Telegram said: {code} {desc or body}")
    if code == 401:
        print("\n  -> The token is wrong or incomplete.")
        print("     Re-copy it from @BotFather. It looks like 1234567890:AA... with no spaces.")
    elif code == 404:
        print("\n  -> The URL was not recognised, which normally means a malformed token.")
        print("     Check you copied the whole thing, including the part before the colon.")
    elif code == 409:
        print("\n  -> A webhook is currently active, which blocks getUpdates.")
        print(f"     Clear it with:  curl -s '{API_BASE}/bot<TOKEN>/deleteWebhook'")
    else:
        print("\n  -> Unexpected error. Check the token and your network, then retry.")


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    if not token:
        print("Paste your bot token from @BotFather (it will not be shown as you type).")
        try:
            token = getpass.getpass("Bot token: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
    if not token:
        print("No token given.")
        return 1

    # 1. Confirm the token works at all.
    print("\n[1/3] Checking the token...")
    status, body = _curl_json(f"{API_BASE}/bot{token}/getMe")
    if not body.get("ok"):
        _explain_failure(status, body)
        return 1
    bot = body["result"]
    username = bot.get("username", "?")
    print(f"      Token is valid. Bot is @{username}.")

    # 2. Look for messages sent to the bot.
    print("\n[2/3] Looking for messages you have sent to the bot...")
    status, body = _curl_json(f"{API_BASE}/bot{token}/getUpdates")
    if not body.get("ok"):
        _explain_failure(status, body)
        return 1

    chats = {}
    for update in body.get("result", []):
        msg = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or {}
        )
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            name = chat.get("username") or chat.get("first_name") or chat.get("title") or "?"
            chats[chat["id"]] = f"{name} ({chat.get('type', '?')})"

    if not chats:
        print("      No messages found. This is the usual sticking point.\n")
        print(f"      Open Telegram, search for  @{username}  , open the chat,")
        print("      press START (or just send 'hi'), then run this script again.\n")
        print("      Telegram only reveals your chat id once you have messaged the bot,")
        print("      and the bot is not allowed to message you until you do.")
        return 1

    print("      Found:")
    for chat_id, label in chats.items():
        print(f"        chat id {chat_id}   {label}")

    chat_id = next(iter(chats))
    if len(chats) > 1:
        print(f"\n      Using {chat_id}. If that is the wrong one, pick another from the list.")

    # 3. Prove the bot can actually message that chat.
    print(f"\n[3/3] Sending a test message to {chat_id}...")
    status, body = _curl_json(
        f"{API_BASE}/bot{token}/sendMessage",
        {"chat_id": chat_id, "text": "Test from your fitness bot. Setup is working."},
    )
    if not body.get("ok"):
        _explain_failure(status, body)
        if body.get("error_code") == 403:
            print(f"     Open the chat with @{username} and press START, then retry.")
        return 1

    print("      Sent. Check Telegram - you should have the message.")
    print("\n" + "=" * 62)
    print("Add these two GitHub repo secrets:")
    print("  TELEGRAM_BOT_TOKEN  = the token you just pasted")
    print(f"  TELEGRAM_CHAT_ID    = {chat_id}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
