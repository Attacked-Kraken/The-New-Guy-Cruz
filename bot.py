#!/usr/bin/env python3
"""The New Guy Cruz — shared Grok/xAI client + CLI chat.

Secrets come from environment / .env only. Never hardcode API keys.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

SYSTEM_PERSONA = (
    "You are The New Guy Cruz — a sharp, capable AI assistant with a bit of "
    "edge and zero corporate fluff. Be helpful, direct, and clear. Prefer "
    "actionable answers over lectures. Match the user's energy; keep humor "
    "dry when it fits. Admit uncertainty instead of bluffing. When the user "
    "asks for code or steps, give them cleanly."
)

DEFAULT_MODEL = "grok-3"
DEFAULT_BASE_URL = "https://api.x.ai/v1"
PREVIEW_CHARS = 800
TELEGRAM_MAX_MESSAGE = 4096


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    val = os.getenv(name)
    if val is None or not str(val).strip():
        return default
    return str(val).strip()


def get_settings() -> Dict[str, Any]:
    api_key = _env("XAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "XAI_API_KEY is not set. Copy .env.example to .env and add your key "
            "from https://console.x.ai/"
        )
    try:
        temperature = float(_env("TEMPERATURE", "0.7") or "0.7")
    except ValueError:
        temperature = 0.7
    try:
        max_history = int(_env("MAX_HISTORY", "20") or "20")
    except ValueError:
        max_history = 20
    return {
        "api_key": api_key,
        "model": _env("XAI_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL,
        "base_url": _env("XAI_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL,
        "temperature": temperature,
        "max_history": max(2, max_history),
    }


def make_client(settings: Optional[Dict[str, Any]] = None) -> OpenAI:
    s = settings or get_settings()
    return OpenAI(api_key=s["api_key"], base_url=s["base_url"])


# Bounded conversation memory: chat_id -> list of {role, content}
_histories: Dict[str, List[Dict[str, str]]] = defaultdict(list)


def reset_history(chat_id: str) -> None:
    _histories.pop(str(chat_id), None)


def get_history(chat_id: str) -> List[Dict[str, str]]:
    return list(_histories.get(str(chat_id), []))


def _trim_history(chat_id: str, max_history: int) -> None:
    """Keep at most max_history user/assistant turns (2 messages each)."""
    key = str(chat_id)
    hist = _histories[key]
    max_msgs = max_history * 2
    if len(hist) > max_msgs:
        _histories[key] = hist[-max_msgs:]


def ask_grok(
    user_message: str,
    *,
    chat_id: str = "cli",
    history: Optional[List[Dict[str, str]]] = None,
    system: Optional[str] = None,
    client: Optional[OpenAI] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Send a user message to Grok with multi-turn memory and return the reply text."""
    s = settings or get_settings()
    cli = client or make_client(s)
    key = str(chat_id)

    if history is not None:
        turn_history = list(history)
    else:
        turn_history = list(_histories[key])

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system or SYSTEM_PERSONA},
        *turn_history,
        {"role": "user", "content": user_message},
    ]

    resp = cli.chat.completions.create(
        model=s["model"],
        messages=messages,
        temperature=s["temperature"],
    )
    reply = (resp.choices[0].message.content or "").strip()
    if not reply:
        reply = "(empty reply from model)"

    if history is None:
        _histories[key].append({"role": "user", "content": user_message})
        _histories[key].append({"role": "assistant", "content": reply})
        _trim_history(key, s["max_history"])

    return reply


def preview_text(full: str, limit: int = PREVIEW_CHARS) -> str:
    """First ~limit chars, break on whitespace when possible."""
    text = full.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # Prefer breaking at last newline or space in the tail
    for sep in ("\n", " "):
        idx = cut.rfind(sep)
        if idx >= limit // 2:
            cut = cut[:idx]
            break
    return cut.rstrip() + "…"


def needs_collapse(full: str, limit: int = PREVIEW_CHARS) -> bool:
    return len(full.strip()) > limit


def cli_main() -> int:
    print("The New Guy Cruz — CLI (xAI / Grok)")
    print("Commands: /reset  /quit")
    print("-" * 40)
    try:
        settings = get_settings()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    client = make_client(settings)
    print(f"Model: {settings['model']} @ {settings['base_url']}")
    print()

    while True:
        try:
            line = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return 0
        if not line:
            continue
        if line.lower() in {"/quit", "/exit", "quit", "exit"}:
            print("Bye.")
            return 0
        if line.lower() in {"/reset", "reset"}:
            reset_history("cli")
            print("(history cleared)\n")
            continue
        try:
            reply = ask_grok(line, chat_id="cli", client=client, settings=settings)
        except Exception as exc:  # noqa: BLE001 — surface API errors to CLI
            print(f"Error: {exc}\n", file=sys.stderr)
            continue
        print(f"Cruz> {reply}\n")


if __name__ == "__main__":
    raise SystemExit(cli_main())
