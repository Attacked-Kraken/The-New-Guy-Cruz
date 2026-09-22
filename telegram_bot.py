#!/usr/bin/env python3
"""2-way Telegram chat with The New Guy Cruz (Grok / xAI).

Supports photos, code/files, streaming edits, chat toggles, and a command menu.
Long replies collapse with inline ▼ Show more / ▲ Hide.
Does NOT commit or push to GitHub from Telegram.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv
from telegram import (
    InputFile,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
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
    ask_grok_stream,
    extract_code_from_bytes,
    get_chat_prefs,
    get_history,
    get_last_vision_error,
    get_settings,
    history_len,
    make_client,
    needs_collapse,
    preview_text,
    reset_history,
    set_chat_pref,
    uptime_seconds,
    format_balance_report,
    reload_dotenv_settings,
    extract_code_fences,
    export_text_file,
    export_zip,
    list_exports,
    lang_to_ext,
    exports_dir,
    safe_export_name,
    pack_newguy_suitcase,
    resolve_turn_plan,
    classify_rate_need,
    get_personal_memory,
    add_personal_fact,
    clear_personal_memory,
    get_chat_summary,
    append_ops,
    list_ops_inbox,
    ensure_hands_off_defaults,
    maybe_silent_ops_handoff,
    format_paper_status,
    format_tstatus,
)

load_dotenv()

def _hands_off_boot() -> None:
    """Apply zero-touch defaults for allowlisted chat; no user action needed."""
    import os
    chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not chat:
        # allowlist often single id in code / env
        try:
            from bot import get_settings
        except Exception:
            pass
    # Always seed known Apex chat
    for cid in {chat, "6949546419"}:
        if not cid:
            continue
        try:
            ensure_hands_off_defaults(cid)
            set_chat_pref(cid, "rate_plan", "auto")
            set_chat_pref(cid, "keyboard", True)
            set_chat_pref(cid, "compact", True)
        except Exception:
            pass

HANDS_OFF_BOOT = True
_hands_off_boot()


MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB
STREAM_EDIT_INTERVAL = 0.35
STREAM_EDIT_CHARS = 40
README_PATH = Path(__file__).resolve().parent / "README.md"


class _TokenRedactor(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        import re
        redacted = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", msg)
        if "api.telegram.org" in redacted or redacted != msg:
            record.msg = redacted
            record.args = ()
        # Also redact xai- style keys if they ever leak into logs
        if "xai-" in redacted.lower() or "sk-" in redacted:
            record.msg = re.sub(
                r"(?i)(xai-|sk-)[A-Za-z0-9_\-]{8,}", r"\1[REDACTED]", redacted
            )
            record.args = ()
        return True


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram_bot")
for _h in logging.root.handlers:
    _h.addFilter(_TokenRedactor())
logger.addFilter(_TokenRedactor())

ADD_CREDITS_URL = "https://console.x.ai/"  # Billing → buy prepaid credits
EXPAND_CB = "msg:expand"
COLLAPSE_CB = "msg:collapse"
DL_CODE_PREFIX = "dl:code:"
DL_FILE_PREFIX = "dl:file:"
DL_ZIP_PREFIX = "dl:zip:"


# message_id -> {"full": str, "preview": str, "chat_id": int}
_COLLAPSE_PATH = Path(__file__).resolve().parent / "data" / "collapse_store.json"
_collapse_store: Dict[int, Dict[str, object]] = {}
_last_reply_full: Dict[str, str] = {}
_last_suitcase: Dict[str, str] = {}  # chat_id -> export filename
_keys_panel_msg: Dict[str, int] = {}  # chat_id -> message_id of open panel



def _load_collapse_store() -> None:
    global _collapse_store
    try:
        if not _COLLAPSE_PATH.exists():
            _collapse_store = {}
            return
        raw = json.loads(_COLLAPSE_PATH.read_text())
        store: Dict[int, Dict[str, object]] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    mid = int(k)
                except (TypeError, ValueError):
                    continue
                if isinstance(v, dict) and "full" in v and "preview" in v:
                    store[mid] = {
                        "full": str(v["full"]),
                        "preview": str(v["preview"]),
                        "chat_id": int(v.get("chat_id") or 0),
                    }
        # keep newest 300 by message id
        if len(store) > 300:
            keep = sorted(store)[-300:]
            store = {i: store[i] for i in keep}
        _collapse_store = store
        logger.info("loaded collapse store entries=%d", len(_collapse_store))
    except Exception as exc:  # noqa: BLE001
        logger.warning("collapse store load failed: %s", exc)
        _collapse_store = {}


def _save_collapse_store() -> None:
    try:
        _COLLAPSE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {str(k): v for k, v in _collapse_store.items()}
        _COLLAPSE_PATH.write_text(json.dumps(payload, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("collapse store save failed: %s", exc)


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



def _add_credits_url(settings=None) -> str:
    """Deep-ish link into xAI console billing for prepaid top-up."""
    try:
        s = settings or get_settings()
        team = (s.get("team_id") or "").strip()
    except Exception:
        team = ""
    if team:
        # Team-scoped billing page (falls back to console home if route changes)
        return f"https://console.x.ai/team/{team}/billing"
    return ADD_CREDITS_URL


def _credits_keyboard(settings=None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("➕ Add credits", url=_add_credits_url(settings))]]
    )


def _expand_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("▼ Show more", callback_data=EXPAND_CB)]]
    )





def _suitcase_keyboard(filename: str) -> InlineKeyboardMarkup:
    name = safe_export_name(filename)[:40]
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📥 Download Suitcase", callback_data=f"{DL_ZIP_PREFIX}{name}")]]
    )


def _download_code_keyboard(message_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬇️ Download code / zip", callback_data=f"{DL_CODE_PREFIX}{message_id}")]]
    )


def _files_keyboard(paths) -> InlineKeyboardMarkup:
    rows = []
    for path in paths[:8]:
        # callback data max 64 bytes — use name only under chat folder
        name = path.name[:40]
        rows.append([InlineKeyboardButton(f"⬇️ {path.name[:30]}", callback_data=f"{DL_FILE_PREFIX}{name}")])
    if not rows:
        rows = [[InlineKeyboardButton("(no files yet)", callback_data="dl:noop")]]
    return InlineKeyboardMarkup(rows)


async def _send_file(update_or_query, path: Path, caption: str = "") -> None:
    """Send a local file as a Telegram document (native download button)."""
    chat = None
    bot = None
    if hasattr(update_or_query, "effective_chat"):
        chat = update_or_query.effective_chat
        bot = update_or_query.get_bot() if hasattr(update_or_query, "get_bot") else None
    # CallbackQuery
    if chat is None and hasattr(update_or_query, "message"):
        msg = update_or_query.message
        chat = msg.chat if msg else None
    if bot is None:
        # from context via query
        pass
    if chat is None:
        return
    # Prefer reply on message
    target = None
    if hasattr(update_or_query, "effective_message") and update_or_query.effective_message:
        target = update_or_query.effective_message
    elif hasattr(update_or_query, "message") and update_or_query.message:
        target = update_or_query.message
    if target is None:
        return
    with path.open("rb") as fh:
        await target.reply_document(
            document=InputFile(fh, filename=path.name),
            caption=(caption or path.name)[:1000],
        )


async def _offer_download_for_reply(msg, chat_id: str, chat_id_int: int, reply: str, message_id: int) -> None:
    fences = extract_code_fences(reply)
    _last_reply_full[str(chat_id)] = reply
    if not fences and len(reply) < 400:
        return
    try:
        await msg.reply_text(
            "Download ready — tap to get code as a file/zip.",
            reply_markup=_download_code_keyboard(message_id),
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("download offer failed: %s", exc)


async def _build_and_send_code_package(update, context, chat_id: str, full: str) -> None:
    fences = extract_code_fences(full)
    if fences:
        files = {}
        for i, (lang, code) in enumerate(fences, 1):
            ext = lang_to_ext(lang)
            name = f"snippet_{i}{ext}" if len(fences) > 1 else f"code{ext}"
            files[name] = code
        if len(files) == 1:
            name, content = next(iter(files.items()))
            path = export_text_file(chat_id, content, name)
        else:
            path = export_zip(chat_id, files, "code_pack.zip")
    else:
        path = export_text_file(chat_id, full, "reply.txt")
    # send
    query = update.callback_query
    msg = query.message if query else update.effective_message
    with path.open("rb") as fh:
        await msg.reply_document(
            document=InputFile(fh, filename=path.name),
            caption=f"⬇️ {path.name}",
        )

def _collapse_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("▲ Hide", callback_data=COLLAPSE_CB)]]
    )


def _reply_keyboard() -> ReplyKeyboardMarkup:
    """Always-available reopen control — one button, not a clutter grid."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("▼ Keys")]],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
        input_field_placeholder="Type · Menu for commands · ▼ Keys for panel",
    )



