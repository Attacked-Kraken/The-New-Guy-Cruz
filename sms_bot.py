#!/usr/bin/env python3
"""Optional SMS front-end for The New Guy Cruz (Twilio).

Requires TWILIO_* env vars. Same ask_grok core as Telegram/CLI.
This is a paid secondary path — Telegram is the recommended free UX.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from dotenv import load_dotenv

from bot import ask_grok, get_settings, make_client, reset_history

load_dotenv()

CHAT_ID = "sms"


def _twilio_ready() -> tuple[bool, str]:
    needed = (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
        "SMS_TO_NUMBER",
    )
    missing = [k for k in needed if not (os.getenv(k) or "").strip()]
    if missing:
        return False, (
            "Twilio SMS is not configured. Missing env vars:\n  - "
            + "\n  - ".join(missing)
            + "\n\nCopy .env.example → .env and fill TWILIO_* / SMS_TO_NUMBER, "
            "or use Telegram instead:\n  python telegram_bot.py"
        )
    return True, ""


def send_sms(body: str) -> str:
    ok, err = _twilio_ready()
    if not ok:
        raise RuntimeError(err)

    try:
        from twilio.rest import Client as TwilioClient
    except ImportError as exc:
        raise RuntimeError(
            "twilio package not installed. Optional SMS only:\n"
            "  pip install twilio\n"
            "Or use Telegram: python telegram_bot.py"
        ) from exc

    account_sid = os.environ["TWILIO_ACCOUNT_SID"].strip()
    auth_token = os.environ["TWILIO_AUTH_TOKEN"].strip()
    from_number = os.environ["TWILIO_FROM_NUMBER"].strip()
    to_number = os.environ["SMS_TO_NUMBER"].strip()

    client = TwilioClient(account_sid, auth_token)
    # Twilio SMS soft limit ~1600 chars for concatenated messages; keep a margin.
    text = body if len(body) <= 1500 else body[:1499] + "…"
    message = client.messages.create(body=text, from_=from_number, to=to_number)
    return message.sid


def reply_via_sms(user_message: str) -> str:
    """Ask Grok and send the reply over SMS. Returns Twilio message SID."""
    settings = get_settings()
    client = make_client(settings)
    reply = ask_grok(
        user_message,
        chat_id=CHAT_ID,
        client=client,
        settings=settings,
    )
    return send_sms(reply)


def cli_main() -> int:
    print("The New Guy Cruz — SMS stub (Twilio optional)")
    print("Commands: /reset  /quit")
    print("-" * 40)

    ok, err = _twilio_ready()
    if not ok:
        print(err, file=sys.stderr)
        return 1

    try:
        settings = get_settings()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    client = make_client(settings)
    print(f"Model: {settings['model']}")
    print(f"SMS to: {os.environ['SMS_TO_NUMBER'].strip()}")
    print()

    while True:
        try:
            line = input("You (SMS)> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return 0
        if not line:
            continue
        if line.lower() in {"/quit", "/exit", "quit", "exit"}:
            print("Bye.")
            return 0
        if line.lower() in {"/reset", "reset"}:
            reset_history(CHAT_ID)
            print("(history cleared)\n")
            continue
        try:
            reply = ask_grok(
                line, chat_id=CHAT_ID, client=client, settings=settings
            )
            sid = send_sms(reply)
            print(f"Cruz> {reply}\n(sent SMS sid={sid})\n")
        except Exception as exc:  # noqa: BLE001
            print(f"Error: {exc}\n", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(cli_main())
