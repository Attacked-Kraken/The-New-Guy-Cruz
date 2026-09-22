#!/usr/bin/env python3
"""2-way Telegram chat with The New Guy Cruz (Grok / xAI).

Long replies collapse to ~800 chars with inline ▼ Show more / ▲ Hide,
same pattern as the trading-bot status/PnL expand buttons.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Dict, Optional, Set

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import (
    TELEGRAM_MAX_MESSAGE,
    ask_grok,
    get_settings,
    make_client,
    needs_collapse,
    preview_text,
    reset_history,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram_bot")

EXPAND_CB = "msg:expand"
COLLAPSE_CB = "msg:collapse"

# message_id -> {"full": str, "preview": str, "chat_id": int}
_collapse_store: Dict[int, Dict[str, object]] = {}


def _allowed_chat_ids() -> Optional[Set[int]]:
    raw = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not raw:
        return None
    ids: Set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logger.warning("Ignoring invalid TELEGRAM_CHAT_ID entry: %r", part)
    return ids or None


def _authorized(update: Update) -> bool:
    allow = _allowed_chat_ids()
    if allow is None:
        return True
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id if chat else None
    user_id = user.id if user else None
    return (chat_id in allow) or (user_id in allow)


def _expand_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("▼ Show more", callback_data=EXPAND_CB)]]
    )


def _collapse_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("▲ Hide", callback_data=COLLAPSE_CB)]]
    )


def _cap(text: str, limit: int = TELEGRAM_MAX_MESSAGE) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    reset_history(chat_id)
    await update.effective_message.reply_text(
        "Hey — I'm *The New Guy Cruz*.\n"
        "Chat normally and I'll reply via Grok/xAI.\n\n"
        "Commands:\n"
        "/start — greet + reset memory\n"
        "/reset — clear conversation memory\n"
        "/help — this help\n\n"
        "Long replies collapse with ▼ Show more.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    await update.effective_message.reply_text(
        "The New Guy Cruz — Telegram ↔ Grok\n\n"
        "/start — greet + reset memory\n"
        "/reset — clear conversation memory\n"
        "/help — show this message\n\n"
        "Just send a text message for a multi-turn reply.\n"
        "Long answers start collapsed (~800 chars); tap ▼ Show more / ▲ Hide."
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    reset_history(chat_id)
    await update.effective_message.reply_text("Conversation memory cleared. Fresh start.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    msg = update.effective_message
    if not msg or not msg.text:
        return

    chat_id = str(update.effective_chat.id)
    user_text = msg.text.strip()
    if not user_text:
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    settings = context.application.bot_data["settings"]
    client = context.application.bot_data["client"]

    try:
        reply = ask_grok(
            user_text,
            chat_id=chat_id,
            client=client,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("ask_grok failed")
        await msg.reply_text(f"Grok error: {exc}")
        return

    if needs_collapse(reply):
        preview = preview_text(reply)
        sent = await msg.reply_text(preview, reply_markup=_expand_keyboard())
        _collapse_store[sent.message_id] = {
            "full": reply,
            "preview": preview,
            "chat_id": update.effective_chat.id,
        }
    else:
        await msg.reply_text(_cap(reply))


async def on_collapse_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query:
        return
    if not _authorized(update):
        await query.answer("Unauthorized", show_alert=True)
        return

    data = query.data or ""
    message = query.message
    if message is None:
        await query.answer()
        return

    stored = _collapse_store.get(message.message_id)
    if not stored:
        await query.answer("Nothing to expand (message expired).", show_alert=True)
        return

    full = str(stored["full"])
    preview = str(stored["preview"])

    try:
        if data == EXPAND_CB:
            await query.answer("Expanded")
            await message.edit_text(
                _cap(full),
                reply_markup=_collapse_keyboard(),
            )
        elif data == COLLAPSE_CB:
            await query.answer("Collapsed")
            await message.edit_text(
                preview,
                reply_markup=_expand_keyboard(),
            )
        else:
            await query.answer("Unknown button")
    except BadRequest as exc:
        # e.g. "message is not modified"
        logger.debug("edit_text: %s", exc)
        await query.answer()


def main() -> int:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print(
            "Error: TELEGRAM_BOT_TOKEN is not set.\n"
            "1. Talk to @BotFather on Telegram → /newbot\n"
            "2. Copy the token into .env as TELEGRAM_BOT_TOKEN=...\n"
            "3. Optionally set TELEGRAM_CHAT_ID to lock the bot to your chat.",
            file=sys.stderr,
        )
        return 1

    try:
        settings = get_settings()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    client = make_client(settings)
    allow = _allowed_chat_ids()
    logger.info(
        "Starting Telegram bot (model=%s, allowlist=%s)",
        settings["model"],
        sorted(allow) if allow else "open",
    )

    app = Application.builder().token(token).build()
    app.bot_data["settings"] = settings
    app.bot_data["client"] = client

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(
        CallbackQueryHandler(on_collapse_callback, pattern=r"^msg:(expand|collapse)$")
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("The New Guy Cruz — Telegram bot running (Ctrl+C to stop)")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