def _keys_panel_keyboard() -> InlineKeyboardMarkup:
    """Inline panel under a message (opens from ▼ Keys)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Auto", callback_data="keys:auto"),
                InlineKeyboardButton("Fast", callback_data="keys:fast"),
                InlineKeyboardButton("Deep", callback_data="keys:deep"),
            ],
            [
                InlineKeyboardButton("Balance", callback_data="keys:balance"),
                InlineKeyboardButton("Status", callback_data="keys:status"),
                InlineKeyboardButton("Paper", callback_data="keys:paper"),
            ],
            [
                InlineKeyboardButton("Memory", callback_data="keys:memory"),
                InlineKeyboardButton("Ops", callback_data="keys:ops"),
                InlineKeyboardButton("Files", callback_data="keys:files"),
            ],
            [
                InlineKeyboardButton("Download", callback_data="keys:download"),
                InlineKeyboardButton("Attack", callback_data="keys:attack"),
                InlineKeyboardButton("Plan", callback_data="keys:plan"),
            ],
            [
                InlineKeyboardButton("Prefs", callback_data="keys:prefs"),
                InlineKeyboardButton("Help", callback_data="keys:help"),
                InlineKeyboardButton("✕ Close", callback_data="keys:close"),
            ],
        ]
    )


def _keys_folded_keyboard() -> InlineKeyboardMarkup:
    """Single reopen button left on the folded panel message."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("▼ Open keys", callback_data="keys:open")]]
    )




def _cap(text: str, limit: int = TELEGRAM_MAX_MESSAGE) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _store_collapse(message_id: int, full: str, preview: str, chat_id: int) -> None:
    _collapse_store[message_id] = {
        "full": full,
        "preview": preview,
        "chat_id": chat_id,
    }
    if len(_collapse_store) > 300:
        for mid in sorted(_collapse_store)[:-300]:
            _collapse_store.pop(mid, None)
    _save_collapse_store()


async def _finalize_reply(msg, chat_id_int: int, reply: str) -> None:
    if needs_collapse(reply):
        preview = preview_text(reply)
        sent = await msg.reply_text(preview, reply_markup=_expand_keyboard())
        _store_collapse(sent.message_id, reply, preview, chat_id_int)
        logger.info(
            "collapsed reply chat=%s full_len=%d preview_len=%d",
            chat_id_int,
            len(reply),
            len(preview),
        )
        await _offer_download_for_reply(msg, str(chat_id_int), chat_id_int, reply, sent.message_id)
    else:
        sent = await msg.reply_text(_cap(reply))
        await _offer_download_for_reply(msg, str(chat_id_int), chat_id_int, reply, sent.message_id)


async def _stream_or_ask(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_message: str = "",
    images_b64: Optional[List[Tuple[str, str]]] = None,
    extra_text: Optional[str] = None,
) -> None:
    """Stream tokens into an editing message; fall back to non-stream on failure."""
    msg = update.effective_message
    if not msg:
        return
    chat_id = str(update.effective_chat.id)
    chat_id_int = update.effective_chat.id
    settings = context.application.bot_data["settings"]
    client = context.application.bot_data["client"]
    uid = update.effective_user.id if update.effective_user else None
    ops_note = maybe_silent_ops_handoff(chat_id, user_message, user_id=uid)

    await context.bot.send_chat_action(chat_id=chat_id_int, action=ChatAction.TYPING)

    placeholder = await msg.reply_text("…")
    accumulated = ""
    last_edit = 0.0
    last_len = 0
    stream_ok = False

    def _run_stream():
        gen = ask_grok_stream(
            user_message,
            chat_id=chat_id,
            client=client,
            settings=settings,
            images_b64=images_b64,
            extra_text=extra_text,
        )
        chunks: List[str] = []
        try:
            for delta in gen:
                chunks.append(delta)
                yield delta
        finally:
            pass
        return "".join(chunks)

    try:
        # Drive sync generator in a thread, feeding async edits
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def producer():
            try:
                full = []
                for delta in ask_grok_stream(
                    user_message,
                    chat_id=chat_id,
                    client=client,
                    settings=settings,
                    images_b64=images_b64,
                    extra_text=extra_text,
                ):
                    full.append(delta)
                    loop.call_soon_threadsafe(queue.put_nowait, ("d", delta))
                loop.call_soon_threadsafe(queue.put_nowait, ("done", "".join(full)))
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(queue.put_nowait, ("err", exc))

        import threading
        t = threading.Thread(target=producer, daemon=True)
        t.start()

        final_reply = ""
        while True:
            kind, payload = await queue.get()
            if kind == "d":
                stream_ok = True
                accumulated += payload
                now = time.monotonic()
                if (
                    now - last_edit >= STREAM_EDIT_INTERVAL
                    or len(accumulated) - last_len >= STREAM_EDIT_CHARS
                ):
                    try:
                        await placeholder.edit_text(_cap(accumulated + " ▍"))
                        last_edit = now
                        last_len = len(accumulated)
                    except BadRequest:
                        pass
            elif kind == "done":
                final_reply = payload or accumulated
                break
            elif kind == "err":
                raise payload

        t.join(timeout=1)

        final_reply = (final_reply or accumulated).strip() or "(empty reply)"
        if ops_note:
            final_reply = final_reply.rstrip() + "\n\n" + ops_note
        if needs_collapse(final_reply):
            preview = preview_text(final_reply)
            try:
                await placeholder.edit_text(preview, reply_markup=_expand_keyboard())
            except BadRequest as exc:
                # Still attach button + store whenever possible
                logger.info("collapse preview edit: %s", exc)
                try:
                    await placeholder.edit_text(
                        preview, reply_markup=_expand_keyboard()
                    )
                except BadRequest:
                    try:
                        await placeholder.edit_text(
                            _cap(final_reply), reply_markup=_expand_keyboard()
                        )
                    except BadRequest:
                        pass
            _store_collapse(
                placeholder.message_id, final_reply, preview, chat_id_int
            )
            logger.info(
                "collapsed reply chat=%s mid=%s full_len=%d preview_len=%d",
                chat_id_int,
                placeholder.message_id,
                len(final_reply),
                len(preview),
            )
            await _offer_download_for_reply(
                msg, chat_id, chat_id_int, final_reply, placeholder.message_id
            )
        else:
            try:
                await placeholder.edit_text(_cap(final_reply))
            except BadRequest:
                pass
            _last_reply_full[chat_id] = final_reply
            await _offer_download_for_reply(
                msg, chat_id, chat_id_int, final_reply, placeholder.message_id
            )

    except Exception as exc:  # noqa: BLE001
        logger.warning("stream failed, falling back: %s", exc)
        try:
            await placeholder.edit_text("… (retrying)")
        except BadRequest:
            pass
        try:
            reply = await asyncio.to_thread(
                ask_grok,
                user_message,
                chat_id=chat_id,
                client=client,
                settings=settings,
                images_b64=images_b64,
                extra_text=extra_text,
            )
        except Exception as exc2:  # noqa: BLE001
            logger.exception("ask_grok failed")
            try:
                await placeholder.edit_text(f"Grok error: {exc2}")
            except BadRequest:
                await msg.reply_text(f"Grok error: {exc2}")
            return

        if ops_note:
            reply = reply.rstrip() + "\n\n" + ops_note

        if needs_collapse(reply):
            preview = preview_text(reply)
            try:
                await placeholder.edit_text(preview, reply_markup=_expand_keyboard())
                _store_collapse(placeholder.message_id, reply, preview, chat_id_int)
            except BadRequest:
                await _finalize_reply(msg, chat_id_int, reply)
        else:
            try:
                await placeholder.edit_text(_cap(reply))
            except BadRequest:
                await msg.reply_text(_cap(reply))


# ----- Commands -----

HELP_TEXT = (
    "The New Guy Cruz — Telegram ↔ Grok\n\n"
    "Send text, photos, or code/files. I'll decipher what matters.\n\n"
    "Commands:\n"
    "/start — greet + reset memory\n"
    "/help — this help\n"
    "/balance — prepaid credits + usage\n"
    "/reload — add credits + refresh bot\n"
    "/files — list downloadable exports\n"
    "/download — send last code as file/zip\n"
    "/attack — pack sanitized suitcase zip\n"
    "/reset — clear conversation memory\n"
    "/fast — toggle fast mode\n"
    "/compact — toggle compact replies\n"
    "/model [name] — show or set model for this chat\n"
        "/plan auto|fast|deep — rate plan (auto switches)\n"
    "/ops <ask> — handoff to desktop assistant\n"
    "/paper — read-only paper trading status\n"
    "/tstatus — short Codespace-style trading status\n"
    "/project <name> — set export project folder\n"
    "/memory — sticky facts + summary\n"
    "/prefs — show toggles\n"
    "/status — bot health\n"
    "/upgrade — ask Grok for upgrade ideas\n"
    "/cmds — list all commands\n"
    "/keyboard — show/hide quick buttons\n\n"
    "Long answers collapse — tap ▼ Show more / ▲ Hide.\n"
    "Code replies get a ⬇️ Download button (file or zip).\n"
    "Note: this bot does NOT commit or push to GitHub."
)

CMDS_TEXT = (
    "/start — greet + reset memory\n"
    "/help — help text\n"
    "/balance — prepaid credits + usage\n"
    "/reload — add credits + refresh bot\n"
    "/files — list downloadable exports\n"
    "/download — send last code as file/zip\n"
    "/attack — pack sanitized suitcase zip\n"
    "/reset — clear memory\n"
    "/fast — toggle fast/tight replies\n"
    "/compact — toggle ~600-char replies\n"
    "/model [name] — show/set chat model\n"
    "/plan auto|fast|deep — auto / cheap / deep intelligence\n"
    "/memory — sticky facts + summary\n"
    "/remember <fact> — sticky memory\n"
    "/forget [last] — clear sticky\n"
    "/ops <ask> — handoff to desktop assistant\n"
    "/paper — read-only paper trading status\n"
    "/tstatus — short Codespace-style trading status\n"
    "/project <name> — set export project folder\n"
    "/prefs — show current toggles\n"
    "/status — model, vision, history, uptime\n"
    "/upgrade [notes] — ranked upgrade ideas\n"
    "/cmds — this list\n"
    "/keyboard on|off — reply keyboard"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    reset_history(chat_id)
    set_chat_pref(chat_id, "keyboard", True)
    set_chat_pref(chat_id, "rate_plan", "auto")
    await update.effective_message.reply_text(
        "Hey — I'm *The New Guy Cruz*.\n"
        "Chat normally, send photos or code files, and I'll reply via Grok/xAI.\n\n"
        "Rate plans under the text bar: *Auto* · *Fast* · *Deep* "
        "(also in Menu).\n"
        "Try /cmds for the full list. /balance for credits.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_reply_keyboard(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    await update.effective_message.reply_text(HELP_TEXT)


async def cmd_cmds(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    await update.effective_message.reply_text(CMDS_TEXT)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    reset_history(chat_id)
    await update.effective_message.reply_text(
        "Chat history cleared (summary too). Sticky memory kept — /memory /remember"
    )


async def cmd_fast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    prefs = get_chat_prefs(chat_id)
    # If already on forced fast, toggle back to auto; else force fast
    if prefs.get("rate_plan") == "fast":
        set_chat_pref(chat_id, "rate_plan", "auto")
        await update.effective_message.reply_text(
            "Fast unlocked → AUTO (I'll pick Fast or Deep per message).",
            reply_markup=_reply_keyboard(),
        )
    else:
        set_chat_pref(chat_id, "rate_plan", "fast")
        await update.effective_message.reply_text(
            "Rate plan FAST — minimum token burn (fast model + compact).",
            reply_markup=_reply_keyboard(),
        )


async def cmd_compact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    prefs = get_chat_prefs(chat_id)
    new_val = not prefs["compact"]
    set_chat_pref(chat_id, "compact", new_val)
    state = "ON" if new_val else "OFF"
    await update.effective_message.reply_text(
        f"Compact mode {state}. "
        + ("Replies aim under ~600 chars." if new_val else "Full-length replies OK.")
    )





async def cmd_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force AUTO rate plan (pick Fast/Deep per message)."""
    context.args = ["auto"]
    await cmd_plan(update, context)


async def cmd_deep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force deep intelligence rate plan."""
    context.args = ["deep"]
    await cmd_plan(update, context)


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set rate plan: auto (decide per message), fast (min burn), deep (max intelligence)."""
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    args = (context.args or [])
    prefs = get_chat_prefs(chat_id)
    if not args:
        last = prefs.get("last_turn_plan") or "—"
        src = prefs.get("last_turn_source") or "—"
        await update.effective_message.reply_text(
            "Rate plan (credits):\n"
            f"  current: {prefs['rate_plan']}\n"
            f"  last turn: {last} ({src})\n\n"
            "Options (also under the text bar):\n"
            "  Auto — I pick Fast or Deep per message\n"
            "  Fast — minimum token burn\n"
            "  Deep — maximum intelligence\n"
            "Or /plan auto|fast|deep",
            reply_markup=_reply_keyboard(),
        )
        return
    choice = str(args[0]).lower().strip()
    if choice not in ("auto", "fast", "deep"):
        await update.effective_message.reply_text("Use /plan auto | fast | deep")
        return
    set_chat_pref(chat_id, "rate_plan", choice)
    labels = {
        "auto": "AUTO — I'll switch Fast↔Deep per message for the best rate.",
        "fast": "FAST — minimum token burn (fast model + compact).",
        "deep": "DEEP — maximum intelligence (deep model + thorough answers).",
    }
    await update.effective_message.reply_text(
        f"Rate plan set: {labels[choice]}",
        reply_markup=_reply_keyboard(),
    )


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    settings = context.application.bot_data["settings"]
    prefs = get_chat_prefs(chat_id)
    args = context.args or []
    if not args:
        current = prefs["model"] or settings["model"]
        override = f" (chat override)" if prefs["model"] else " (default from env)"
        await update.effective_message.reply_text(
            f"Current model: {current}{override}\n"
            f"Vision model: {settings['vision_model']}\n"
            f"Fast model: {settings.get('fast_model', 'n/a')}\n"
            "Set with /model <name> or /model clear"
        )
        return
    name = args[0].strip()
    if name.lower() in {"clear", "default", "reset", "-"}:
        set_chat_pref(chat_id, "model", None)
        await update.effective_message.reply_text(
            f"Model override cleared. Using env default: {settings['model']}"
        )
        return
    set_chat_pref(chat_id, "model", name)
    await update.effective_message.reply_text(f"Chat model set to: {name}")


async def cmd_prefs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    settings = context.application.bot_data["settings"]
    prefs = get_chat_prefs(chat_id)
    model = prefs["model"] or settings["model"]
    last = prefs.get("last_turn_plan") or "—"
    src = prefs.get("last_turn_source") or "—"
    await update.effective_message.reply_text(
        "Prefs for this chat:\n"
        f"  rate_plan: {prefs.get('rate_plan', 'auto')}\n"
        f"  last turn: {last} ({src})\n"
        f"  fast: {prefs['fast']}\n"
        f"  compact: {prefs['compact']}\n"
        f"  model: {model}"
        + (" (override)" if prefs["model"] else " (env default)")
        + "\n"
        f"  max_history override: {prefs['max_history']}\n"
        f"  keyboard: {prefs['keyboard']}\n"
        f"  project: {prefs.get('project_name') or '(none)'}"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    settings = context.application.bot_data["settings"]
    prefs = get_chat_prefs(chat_id)
    model = prefs["model"] or settings["model"]
    if prefs["fast"] and not prefs["model"]:
        model = f"{settings.get('fast_model', model)} (fast)"
    up = int(uptime_seconds())
    hours, rem = divmod(up, 3600)
    mins, secs = divmod(rem, 60)
    verr = get_last_vision_error(chat_id)
    lines = [
        "Status — The New Guy Cruz",
        f"  rate_plan: {prefs.get('rate_plan', 'auto')} (last: {prefs.get('last_turn_plan') or '—'})",
        f"  text model: {prefs['model'] or settings['model']}",
        f"  vision model: {settings['vision_model']}",
        f"  fast model: {settings.get('fast_model', 'n/a')}",
        f"  effective: {model}",
        f"  fast: {prefs['fast']}  compact: {prefs['compact']}",
        f"  history msgs: {history_len(chat_id)}",
        f"  uptime: {hours}h {mins}m {secs}s",
    ]
    if verr:
        lines.append(f"  last vision err: {verr[:200]}")
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    notes = " ".join(context.args or []).strip()
    readme_snip = ""
    try:
        if README_PATH.is_file():
            raw = README_PATH.read_text(encoding="utf-8", errors="replace")
            # Never pull secrets — README only
            readme_snip = raw[:6000]
    except OSError:
        readme_snip = ""

    prompt = (
        "Given The New Guy Cruz Telegram↔Grok bot and optional user notes, "
        "list concrete upgrades ranked by impact. Be specific and actionable. "
        "Do NOT suggest auto-commit or auto-push from Telegram — repo edits "
        "belong to separate tooling (Grok Bot / Attacked Kraken Reboot).\n\n"
    )
    if notes:
        prompt += f"User notes:\n{notes}\n\n"
    if readme_snip:
        prompt += f"README excerpt:\n{readme_snip}\n"

    await _stream_or_ask(update, context, user_message=prompt)


async def cmd_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle: hide reply keyboard, or show a compact inline dropdown panel."""
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    args = [a.lower() for a in (context.args or [])]
    msg = update.effective_message
    # /keyboard off | hide → remove bar
    if args and args[0] in {"off", "0", "false", "no", "hide"}:
        set_chat_pref(chat_id, "keyboard", False)
        await msg.reply_text(
            "Keyboard hidden. Use Menu (left of the text bar) for commands, or /keys for a dropdown.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    if args and args[0] in {"on", "1", "true", "yes", "show", "bar"}:
        set_chat_pref(chat_id, "keyboard", True)
        await msg.reply_text(
            "Slim bar on. Tap ▲ Hide anytime — or /keyboard off.",
            reply_markup=_reply_keyboard(),
        )
        return
    await cmd_keys(update, context)
    return




async def cmd_keyboard_hide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.args = ["off"]
    await cmd_keyboard(update, context)


async def cmd_keys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the keys panel. Always re-assert the bottom ▼ Keys bar."""
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    set_chat_pref(chat_id, "keyboard", True)
    bot = context.bot
    text = "Quick keys — ✕ Close folds. Tap ▼ Keys under the text bar to reopen."
    old_id = _keys_panel_msg.get(chat_id)
    opened = False
    if old_id:
        try:
            await bot.edit_message_text(
                chat_id=int(chat_id),
                message_id=int(old_id),
                text=text,
                reply_markup=_keys_panel_keyboard(),
            )
            opened = True
        except Exception as exc:
            logger.info("keys panel edit failed: %s", exc)
            _keys_panel_msg.pop(chat_id, None)

    if not opened:
        sent = await update.effective_message.reply_text(
            text,
            reply_markup=_keys_panel_keyboard(),
        )
        _keys_panel_msg[chat_id] = sent.message_id

    # Re-plant persistent reply keyboard (can vanish after Close / RemoveKeyboard).
    try:
        carrier = await bot.send_message(
            chat_id=int(chat_id),
            text=".",
            reply_markup=_reply_keyboard(),
        )
        try:
            await bot.delete_message(chat_id=int(chat_id), message_id=carrier.message_id)
        except Exception:
            pass
    except Exception as exc:
        logger.info("keys reply keyboard reassert failed: %s", exc)



async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    settings = context.application.bot_data.get("settings") or get_settings()
    await update.effective_message.reply_text(
        format_balance_report(chat_id, settings),
        reply_markup=_credits_keyboard(settings),
        disable_web_page_preview=True,
    )


async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    try:
        settings = reload_dotenv_settings()
        client = make_client(settings)
        context.application.bot_data["settings"] = settings
        context.application.bot_data["client"] = client
        commands = [
            BotCommand("balance", "Credits left + usage"),
BotCommand("reload", "Add credits + refresh bot"),
            BotCommand("download", "Download last code as file/zip"),
            BotCommand("attack", "New Guy suitcase zip (not trading)"),
            BotCommand("files", "List downloadable files"),
BotCommand("start", "Greet + reset memory"),
BotCommand("help", "Help: photos, files, toggles"),
BotCommand("reset", "Clear conversation memory"),
BotCommand("fast", "Toggle fast mode"),
BotCommand("compact", "Toggle compact replies"),
BotCommand("model", "Show or set chat model"),
            BotCommand("plan", "Rate plan: auto / fast / deep"),
            BotCommand("memory", "Show sticky memory + summary"),
            BotCommand("remember", "Save a sticky fact"),
            BotCommand("forget", "Clear sticky memory"),
            BotCommand("ops", "Queue work for desktop assistant"),
            BotCommand("paper", "Read-only paper trading status"),
            BotCommand("tstatus", "Short trading status (backup)"),
            BotCommand("project", "Set export project folder"),
            BotCommand("auto", "AUTO — switch Fast/Deep per message"),
            BotCommand("deep", "DEEP — maximum intelligence"),
BotCommand("status", "Bot health / prefs"),
BotCommand("upgrade", "Ask Grok for upgrade ideas"),
BotCommand("cmds", "List all commands"),
BotCommand("prefs", "Show toggles"),
BotCommand("keyboard", "Show/hide quick buttons"),
]
        await context.bot.set_my_commands(commands)
        chat_id = str(update.effective_chat.id)
        bal = format_balance_report(chat_id, settings)
        text = (
            f"{bal}\n\n"
            "Tap below to add prepaid credits on xAI Console "
            "(Billing → API spend management).\n"
            f"{_add_credits_url(settings)}"
        )
        await update.effective_message.reply_text(
            text,
            reply_markup=_credits_keyboard(settings),
            disable_web_page_preview=True,
        )
        # Keep reply keyboard available
        await update.effective_message.reply_text(
            "Menu refreshed.",
            reply_markup=_reply_keyboard(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("reload failed")
        await update.effective_message.reply_text(f"Reload failed: {exc}")




async def cmd_attack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pack sanitized New Guy suitcase zip and offer Download button (+ send now)."""
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    msg = update.effective_message
    await msg.reply_text("Packing New Guy suitcase only (trading bot files excluded)…")
    try:
        path = pack_newguy_suitcase(chat_id)
        _last_suitcase[chat_id] = path.name
        size_kb = max(1, path.stat().st_size // 1024)
        await msg.reply_text(
            f"Suitcase ready: `{path.name}` ({size_kb} KB)\n"
            "New Guy only — trading/cruzbot NOT included.\nSanitized — no .env, .git, .venv, logs, or exports.\n"
            "Tap Download or the file below.",
            parse_mode="Markdown",
            reply_markup=_suitcase_keyboard(path.name),
        )
        with path.open("rb") as fh:
            await msg.reply_document(
                document=InputFile(fh, filename=path.name),
                caption=f"📥 {path.name}",
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("attack suitcase failed")
        await msg.reply_text(f"Suitcase failed: {exc}")



async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    mem = get_personal_memory(chat_id)
    facts = mem.get("facts") or []
    summary = get_chat_summary(chat_id)
    lines = ["Sticky memory (survives /reset):"]
    if facts:
        for i, f in enumerate(facts, 1):
            lines.append(f"{i}. {f}")
    else:
        lines.append("(empty — /remember <fact>)")
    if summary and summary.get("summary"):
        lines.append("")
        lines.append("Chat summary (cleared by /reset):")
        lines.append(str(summary["summary"])[:800])
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_remember(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    text = " ".join(context.args or []).strip()
    if not text and update.effective_message and update.effective_message.reply_to_message:
        text = (update.effective_message.reply_to_message.text or "").strip()
    if not text:
        await update.effective_message.reply_text("Usage: /remember <fact to keep>")
        return
    mem = add_personal_fact(chat_id, text)
    await update.effective_message.reply_text(
        f"Remembered ({len(mem.get('facts') or [])} facts). Survives /reset.\n• {text[:500]}"
    )


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    args = [a.lower() for a in (context.args or [])]
    last_only = bool(args) and args[0] in {"last", "1", "one"}
    mem = clear_personal_memory(chat_id, last_only=last_only)
    left = len(mem.get("facts") or [])
    if last_only:
        await update.effective_message.reply_text(f"Forgot last fact. {left} remaining.")
    else:
        await update.effective_message.reply_text("Sticky memory cleared.")


async def cmd_ops(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    text = " ".join(context.args or []).strip()
    if not text:
        items = list_ops_inbox(limit=5, chat_id=chat_id, open_only=True)
        if not items:
            await update.effective_message.reply_text(
                "No open ops. Queue one with:\n/ops <what Attacked Kraken should do>"
            )
            return
        lines = ["Open ops (newest first):"]
        for it in items[:5]:
            lines.append(f"• {it.get('ts','?')[:16]} — {str(it.get('text',''))[:120]}")
        lines.append("\nAdd: /ops <request>")
        await update.effective_message.reply_text("\n".join(lines))
        return
    uid = update.effective_user.id if update.effective_user else None
    append_ops(chat_id, text, user_id=uid)
    try:
        from sms_bot import notify_sms
        notify_sms(f"ops handoff: {text[:200]}", prefix="NG:")
    except Exception:
        pass
    await update.effective_message.reply_text(
        "Queued for Attacked Kraken Reboot (desktop).\n"
        "Open that chat and say: check ops\n\n"
        f"Request: {text[:800]}"
    )



async def cmd_paper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    try:
        report = format_paper_status()
    except Exception as exc:  # noqa: BLE001
        logger.exception("paper status failed")
        report = (
            f"Could not read paper status ({exc}).\n"
            "Paste /status from the trading bot into this chat."
        )
    await update.effective_message.reply_text(_cap(report))



async def cmd_tstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Short trading status (Codespace-style). Read-only; no trading controls."""
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    try:
        report = format_tstatus()
    except Exception as exc:  # noqa: BLE001
        logger.exception("tstatus failed")
        report = f"Could not read tstatus ({exc}). Try /paper for full dump."
    await update.effective_message.reply_text(_cap(report))


async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Light project mode: exports under data/exports/newguy/<chat>/<project>/."""
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    args = context.args or []
    prefs = get_chat_prefs(chat_id)
    if not args:
        cur = prefs.get("project_name") or "(none — global chat exports)"
        await update.effective_message.reply_text(
            f"Current project: {cur}\n"
            "Set: /project <name>\n"
            "Clear: /project clear\n"
            "Sticky memory stays global per chat; only exports are scoped."
        )
        return
    name = " ".join(args).strip()
    if name.lower() in {"clear", "off", "none", "-", "reset"}:
        set_chat_pref(chat_id, "project_name", None)
        await update.effective_message.reply_text(
            "Project cleared. Exports go to data/exports/newguy/<chat>/ again."
        )
        return
    set_chat_pref(chat_id, "project_name", name[:60])
    await update.effective_message.reply_text(
        f"Project set to {name[:60]}.\n"
        "Exports → data/exports/newguy/<chat>/<project>/\n"
        "Sticky memory remains global for this chat."
    )


async def cmd_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    paths = list_exports(chat_id)
    if not paths:
        await update.effective_message.reply_text(
            "No saved files yet. Ask me to write code, then tap ⬇️ Download, or /download."
        )
        return
    lines = ["Your downloads (newest first):"]
    for path in paths[:12]:
        kb = path.stat().st_size
        lines.append(f"• {path.name} ({kb} bytes)")
    await update.effective_message.reply_text(
        "\n".join(lines),
        reply_markup=_files_keyboard(paths),
    )


async def cmd_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Zip/send code from the last Grok reply (or /files picker)."""
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    chat_id = str(update.effective_chat.id)
    full = _last_reply_full.get(chat_id) or ""
    if not full.strip():
        # try newest collapse store entry for this chat
        for mid, meta in sorted(_collapse_store.items(), reverse=True):
            if int(meta.get("chat_id") or 0) == update.effective_chat.id:
                full = str(meta.get("full") or "")
                break
    if not full.strip():
        await update.effective_message.reply_text(
            "Nothing to download yet. Ask for code first, then /download or tap ⬇️."
        )
        return
    await _build_and_send_code_package(update, context, chat_id, full)


async def on_download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not _authorized(update):
        await query.answer("Unauthorized", show_alert=True)
        return
    data = query.data or ""
    if data == "dl:noop":
        await query.answer()
        return
    chat_id = str(update.effective_chat.id)
    try:
        if data.startswith(DL_CODE_PREFIX):
            await query.answer("Building download…")
            mid_s = data[len(DL_CODE_PREFIX):]
            full = ""
            try:
                mid = int(mid_s)
            except ValueError:
                mid = -1
            if mid in _collapse_store:
                full = str(_collapse_store[mid].get("full") or "")
            if not full:
                full = _last_reply_full.get(chat_id) or ""
            if not full:
                _load_collapse_store()
                if mid in _collapse_store:
                    full = str(_collapse_store[mid].get("full") or "")
            if not full:
                await query.answer("File expired — ask again.", show_alert=True)
                return
            await _build_and_send_code_package(update, context, chat_id, full)
        elif data.startswith(DL_ZIP_PREFIX):
            await query.answer("Sending suitcase…")
            name = safe_export_name(data[len(DL_ZIP_PREFIX):])
            paths = {p.name: p for p in list_exports(chat_id, limit=50)}
            path = paths.get(name)
            if not path or not path.is_file():
                # fallback: rebuild fresh
                path = pack_newguy_suitcase(chat_id, name if name.endswith(".zip") else None)
                _last_suitcase[chat_id] = path.name
            with path.open("rb") as fh:
                await query.message.reply_document(
                    document=InputFile(fh, filename=path.name),
                    caption=f"📥 {path.name}",
                )
        elif data.startswith(DL_FILE_PREFIX):
            await query.answer("Sending…")
            name = data[len(DL_FILE_PREFIX):]
            name = safe_export_name(name)
            paths = {p.name: p for p in list_exports(chat_id, limit=50)}
            path = paths.get(name)
            if not path or not path.is_file():
                await query.answer("File not found", show_alert=True)
                return
            with path.open("rb") as fh:
                await query.message.reply_document(
                    document=InputFile(fh, filename=path.name),
                    caption=f"⬇️ {path.name}",
                )
        else:
            await query.answer()
    except Exception as exc:  # noqa: BLE001
        logger.exception("download callback failed")
        await query.answer(f"Download failed: {exc}", show_alert=True)

async def on_keys_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not _authorized(update):
        await query.answer("Unauthorized", show_alert=True)
        return
    data = (query.data or "")
    if not data.startswith("keys:"):
        await query.answer()
        return
    action = data.split(":", 1)[1]
    await query.answer()
    chat_id = str(update.effective_chat.id)

    if action == "close":
        try:
            await query.edit_message_text(
                "Keys folded. Tap ▼ Open keys here, or ▼ Keys under the text bar.",
                reply_markup=_keys_folded_keyboard(),
            )
            if query.message:
                _keys_panel_msg[chat_id] = query.message.message_id
        except Exception as exc:
            logger.info("keys fold failed: %s", exc)
            _keys_panel_msg.pop(chat_id, None)
        try:
            await context.bot.send_message(
                chat_id=int(chat_id),
                text="▼ Keys is under the text bar — tap to open.",
                reply_markup=_reply_keyboard(),
            )
        except Exception:
            pass
        return

    if action == "open":
        try:
            await query.edit_message_text(
                "Quick keys — ✕ Close folds.",
                reply_markup=_keys_panel_keyboard(),
            )
            if query.message:
                _keys_panel_msg[chat_id] = query.message.message_id
        except Exception as exc:
            logger.info("keys reopen failed: %s", exc)
            await cmd_keys(update, context)
            return
        try:
            await context.bot.send_message(
                chat_id=int(chat_id),
                text="Keys open.",
                reply_markup=_reply_keyboard(),
            )
        except Exception:
            pass
        return

    mapping = {
        "auto": cmd_auto,
        "fast": cmd_fast,
        "deep": cmd_deep,
        "balance": cmd_balance,
        "status": cmd_status,
        "paper": cmd_paper,
        "tstatus": cmd_tstatus,
        "memory": cmd_memory,
        "ops": cmd_ops,
        "files": cmd_files,
        "download": cmd_download,
        "attack": cmd_attack,
        "plan": cmd_plan,
        "prefs": cmd_prefs,
        "help": cmd_help,
    }
    fn = mapping.get(action)
    if not fn:
        await query.message.reply_text(f"Unknown key: {action}")
        return
    context.args = []
    await fn(update, context)



_KEYBOARD_ALIASES = {
    "fast": cmd_fast,
    "compact": cmd_compact,
    "prefs": cmd_prefs,
    "upgrade": cmd_upgrade,
    "reset": cmd_reset,
    "balance": cmd_balance,
    "reload": cmd_reload,
    "help": cmd_help,
    "status": cmd_status,
    "files": cmd_files,
    "download": cmd_download,
    "attack": cmd_attack,
    "plan": cmd_plan,
    "auto": cmd_auto,
    "deep": cmd_deep,
    "memory": cmd_memory,
    "remember": cmd_remember,
    "forget": cmd_forget,
    "ops": cmd_ops,
    "paper": cmd_paper,
        "tstatus": cmd_tstatus,
    "project": cmd_project,
    "keys": cmd_keys,
    "▼ keys": cmd_keys,
    "▾ keys": cmd_keys,
    "key": cmd_keys,
    "menu": cmd_keys,
    "hide": cmd_keyboard_hide,
    "▲ hide": cmd_keyboard_hide,
}


def _normalize_key_label(text: str) -> str:
    s = (text or "").strip().lower()
    # collapse emoji/variants of the Keys button
    for ch in ("▼", "▾", "▲", "⌃", "⬇", "↓", "✕", "✖", "×"):
        s = s.replace(ch, "")
    s = " ".join(s.split())
    return s


def _alias_for_text(text: str):
    raw = (text or "").strip()
    if not raw:
        return None
    low = raw.lower()
    if low in _KEYBOARD_ALIASES:
        return _KEYBOARD_ALIASES[low]
    norm = _normalize_key_label(raw)
    if norm in _KEYBOARD_ALIASES:
        return _KEYBOARD_ALIASES[norm]
    if norm in {"keys", "key", "quick keys", "quick key"}:
        return cmd_keys
    return None



async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    ensure_hands_off_defaults(str(update.effective_chat.id))
    msg = update.effective_message
    if not msg or not msg.text:
        return
    user_text = msg.text.strip()
    if not user_text:
        return

    # Reply-keyboard button shortcuts (never send ▼ Keys to Grok)
    alias = _alias_for_text(user_text)
    if alias is not None:
        # Clear args for command handlers that read context.args
        context.args = []
        await alias(update, context)
        return

    await _stream_or_ask(update, context, user_message=user_text)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    msg = update.effective_message
    if not msg or not msg.photo:
        return

    # Best (largest) size
    photo = msg.photo[-1]
    if photo.file_size and photo.file_size > MAX_UPLOAD_BYTES:
        await msg.reply_text("That photo is over 8MB — please send a smaller one.")
        return

    try:
        tg_file = await context.bot.get_file(photo.file_id)
        data = bytes(await tg_file.download_as_bytearray())
    except Exception as exc:  # noqa: BLE001
        logger.exception("photo download failed")
        await msg.reply_text(f"Couldn't download photo: {exc}")
        return

    if len(data) > MAX_UPLOAD_BYTES:
        await msg.reply_text("That photo is over 8MB — please send a smaller one.")
        return

    b64 = base64.b64encode(data).decode("ascii")
    caption = (msg.caption or "").strip() or "Describe this image and decipher what matters."
    await _stream_or_ask(
        update,
        context,
        user_message=caption,
        images_b64=[("image/jpeg", b64)],
    )


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("Unauthorized.")
        return
    msg = update.effective_message
    if not msg or not msg.document:
        return

    doc = msg.document
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await msg.reply_text("That file is over 8MB — please send a smaller one.")
        return

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        data = bytes(await tg_file.download_as_bytearray())
    except Exception as exc:  # noqa: BLE001
        logger.exception("document download failed")
        await msg.reply_text(f"Couldn't download file: {exc}")
        return

    if len(data) > MAX_UPLOAD_BYTES:
        await msg.reply_text("That file is over 8MB — please send a smaller one.")
        return

    filename = doc.file_name or "file"
    mime = (doc.mime_type or "").lower()
    caption = (msg.caption or "").strip()

    # Image document → vision path
    if mime.startswith("image/") or filename.lower().endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
    ):
        mime_use = mime if mime.startswith("image/") else "image/jpeg"
        if filename.lower().endswith(".png"):
            mime_use = "image/png"
        elif filename.lower().endswith(".webp"):
            mime_use = "image/webp"
        elif filename.lower().endswith(".gif"):
            mime_use = "image/gif"
        b64 = base64.b64encode(data).decode("ascii")
        prompt = caption or "Describe this image and decipher what matters."
        await _stream_or_ask(
            update, context, user_message=prompt, images_b64=[(mime_use, b64)]
        )
        return

    extracted = extract_code_from_bytes(filename, data)
    prompt = (
        caption
        or f"Here is file {filename}; decipher what matters and answer."
    )
    if caption:
        prompt = (
            f"{caption}\n\nHere is file {filename}; decipher what matters and answer."
        )
    else:
        prompt = f"Here is file {filename}; decipher what matters and answer."

    await _stream_or_ask(
        update,
        context,
        user_message=prompt,
        extra_text=f"--- {filename} ---\n{extracted}",
    )


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

    # Reload from disk in case another worker/restart wrote it
    if message.message_id not in _collapse_store:
        _load_collapse_store()

    stored = _collapse_store.get(message.message_id)
    logger.info(
        "collapse tap data=%s mid=%s found=%s store_n=%d",
        data,
        message.message_id,
        bool(stored),
        len(_collapse_store),
    )
    if not stored:
        await query.answer(
            "Full text lost after a bot restart. Send the question again.",
            show_alert=True,
        )
        return

    full = str(stored["full"])
    preview = str(stored["preview"])

    try:
        if data == EXPAND_CB:
            await query.answer("Expanded")
            body = _cap(full)
            try:
                await message.edit_text(body, reply_markup=_collapse_keyboard())
            except BadRequest as exc:
                # If edit fails (e.g. too long / not modified), send as new messages
                logger.info("expand edit failed (%s); sending follow-ups", exc)
                await message.reply_text(body)
                # Keep preview message with collapse button state best-effort
                try:
                    await message.edit_reply_markup(reply_markup=_collapse_keyboard())
                except BadRequest:
                    pass
            # If truncated by Telegram limit, send remainder
            if len(full) > TELEGRAM_MAX_MESSAGE:
                rest = full[TELEGRAM_MAX_MESSAGE - 1 :]
                chunk_size = TELEGRAM_MAX_MESSAGE - 20
                for i in range(0, len(rest), chunk_size):
                    await message.reply_text(rest[i : i + chunk_size])
        elif data == COLLAPSE_CB:
            await query.answer("Collapsed")
            try:
                await message.edit_text(
                    preview,
                    reply_markup=_expand_keyboard(),
                )
            except BadRequest as exc:
                logger.info("collapse edit: %s", exc)
                await query.answer()
        else:
            await query.answer("Unknown button")
    except BadRequest as exc:
        logger.info("collapse callback edit_text: %s", exc)
        await query.answer()


async def _post_init(app: Application) -> None:
    commands = [
                BotCommand("balance", "Credits left + usage"),
BotCommand("reload", "Add credits + refresh bot"),
            BotCommand("download", "Download last code as file/zip"),
            BotCommand("attack", "New Guy suitcase zip (not trading)"),
            BotCommand("files", "List downloadable files"),
BotCommand("start", "Greet + reset memory"),
BotCommand("help", "Help: photos, files, toggles"),
BotCommand("reset", "Clear conversation memory"),
BotCommand("fast", "Toggle fast mode"),
BotCommand("compact", "Toggle compact replies"),
BotCommand("model", "Show or set chat model"),
            BotCommand("plan", "Rate plan: auto / fast / deep"),
            BotCommand("memory", "Show sticky memory + summary"),
            BotCommand("remember", "Save a sticky fact"),
            BotCommand("forget", "Clear sticky memory"),
            BotCommand("ops", "Queue work for desktop assistant"),
            BotCommand("paper", "Read-only paper trading status"),
            BotCommand("tstatus", "Short trading status (backup)"),
            BotCommand("project", "Set export project folder"),
            BotCommand("auto", "AUTO — switch Fast/Deep per message"),
            BotCommand("deep", "DEEP — maximum intelligence"),
BotCommand("status", "Bot health / prefs"),
BotCommand("upgrade", "Ask Grok for upgrade ideas"),
BotCommand("cmds", "List all commands"),
BotCommand("prefs", "Show toggles"),
BotCommand("keyboard", "Show/hide slim bar"),
            BotCommand("keys", "Open dropdown key panel"),
]
    try:
        await app.bot.set_my_commands(commands)
        logger.info("set_my_commands OK (%d commands)", len(commands))
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_my_commands failed: %s", exc)

    # Confirm identity without logging token
    try:
        me = await app.bot.get_me()
        logger.info(
            "Application started as @%s id=%s",
            me.username,
            me.id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("getMe failed: %s", exc)


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
        "Starting Telegram bot (model=%s vision=%s allowlist=%s)",
        settings["model"],
        settings["vision_model"],
        sorted(allow) if allow else "open",
    )

    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["client"] = client

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("fast", cmd_fast))
    app.add_handler(CommandHandler("compact", cmd_compact))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("prefs", cmd_prefs))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("upgrade", cmd_upgrade))
    app.add_handler(CommandHandler("cmds", cmd_cmds))
    app.add_handler(CommandHandler("keyboard", cmd_keyboard))
    app.add_handler(CommandHandler("keys", cmd_keys))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("files", cmd_files))
    app.add_handler(CommandHandler("download", cmd_download))
    app.add_handler(CommandHandler("attack", cmd_attack))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("ops", cmd_ops))
    app.add_handler(CommandHandler("paper", cmd_paper))
    app.add_handler(CommandHandler("tstatus", cmd_tstatus))
    app.add_handler(CommandHandler("project", cmd_project))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("remember", cmd_remember))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("auto", cmd_auto))
    app.add_handler(CommandHandler("deep", cmd_deep))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(
        CallbackQueryHandler(on_collapse_callback, pattern=r"^msg:(expand|collapse)$")
    )
    app.add_handler(
        CallbackQueryHandler(on_download_callback, pattern=r"^dl:")
    )
    app.add_handler(
        CallbackQueryHandler(on_keys_callback, pattern=r"^keys:")
    )
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("The New Guy Cruz — Telegram bot running (Ctrl+C to stop)")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
