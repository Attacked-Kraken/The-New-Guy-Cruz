#!/usr/bin/env python3
"""The New Guy Cruz — shared Grok/xAI client + CLI chat.

Secrets come from environment / .env only. Never hardcode API keys.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Sequence, Tuple, Union
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from openai import OpenAI

_ROOT = Path(__file__).resolve().parent
_DATA_DIR = _ROOT / "data"
_HISTORY_PATH = _DATA_DIR / "chat_history.json"

_PREFS_PATH = _DATA_DIR / "chat_prefs.json"
_USAGE_PATH = _DATA_DIR / "usage.json"
_PERSONAL_MEMORY_PATH = _DATA_DIR / "personal_memory.json"
_CHAT_SUMMARIES_PATH = _DATA_DIR / "chat_summaries.json"
_OPS_INBOX_PATH = _DATA_DIR / "ops_inbox.jsonl"

# Read-only paper status (never .env / keys)
CRUZBOT_INSTANCE_DATA = Path("/workspace/cruzbot_instance_2/data")
CRUZBOT_SAFE_STATUS_FILES = (
    "bot_heartbeat.json",
    "paper_book_2.json",
    "cb_auto_resume.json",
    "cb_phd_bypass.json",
    "wf_last_snapshot.json",
)

# Rolling summary: refresh when history exceeds this many stored messages (~8 turns)
SUMMARY_TRIGGER_MSGS = 16
# When a summary exists, shrink the recent API window further for FAST turns
SUMMARY_FAST_RECENT_TURNS = 6
SUMMARY_PROMPT = (
    "Summarize durable facts, open tasks, decisions, and code context in <=1200 chars. No fluff."
)

load_dotenv(_ROOT / ".env")

SYSTEM_PERSONA = (
    "You are The New Guy Cruz — a sharp, capable AI assistant with a bit of "
    "edge and zero corporate fluff. Be helpful, direct, and clear. Prefer "
    "actionable answers over lectures. Match the user's energy; keep humor "
    "dry when it fits. Admit uncertainty instead of bluffing. When the user "
    "asks for code or steps, give them cleanly.\n\n"
    "CRITICAL — memory: You receive prior turns in this chat. ALWAYS use that "
    "history. If the user follows up with short asks like \"what should I do\" "
    "or \"and then?\", treat the recent conversation (status dumps, images, "
    "code, decisions) as the subject. Never pretend you lack context that is "
    "already in the thread. Only ask for context when the history truly has none."
)

FAST_SYSTEM_ADDON = "Reply fast and tight; lead with the answer; skip fluff."
COMPACT_SYSTEM_ADDON = "Keep replies under ~600 chars unless asked for detail."
DEEP_SYSTEM_ADDON = (
    "Deep intelligence mode: reason carefully, check edge cases, and give a thorough\n    answer when the question is technical, high-stakes, or ambiguous. Still stay\n    clear — no filler."
)
AUTO_SYSTEM_ADDON = (
    "Rate plan is AUTO: the runtime picks Fast (cheap) or Deep (thorough) per message.\n    Match that choice — short when Fast, careful when Deep."
)

DEFAULT_MODEL = "grok-4.7"
DEFAULT_VISION_MODEL = "grok-4.6"
VISION_FALLBACKS = ("grok-4.6", "grok-2-vision-1212", "grok-2-vision")
DEFAULT_FAST_MODEL = "grok-4.20-0309-non-reasoning"
DEFAULT_DEEP_MODEL = "grok-4.7"
DEFAULT_BASE_URL = "https://api.x.ai/v1"
PREVIEW_CHARS = 280
TELEGRAM_MAX_MESSAGE = 4096
CODE_EXTRACT_CAP = 80_000
TEXT_FILE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".json", ".md", ".txt", ".csv",
    ".toml", ".yml", ".yaml", ".rs", ".go", ".sh", ".html", ".css",
    ".sql", ".env", ".ini", ".cfg", ".xml", ".rb", ".java", ".kt",
    ".swift", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".r",
}

ContentPart = Dict[str, Any]


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
        max_history = int(_env("MAX_HISTORY", "40") or "20")
    except ValueError:
        max_history = 40
    return {
        "api_key": api_key,
        "model": _env("XAI_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL,
        "vision_model": _env("XAI_VISION_MODEL", DEFAULT_VISION_MODEL) or DEFAULT_VISION_MODEL,
        "fast_model": _env("XAI_FAST_MODEL", DEFAULT_FAST_MODEL) or DEFAULT_FAST_MODEL,
        "deep_model": _env("XAI_DEEP_MODEL", DEFAULT_DEEP_MODEL) or DEFAULT_DEEP_MODEL,
        "base_url": _env("XAI_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL,
        "temperature": temperature,
        "max_history": max(2, max_history),
        "management_key": _env("XAI_MANAGEMENT_KEY"),
        "team_id": _env("XAI_TEAM_ID"),
    }


def reload_dotenv_settings() -> Dict[str, Any]:
    """Re-read .env into process env and return fresh settings (no secrets logged)."""
    load_dotenv(_ROOT / ".env", override=True)
    return get_settings()


def make_client(settings: Optional[Dict[str, Any]] = None) -> OpenAI:
    s = settings or get_settings()
    return OpenAI(api_key=s["api_key"], base_url=s["base_url"])


# Bounded conversation memory
_histories: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

def _load_histories() -> None:
    global _histories
    try:
        if not _HISTORY_PATH.is_file():
            return
        raw = json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        loaded: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for cid, turns in raw.items():
            if not isinstance(turns, list):
                continue
            clean = []
            for m in turns:
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                content = m.get("content")
                if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                    clean.append({"role": role, "content": content})
            if clean:
                # cap per chat at 80 messages (~40 turns)
                loaded[str(cid)] = clean[-80:]
        _histories = loaded
    except Exception:
        pass


def _save_histories() -> None:
    try:
        _ensure_data_dir()
        payload = {cid: msgs for cid, msgs in _histories.items() if msgs}
        _HISTORY_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

_chat_prefs: Dict[str, Dict[str, Any]] = defaultdict(dict)
_last_vision_error: Dict[str, str] = {}

# Token usage: session (process lifetime) + persisted totals
_usage_global = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
_usage_chat: Dict[str, Dict[str, int]] = defaultdict(
    lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
)
_last_quota_error: Optional[str] = None
_last_quota_at: Optional[float] = None

BOT_STARTED_AT = time.time()


def _ensure_data_dir() -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _load_prefs_file() -> None:
    if not _PREFS_PATH.is_file():
        return
    try:
        raw = json.loads(_PREFS_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for cid, prefs in raw.items():
                if isinstance(prefs, dict):
                    _chat_prefs[str(cid)] = dict(prefs)
    except Exception:
        pass


def _save_prefs_file() -> None:
    _ensure_data_dir()
    try:
        payload = {k: dict(v) for k, v in _chat_prefs.items() if v}
        _PREFS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_usage_file() -> None:
    if not _USAGE_PATH.is_file():
        return
    try:
        raw = json.loads(_USAGE_PATH.read_text(encoding="utf-8"))
        g = raw.get("global") or {}
        for k in ("prompt_tokens", "completion_tokens", "total_tokens", "calls"):
            if k in g:
                _usage_global[k] = int(g[k])
        for cid, u in (raw.get("chats") or {}).items():
            if isinstance(u, dict):
                _usage_chat[str(cid)] = {
                    "prompt_tokens": int(u.get("prompt_tokens", 0)),
                    "completion_tokens": int(u.get("completion_tokens", 0)),
                    "total_tokens": int(u.get("total_tokens", 0)),
                    "calls": int(u.get("calls", 0)),
                }
    except Exception:
        pass


def _save_usage_file() -> None:
    _ensure_data_dir()
    try:
        payload = {
            "global": dict(_usage_global),
            "chats": {k: dict(v) for k, v in _usage_chat.items()},
        }
        _USAGE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


_load_prefs_file()
_load_usage_file()


def get_chat_prefs(chat_id: str) -> Dict[str, Any]:
    key = str(chat_id)
    prefs = _chat_prefs[key]
    # rate_plan: auto | fast | deep  (default auto — min burn unless message needs depth)
    plan = str(prefs.get("rate_plan") or "auto").lower().strip()
    if plan not in ("auto", "fast", "deep"):
        plan = "auto"
    fast = bool(prefs.get("fast", False))
    compact = bool(prefs.get("compact", False))
    # Legacy toggles still work; explicit rate_plan wins when set via /plan
    project = prefs.get("project_name")
    if project is not None:
        project = str(project).strip() or None
    return {
        "fast": fast,
        "compact": compact,
        "rate_plan": plan,
        "model": prefs.get("model"),
        "max_history": prefs.get("max_history"),
        "keyboard": bool(prefs.get("keyboard", True)),
        "project_name": project,
        "last_turn_plan": prefs.get("_last_turn_plan"),
        "last_turn_source": prefs.get("_last_turn_source"),
    }


def set_chat_pref(chat_id: str, name: str, value: Any) -> Dict[str, Any]:
    key = str(chat_id)
    allowed = {"fast", "compact", "model", "max_history", "keyboard", "rate_plan", "project_name"}
    if name not in allowed:
        raise ValueError(f"Unknown pref: {name}")
    if value is None and name in ("model", "max_history", "project_name"):
        _chat_prefs[key].pop(name, None)
    else:
        _chat_prefs[key][name] = value
    # Keep legacy fast/compact aligned when rate_plan is set explicitly
    if name == "rate_plan":
        plan = str(value or "auto").lower().strip()
        if plan == "fast":
            _chat_prefs[key]["fast"] = True
            _chat_prefs[key]["compact"] = True
        elif plan == "deep":
            _chat_prefs[key]["fast"] = False
            _chat_prefs[key]["compact"] = False
        elif plan == "auto":
            # auto decides per message; leave compact on as a soft default for cheap turns
            _chat_prefs[key]["fast"] = False
            _chat_prefs[key]["compact"] = True
    _save_prefs_file()
    return get_chat_prefs(key)


def reset_history(chat_id: str) -> None:
    """Clear conversation history AND rolling summary. Sticky personal memory is kept."""
    key = str(chat_id)
    _histories.pop(key, None)
    _save_histories()
    clear_chat_summary(key)


def get_history(chat_id: str) -> List[Dict[str, Any]]:
    return list(_histories.get(str(chat_id), []))


def history_len(chat_id: str) -> int:
    return len(_histories.get(str(chat_id), []))


def _trim_history(chat_id: str, max_history: int) -> None:
    key = str(chat_id)
    hist = _histories[key]
    max_msgs = max_history * 2
    if len(hist) > max_msgs:
        _histories[key] = hist[-max_msgs:]


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ----- Sticky personal memory (survives /reset) -----

_personal_memory: Dict[str, Dict[str, Any]] = {}
_personal_memory_lock = threading.Lock()


def _load_personal_memory() -> None:
    global _personal_memory
    try:
        if not _PERSONAL_MEMORY_PATH.is_file():
            _personal_memory = {}
            return
        raw = json.loads(_PERSONAL_MEMORY_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            _personal_memory = {}
            return
        clean: Dict[str, Dict[str, Any]] = {}
        for cid, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            facts = entry.get("facts") or []
            if not isinstance(facts, list):
                facts = []
            facts = [str(f).strip() for f in facts if str(f).strip()]
            clean[str(cid)] = {
                "facts": facts,
                "updated": str(entry.get("updated") or _iso_now()),
            }
        _personal_memory = clean
    except Exception:
        _personal_memory = {}


def _save_personal_memory() -> None:
    try:
        _ensure_data_dir()
        with _personal_memory_lock:
            payload = {k: dict(v) for k, v in _personal_memory.items() if v.get("facts")}
        _PERSONAL_MEMORY_PATH.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def get_personal_memory(chat_id: str) -> Dict[str, Any]:
    key = str(chat_id)
    with _personal_memory_lock:
        entry = _personal_memory.get(key)
        if not entry:
            return {"facts": [], "updated": ""}
        return {"facts": list(entry.get("facts") or []), "updated": str(entry.get("updated") or "")}


def add_personal_fact(chat_id: str, fact: str) -> Dict[str, Any]:
    key = str(chat_id)
    fact = (fact or "").strip()
    if not fact:
        raise ValueError("empty fact")
    # Cap length; never store huge blobs
    if len(fact) > 500:
        fact = fact[:497].rstrip() + "…"
    with _personal_memory_lock:
        entry = _personal_memory.setdefault(key, {"facts": [], "updated": _iso_now()})
        facts = list(entry.get("facts") or [])
        if fact not in facts:
            facts.append(fact)
        # soft cap
        if len(facts) > 40:
            facts = facts[-40:]
        entry["facts"] = facts
        entry["updated"] = _iso_now()
        _personal_memory[key] = entry
        out = {"facts": list(facts), "updated": entry["updated"]}
    _save_personal_memory()
    return out


def clear_personal_memory(chat_id: str, *, last_only: bool = False) -> Dict[str, Any]:
    key = str(chat_id)
    with _personal_memory_lock:
        entry = _personal_memory.get(key)
        if not entry or not entry.get("facts"):
            _personal_memory.pop(key, None)
            out = {"facts": [], "updated": ""}
        elif last_only:
            facts = list(entry.get("facts") or [])
            if facts:
                facts.pop()
            if facts:
                entry["facts"] = facts
                entry["updated"] = _iso_now()
                _personal_memory[key] = entry
                out = {"facts": list(facts), "updated": entry["updated"]}
            else:
                _personal_memory.pop(key, None)
                out = {"facts": [], "updated": ""}
        else:
            _personal_memory.pop(key, None)
            out = {"facts": [], "updated": ""}
    _save_personal_memory()
    return out


def format_personal_memory_block(chat_id: str) -> str:
    mem = get_personal_memory(chat_id)
    facts = mem.get("facts") or []
    if not facts:
        return ""
    lines = ["STICKY MEMORY (survives /reset):"]
    for f in facts:
        lines.append(f"- {f}")
    return "\n".join(lines)


# ----- Rolling conversation summaries -----

def ensure_hands_off_defaults(chat_id: str) -> Dict[str, Any]:
    """Zero-touch defaults: AUTO rate plan, keyboard on, soft compact, no model override."""
    key = str(chat_id)
    prefs = _chat_prefs[key]
    changed = False
    if prefs.get("rate_plan") not in ("auto", "fast", "deep"):
        prefs["rate_plan"] = "auto"
        changed = True
    if "rate_plan" not in prefs:
        prefs["rate_plan"] = "auto"
        changed = True
    # If never explicitly set away from auto, keep auto
    if prefs.get("rate_plan") is None:
        prefs["rate_plan"] = "auto"
        changed = True
    if not prefs.get("keyboard", True):
        # only force keyboard on once for hands-off
        pass
    if "keyboard" not in prefs:
        prefs["keyboard"] = True
        changed = True
    if "compact" not in prefs:
        prefs["compact"] = True
        changed = True
    if "fast" not in prefs and prefs.get("rate_plan", "auto") == "auto":
        prefs["fast"] = False
        changed = True
    if changed:
        _save_prefs_file()
    # Seed sticky memory once with lean-burn prefs if empty
    mem = get_personal_memory(key)
    if not (mem.get("facts") or []):
        add_personal_fact(key, "Hands-off mode: keep rate plan AUTO; pick Fast or Deep per message.")
        add_personal_fact(key, "Prefer minimum token burn for everyday chat; use Deep only when the question is hard.")
        add_personal_fact(key, "User is Apex Signals; New Guy is Telegram Grok — desktop/ops work goes via /ops to Attacked Kraken.")
    return get_chat_prefs(key)




_chat_summaries: Dict[str, Dict[str, Any]] = {}
_chat_summaries_lock = threading.Lock()
_summary_inflight: set = set()


def _load_chat_summaries() -> None:
    global _chat_summaries
    try:
        if not _CHAT_SUMMARIES_PATH.is_file():
            _chat_summaries = {}
            return
        raw = json.loads(_CHAT_SUMMARIES_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            _chat_summaries = {}
            return
        clean: Dict[str, Dict[str, Any]] = {}
        for cid, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            summary = str(entry.get("summary") or "").strip()
            if not summary:
                continue
            try:
                covered = int(entry.get("covered_turns") or 0)
            except (TypeError, ValueError):
                covered = 0
            clean[str(cid)] = {
                "summary": summary,
                "covered_turns": covered,
                "updated": str(entry.get("updated") or _iso_now()),
            }
        _chat_summaries = clean
    except Exception:
        _chat_summaries = {}


def _save_chat_summaries() -> None:
    try:
        _ensure_data_dir()
        with _chat_summaries_lock:
            payload = {k: dict(v) for k, v in _chat_summaries.items() if v.get("summary")}
        _CHAT_SUMMARIES_PATH.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def get_chat_summary(chat_id: str) -> Optional[Dict[str, Any]]:
    key = str(chat_id)
    with _chat_summaries_lock:
        entry = _chat_summaries.get(key)
        if not entry or not entry.get("summary"):
            return None
        return {
            "summary": str(entry["summary"]),
            "covered_turns": int(entry.get("covered_turns") or 0),
            "updated": str(entry.get("updated") or ""),
        }


def clear_chat_summary(chat_id: str) -> None:
    key = str(chat_id)
    with _chat_summaries_lock:
        _chat_summaries.pop(key, None)
        _summary_inflight.discard(key)
    _save_chat_summaries()


def _set_chat_summary(chat_id: str, summary: str, covered_turns: int) -> None:
    key = str(chat_id)
    summary = (summary or "").strip()
    if len(summary) > 1400:
        summary = summary[:1397].rstrip() + "…"
    with _chat_summaries_lock:
        if not summary:
            _chat_summaries.pop(key, None)
        else:
            _chat_summaries[key] = {
                "summary": summary,
                "covered_turns": int(covered_turns),
                "updated": _iso_now(),
            }
    _save_chat_summaries()


def _format_turns_for_summary(msgs: List[Dict[str, Any]], cap_chars: int = 6000) -> str:
    parts: List[str] = []
    total = 0
    for m in msgs:
        role = m.get("role") or "?"
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        if len(content) > 800:
            content = content[:797].rstrip() + "…"
        line = f"{role}: {content}"
        if total + len(line) + 1 > cap_chars:
            break
        parts.append(line)
        total += len(line) + 1
    return "\n".join(parts)


def refresh_chat_summary(
    chat_id: str,
    *,
    client: Optional[OpenAI] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Summarize older turns with the fast model. No-op if history is short."""
    key = str(chat_id)
    hist = list(_histories.get(key, []))
    if len(hist) < SUMMARY_TRIGGER_MSGS:
        return None
    # Keep last ~6 messages (3 turns) out of the summary so recent window stays fresh
    keep_recent = 6
    older = hist[:-keep_recent] if len(hist) > keep_recent else hist[:-2]
    if len(older) < 4:
        return None
    existing = get_chat_summary(key)
    prev = (existing or {}).get("summary") or ""
    body = _format_turns_for_summary(older)
    if not body.strip():
        return None
    prompt_user = (
        (f"Previous summary:\n{prev}\n\n" if prev else "")
        + f"Conversation excerpt to fold in:\n{body}"
    )
    s = settings or get_settings()
    cli = client or make_client(s)
    model = s.get("fast_model") or s["model"]
    try:
        resp = cli.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": prompt_user},
            ],
            temperature=0.2,
        )
        summary = (resp.choices[0].message.content or "").strip()
        record_usage(key, getattr(resp, "usage", None))
    except Exception:
        return None
    if not summary:
        return None
    covered_turns = max(0, (len(hist) - keep_recent) // 2)
    _set_chat_summary(key, summary, covered_turns)
    return summary


def _maybe_refresh_summary_async(
    chat_id: str,
    *,
    client: Optional[OpenAI] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> None:
    key = str(chat_id)
    if len(_histories.get(key, [])) < SUMMARY_TRIGGER_MSGS:
        return
    with _chat_summaries_lock:
        if key in _summary_inflight:
            return
        _summary_inflight.add(key)

    def _worker() -> None:
        try:
            refresh_chat_summary(key, client=client, settings=settings)
        except Exception:
            pass
        finally:
            with _chat_summaries_lock:
                _summary_inflight.discard(key)

    threading.Thread(target=_worker, daemon=True, name=f"summary-{key}").start()


def _api_history_with_summary(
    chat_id: str,
    turn_history: List[Dict[str, Any]],
    max_hist: int,
    *,
    use_fast: bool = False,
) -> List[Dict[str, Any]]:
    """Build history slice for the API, injecting summary when present."""
    summary_entry = get_chat_summary(chat_id)
    recent_turns = max(2, int(max_hist))
    if summary_entry and use_fast:
        recent_turns = min(recent_turns, SUMMARY_FAST_RECENT_TURNS)
    max_msgs = recent_turns * 2
    recent = turn_history[-max_msgs:]
    if not summary_entry:
        return recent
    note = (
        "Earlier conversation summary (older turns compressed; sticky memory is separate):\n"
        + str(summary_entry["summary"])
    )
    # Synthetic pair so role alternation stays sane for chat APIs
    return [
        {"role": "user", "content": note},
        {"role": "assistant", "content": "Got it — I'll use that summary as context for older turns."},
        *recent,
    ]


# ----- Ops handoff inbox (Attacked Kraken Reboot) -----

_ops_lock = threading.Lock()
_SECRETISH = re.compile(
    r"(?i)(xai-|sk-|Bearer\s+|api[_-]?key\s*[:=]\s*|TELEGRAM_BOT_TOKEN\s*[:=]\s*)"
    r"[A-Za-z0-9_\-]{8,}"
)


def _scrub_ops_text(text: str) -> str:
    """Never put .env secrets into ops lines."""
    scrubbed = _SECRETISH.sub("[REDACTED]", text or "")
    # Also redact long token-looking blobs
    scrubbed = re.sub(r"\b[A-Za-z0-9_\-]{40,}\b", "[REDACTED_LONG]", scrubbed)
    return scrubbed.strip()


def append_ops(chat_id: str, text: str, user_id: Optional[int] = None) -> Dict[str, Any]:
    cleaned = _scrub_ops_text(text)
    if not cleaned:
        raise ValueError("empty ops request")
    if len(cleaned) > 2000:
        cleaned = cleaned[:1997].rstrip() + "…"
    entry = {
        "ts": _iso_now(),
        "chat_id": str(chat_id),
        "user_id": int(user_id) if user_id is not None else None,
        "text": cleaned,
        "status": "open",
    }
    _ensure_data_dir()
    line = json.dumps(entry, ensure_ascii=False)
    with _ops_lock:
        with _OPS_INBOX_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return entry


def list_ops_inbox(limit: int = 20, *, chat_id: Optional[str] = None, open_only: bool = True) -> List[Dict[str, Any]]:
    if not _OPS_INBOX_PATH.is_file():
        return []
    items: List[Dict[str, Any]] = []
    try:
        with _OPS_INBOX_PATH.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if chat_id is not None and str(obj.get("chat_id")) != str(chat_id):
                    continue
                if open_only and str(obj.get("status") or "open") != "open":
                    continue
                items.append(obj)
    except Exception:
        return []
    if limit <= 0:
        return items
    return items[-limit:]


def record_usage(chat_id: str, usage: Any) -> None:
    """Record prompt/completion/total tokens from API usage object if present."""
    if usage is None:
        return
    try:
        pt = int(getattr(usage, "prompt_tokens", None) or usage.get("prompt_tokens") or 0)  # type: ignore[union-attr]
        ct = int(getattr(usage, "completion_tokens", None) or usage.get("completion_tokens") or 0)  # type: ignore[union-attr]
        tt = int(getattr(usage, "total_tokens", None) or usage.get("total_tokens") or (pt + ct))  # type: ignore[union-attr]
    except Exception:
        return
    key = str(chat_id)
    for bucket in (_usage_global, _usage_chat[key]):
        bucket["prompt_tokens"] += pt
        bucket["completion_tokens"] += ct
        bucket["total_tokens"] += tt
        bucket["calls"] += 1
    _save_usage_file()


def get_usage(chat_id: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "session_started_at": BOT_STARTED_AT,
        "global": dict(_usage_global),
    }
    if chat_id is not None:
        out["chat"] = dict(_usage_chat.get(str(chat_id), {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0,
        }))
    return out


def note_quota_error(exc: BaseException) -> None:
    global _last_quota_error, _last_quota_at
    msg = str(exc)
    low = msg.lower()
    if "429" in msg or "insufficient_quota" in low or "rate" in low or "quota" in low:
        _last_quota_error = msg[:400]
        _last_quota_at = time.time()


def get_last_quota_error() -> Optional[Dict[str, Any]]:
    if not _last_quota_error:
        return None
    return {"message": _last_quota_error, "at": _last_quota_at}


def fetch_prepaid_balance(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Fetch prepaid balance via management API. Never logs the key.

    Docs: total.val is USD cents with inverted sign (-2500 => $25.00 remaining).
    """
    s = settings or get_settings()
    mgmt = s.get("management_key")
    team = s.get("team_id")
    if not mgmt or not team:
        return {
            "ok": False,
            "reason": "missing_creds",
            "detail": "Set XAI_MANAGEMENT_KEY + XAI_TEAM_ID for live prepaid balance.",
        }
    import httpx

    url = f"https://management-api.x.ai/v1/billing/teams/{team}/prepaid/balance"
    try:
        with httpx.Client(timeout=20.0) as http:
            resp = http.get(url, headers={"Authorization": f"Bearer {mgmt}"})
        if resp.status_code != 200:
            return {
                "ok": False,
                "reason": "http_error",
                "status": resp.status_code,
                "detail": (resp.text or "")[:300],
            }
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "request_failed", "detail": str(exc)[:300]}

    # Parse total.val — inverted cents
    cents_raw = None
    try:
        total = data.get("total") if isinstance(data, dict) else None
        if isinstance(total, dict) and "val" in total:
            cents_raw = int(total["val"])
        elif isinstance(data, dict) and "val" in data:
            cents_raw = int(data["val"])
        elif isinstance(data, (int, float)):
            cents_raw = int(data)
    except (TypeError, ValueError):
        cents_raw = None

    dollars_left = None
    if cents_raw is not None:
        # Inverted sign: negative cents => positive dollars remaining
        dollars_left = abs(cents_raw) / 100.0
        # If positive raw unexpectedly, still show abs as dollars but flag
        sign_note = "inverted_cents" if cents_raw <= 0 else "positive_raw_cents"

    return {
        "ok": True,
        "raw_cents": cents_raw,
        "dollars_left": dollars_left,
        "sign_note": sign_note if cents_raw is not None else None,
        "raw": data,
    }


def format_balance_report(chat_id: str, settings: Optional[Dict[str, Any]] = None) -> str:
    s = settings or get_settings()
    usage = get_usage(chat_id)
    g = usage["global"]
    c = usage["chat"]
    bal = fetch_prepaid_balance(s)

    if bal.get("ok") and bal.get("dollars_left") is not None:
        credit_line = f"Credits left: ${bal['dollars_left']:.2f}"
        # 0 with empty ledger usually means Management key is on an unfunded team
        if float(bal.get("dollars_left") or 0) == 0 and not (bal.get("raw") or {}).get("changes"):
            credit_line += "\n(No prepaid on this Management key's team — use the team that holds your credits.)"
    else:
        credit_line = "Credits left: unavailable"

    lines = [
        credit_line,
        "",
        "Usage",
        f"This chat: {c['total_tokens']} tokens ({c['calls']} calls)",
        f"All chats: {g['total_tokens']} tokens ({g['calls']} calls)",
        "",
        "/reload — add credits",
    ]
    return "\n".join(lines)


def extract_code_from_bytes(filename: str, data: bytes) -> str:
    name = (filename or "file").strip() or "file"
    lower = name.lower()
    ext = ""
    if "." in lower:
        ext = "." + lower.rsplit(".", 1)[-1]
    binary_exts = {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
        ".pdf", ".zip", ".gz", ".tar", ".bz2", ".7z", ".rar",
        ".exe", ".dll", ".so", ".dylib", ".bin", ".woff", ".woff2",
        ".mp3", ".mp4", ".mov", ".avi", ".wav", ".ogg",
    }
    if ext in binary_exts:
        return f"[Cannot extract text from binary file {name}]"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("latin-1")
        except Exception:
            return f"[Could not decode {name} as text]"
    note = ""
    if len(text) > CODE_EXTRACT_CAP:
        text = text[:CODE_EXTRACT_CAP]
        note = f"\n\n[Truncated to {CODE_EXTRACT_CAP} chars]"
    if ext and ext not in TEXT_FILE_EXTS and ext not in binary_exts:
        return f"[File {name} (ext {ext})]\n{text}{note}"
    return f"{text}{note}"


def _build_user_content(
    user_message: str,
    *,
    images_b64: Optional[Sequence[Tuple[str, str]]] = None,
    extra_text: Optional[str] = None,
    content_parts: Optional[List[ContentPart]] = None,
) -> Union[str, List[ContentPart]]:
    if content_parts is not None:
        return content_parts
    text_bits: List[str] = []
    if user_message and user_message.strip():
        text_bits.append(user_message.strip())
    if extra_text and extra_text.strip():
        text_bits.append(extra_text.strip())
    combined = "\n\n".join(text_bits) if text_bits else "(see attached)"
    images = list(images_b64 or [])
    if not images:
        return combined
    parts: List[ContentPart] = [{"type": "text", "text": combined}]
    for mime, b64 in images:
        mime = (mime or "image/jpeg").strip() or "image/jpeg"
        raw = b64.strip()
        if raw.startswith("data:"):
            url = raw
        else:
            url = f"data:{mime};base64,{raw}"
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def _history_text_for_store(user_content: Union[str, List[ContentPart]]) -> str:
    if isinstance(user_content, str):
        return user_content
    texts = []
    n_img = 0
    for part in user_content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            texts.append(str(part.get("text") or ""))
        elif part.get("type") == "image_url":
            n_img += 1
    base = "\n".join(t for t in texts if t).strip() or "(image message)"
    if n_img:
        base = f"{base}\n[{n_img} image(s) attached]"
    if len(base) > 4000:
        base = base[:4000] + "…"
    return base



_DEEP_HINTS = re.compile(
    r"(?i)\b("
    r"debug|bug|fix|crash|error|traceback|stack\s*trace|"
    r"architect|design|refactor|implement|algorithm|prove|why\s+does|"
    r"risk|drawdown|sharpe|sortino|backtest|quant|phd|strategy|"
    r"security|exploit|vuln|auth|token\s+leak|"
    r"deep|thorough|careful|think\s+hard|reason\s+step|"
    r"compare\s+options|trade[- ]?off|root\s*cause|diagnose|"
    r"write\s+(a\s+)?(full|complete|production)|multi[- ]?file|"
    r"code\s+review|pull\s*request|diff\s+analysis|"
    r"search|look\s*up|latest|current\s+(news|price|events?)|"
    r"desktop|box|repo|restart|deploy"
    r")\b"
)
_FAST_HINTS = re.compile(
    r"(?i)^(?:ok|thanks|thx|yes|no|yep|nope|k|got\s*it|cool|hi|hey|hello|"
    r"ping|status|balance|help|cmds?)\s*[!.]?$|"
    r"\b(?:quick|brief|tl;?dr|one\s*line|short\s*answer|just\s*tell\s*me)\b"
)


def classify_rate_need(
    user_text: str,
    *,
    has_images: bool = False,
    has_code_file: bool = False,
) -> str:
    """Return 'fast' or 'deep' for this turn (used when rate_plan=auto)."""
    text = (user_text or "").strip()
    if has_images:
        return "deep"  # vision is costly; treat as intentional / high-value
    if has_code_file and len(text) > 40:
        return "deep"
    if _FAST_HINTS.search(text) and not _DEEP_HINTS.search(text):
        return "fast"
    if _DEEP_HINTS.search(text):
        return "deep"
    # Long / multi-question messages need depth
    if len(text) >= 500 or text.count("?") >= 2:
        return "deep"
    if "```" in text and len(text) > 200:
        return "deep"
    return "fast"


def resolve_turn_plan(
    chat_id: str,
    user_text: str,
    *,
    has_images: bool = False,
    has_code_file: bool = False,
) -> Dict[str, Any]:
    """Pick effective plan for this turn and stamp last_plan on prefs (in-memory note)."""
    prefs = get_chat_prefs(chat_id)
    plan = prefs["rate_plan"]
    if plan == "auto":
        chosen = classify_rate_need(
            user_text, has_images=has_images, has_code_file=has_code_file
        )
        source = "auto"
    elif plan == "deep":
        chosen = "deep"
        source = "forced"
    else:
        chosen = "fast"
        source = "forced"
    # stamp for /prefs /status (not persisted as sticky choice)
    _chat_prefs[str(chat_id)]["_last_turn_plan"] = chosen
    _chat_prefs[str(chat_id)]["_last_turn_source"] = source
    return {
        "rate_plan": plan,
        "chosen": chosen,
        "source": source,
        "fast": chosen == "fast",
        "compact": chosen == "fast" or prefs["compact"],
        "deep": chosen == "deep",
    }


def _resolve_system(chat_id: str, system: Optional[str], turn: Optional[Dict[str, Any]] = None) -> str:
    """Build system prompt with a stable prefix for prompt-cache hits.

    Order: sticky memory (if any) + persona first, then rate-plan addons.
    Sticky changes infrequently; persona is constant — keep them at the front.
    """
    prefs = get_chat_prefs(chat_id)
    parts: List[str] = []
    # Prompt-cache friendly prefix: sticky + persona first
    sticky = format_personal_memory_block(chat_id)
    if sticky:
        parts.append(sticky)
    parts.append(system or SYSTEM_PERSONA)
    if prefs.get("rate_plan") == "auto":
        parts.append(AUTO_SYSTEM_ADDON)
    use_fast = bool(turn and turn.get("fast")) if turn is not None else prefs["fast"]
    use_compact = bool(turn and turn.get("compact")) if turn is not None else prefs["compact"]
    use_deep = bool(turn and turn.get("deep")) if turn is not None else (prefs["rate_plan"] == "deep")
    if use_deep:
        parts.append(DEEP_SYSTEM_ADDON)
    if use_fast:
        parts.append(FAST_SYSTEM_ADDON)
    if use_compact:
        parts.append(COMPACT_SYSTEM_ADDON)
    return "\n\n".join(parts)


def _resolve_runtime(
    chat_id: str,
    settings: Dict[str, Any],
    *,
    has_images: bool,
    turn: Optional[Dict[str, Any]] = None,
) -> Tuple[str, float, int, List[str]]:
    prefs = get_chat_prefs(chat_id)
    temp = settings["temperature"]
    max_hist = prefs["max_history"] if prefs["max_history"] is not None else settings["max_history"]
    vision_try: List[str] = []
    if has_images:
        ordered = [settings["vision_model"], *VISION_FALLBACKS]
        seen = set()
        for m in ordered:
            if m and m not in seen:
                seen.add(m)
                vision_try.append(m)
    use_fast = bool(turn and turn.get("fast")) if turn is not None else prefs["fast"]
    use_deep = bool(turn and turn.get("deep")) if turn is not None else (prefs["rate_plan"] == "deep")
    if prefs["model"]:
        model = str(prefs["model"])
    elif use_fast and not has_images:
        model = settings.get("fast_model") or settings["model"]
        temp = min(temp, 0.35)
        max_hist = min(int(max_hist), 10)
    elif use_deep and not has_images:
        model = settings.get("deep_model") or settings["model"]
        temp = max(temp, 0.55)
        # keep fuller history for deep reasoning
        max_hist = max(int(max_hist), min(int(settings["max_history"]), 30))
    else:
        model = settings["model"]
    if use_fast:
        temp = min(temp, 0.35)
        max_hist = min(int(max_hist), 10)
    return model, float(temp), int(max_hist), vision_try


def tools_enabled() -> bool:
    """NEWGUY_TOOLS env flag — default ON."""
    raw = (_env("NEWGUY_TOOLS", "1") or "1").strip().lower()
    return raw not in {"0", "false", "off", "no", "disabled"}


_SEARCH_HINTS = re.compile(
    r"(?i)\b("
    r"search|look\s*up|google|web\s*search|find\s+online|"
    r"latest|current\s+(news|price|events?|status)|what.?s\s+happening|"
    r"as\s+of\s+today|recent\s+(news|updates?)"
    r")\b"
)

_OPS_HANDOFF_HINTS = re.compile(
    r"(?i)(?:\b(?:desktop|box|repo|restart|deploy)\b|"
    r"do\s+it\s+on\s+the\s+computer|"
    r"on\s+the\s+(?:box|desktop|computer))"
)

# OpenAI-compatible function tool (fallback when xAI server-side web_search unavailable)
_WEB_SEARCH_FUNCTION_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the public web for current information. "
            "Use when the user asks for latest/current facts, news, or to search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query string",
                }
            },
            "required": ["query"],
        },
    },
}


def wants_web_search(user_text: str) -> bool:
    return bool(_SEARCH_HINTS.search(user_text or ""))


def should_silent_ops(user_text: str) -> bool:
    """True when message looks like a desktop/box handoff — not trivial chat."""
    text = (user_text or "").strip()
    if not text or len(text) < 12:
        return False
    # Skip short acknowledgements / greetings
    if _FAST_HINTS.search(text) and len(text) < 48 and not _OPS_HANDOFF_HINTS.search(text):
        return False
    return bool(_OPS_HANDOFF_HINTS.search(text))


def maybe_silent_ops_handoff(
    chat_id: str,
    user_text: str,
    user_id: Optional[int] = None,
) -> Optional[str]:
    """Auto-queue ops for desktop work; return footer line or None."""
    if not should_silent_ops(user_text):
        return None
    try:
        append_ops(chat_id, user_text, user_id=user_id)
    except Exception:
        return None
    return "Queued for Attacked Kraken — say check ops there."




def _scrub_status_value(val: Any) -> Any:
    """Scrub secret-looking strings from status payloads."""
    if isinstance(val, dict):
        out = {}
        for k, v in val.items():
            lk = str(k).lower()
            if any(x in lk for x in ("key", "secret", "token", "password", "api_key", "auth")):
                out[k] = "[REDACTED]"
            else:
                out[k] = _scrub_status_value(v)
        return out
    if isinstance(val, list):
        return [_scrub_status_value(v) for v in val[:50]]
    if isinstance(val, str):
        return _scrub_ops_text(val)
    return val


def format_tstatus() -> str:
    """Short Codespace-style trading status for /tstatus (read-only, no secrets)."""
    import time as _time
    from datetime import datetime, timezone

    root = CRUZBOT_INSTANCE_DATA
    if not root.is_dir():
        return "No cruzbot data at /workspace/cruzbot_instance_2/data"

    def _load(name: str):
        fp = root / name
        if not fp.is_file():
            return None
        try:
            return json.loads(fp.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return None

    hb = _load("bot_heartbeat.json") or {}
    book = _load("paper_book_2.json") or {}
    cb = _load("cb_auto_resume.json") or {}
    phd = _load("cb_phd_bypass.json") or {}

    mode = hb.get("mode") or ("PAPER" if book else "?")
    pid = hb.get("pid")
    open_pos = hb.get("open_positions")
    if open_pos is None:
        positions = book.get("positions") or {}
        open_pos = len(positions) if isinstance(positions, (dict, list)) else "?"
    cash = book.get("cash")
    try:
        cash_s = f"${float(cash):,.2f}" if cash is not None else "?"
    except (TypeError, ValueError):
        cash_s = "?"
    consec = book.get("consecutive_losses")
    # Age from heartbeat unix/ts
    age_s = None
    try:
        if hb.get("unix") is not None:
            age_s = max(0.0, _time.time() - float(hb["unix"]))
        elif hb.get("ts"):
            ts = str(hb["ts"]).replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_s = max(0.0, _time.time() - dt.timestamp())
    except Exception:
        age_s = None
    age_txt = f"{age_s:.0f}s ago" if age_s is not None else "unknown"

    # CB paused?
    paused = None
    if isinstance(cb, dict) and cb:
        paused = cb.get("paused")
        if paused is None:
            paused = cb.get("cb_active")
    # Fallback: consec >= 3 often means CB trip in paper
    if paused is None and consec is not None:
        try:
            paused = int(consec) >= 3
        except (TypeError, ValueError):
            paused = None
    paused_s = {True: "YES", False: "no", None: "?"}.get(paused, str(paused))

    tg = hb.get("tg_listener") or "?"
    last_closes = []
    closed = book.get("closed_trades") or []
    if isinstance(closed, list) and closed:
        for t in closed[-3:][::-1]:
            if not isinstance(t, dict):
                continue
            sym = t.get("symbol") or "?"
            pnl = t.get("pnl")
            try:
                pnl_s = f"${float(pnl):+.2f}" if pnl is not None else "?"
            except (TypeError, ValueError):
                pnl_s = "?"
            tag = "WIN" if t.get("won") else ("LOSS" if t.get("won") is False else "")
            last_closes.append(f"{sym} {pnl_s} {tag}".strip())
    closes_line = ", ".join(last_closes) if last_closes else "(none)"

    phd_flag = ""
    if isinstance(phd, dict) and phd.get("phd_weak_bypass_after_cb"):
        phd_flag = " | PHD bypass on"

    lines = [
        f"Trading /tstatus (read-only)",
        f"mode={mode} pid={pid} hb={age_txt}",
        f"open={open_pos} cash={cash_s} consec_loss={consec}",
        f"CB paused={paused_s} | TG={tg}{phd_flag}",
        f"last closes: {closes_line}",
    ]
    return "\n".join(lines)


def format_paper_status() -> str:
    """Read-only paper/trading status from cruzbot_instance_2 public-ish JSON.

    NEVER reads .env, keys, or writes. Scrubs token-like strings.
    """
    root = CRUZBOT_INSTANCE_DATA
    if not root.is_dir():
        return (
            "No cruzbot status files found at /workspace/cruzbot_instance_2/data.\n"
            "Paste /status from the trading bot here, or ensure that instance is on this box."
        )
    blocks: List[str] = ["Paper status (read-only, scrubbed):"]
    found = 0
    for name in CRUZBOT_SAFE_STATUS_FILES:
        fp = root / name
        if not fp.is_file():
            continue
        try:
            raw = fp.read_text(encoding="utf-8", errors="replace")
            if len(raw) > 200_000:
                blocks.append(f"\n[{name}] skipped (too large)")
                continue
            data = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            blocks.append(f"\n[{name}] unreadable ({type(exc).__name__})")
            continue
        data = _scrub_status_value(data)
        found += 1
        if name == "bot_heartbeat.json" and isinstance(data, dict):
            blocks.append(
                "\nHeartbeat:\n"
                f"  mode: {data.get('mode')}\n"
                f"  ts: {data.get('ts')}\n"
                f"  open_positions: {data.get('open_positions')}\n"
                f"  dead_man: {data.get('dead_man')}"
            )
        elif name == "paper_book_2.json" and isinstance(data, dict):
            positions = data.get("positions") or {}
            if isinstance(positions, dict):
                n_pos = len(positions)
            elif isinstance(positions, list):
                n_pos = len(positions)
            else:
                n_pos = "?"
            closed = data.get("closed_trades") or []
            n_closed = len(closed) if isinstance(closed, list) else "?"
            blocks.append(
                "\nPaper book:\n"
                f"  cash: {data.get('cash')}\n"
                f"  day_start_equity: {data.get('day_start_equity')}\n"
                f"  consecutive_losses: {data.get('consecutive_losses')}\n"
                f"  open_positions: {n_pos}\n"
                f"  closed_trades: {n_closed}\n"
                f"  updated_at: {data.get('updated_at')}"
            )
        elif name == "cb_auto_resume.json" and isinstance(data, dict):
            blocks.append(
                "\nCircuit breaker:\n"
                f"  paused: {data.get('paused')}\n"
                f"  cb_active: {data.get('cb_active')}\n"
                f"  auto_resume_armed: {data.get('cb_auto_resume_armed')}\n"
                f"  updated_at: {data.get('updated_at')}"
            )
        elif name == "wf_last_snapshot.json" and isinstance(data, dict):
            blocks.append(
                "\nWF snapshot:\n"
                f"  trade_profile: {data.get('trade_profile')}\n"
                f"  expectancy: {data.get('expectancy')}\n"
                f"  n_trades: {data.get('n_trades')}\n"
                f"  max_total_exposure_usd: {data.get('max_total_exposure_usd')}\n"
                f"  circuit_breaker_enabled: {data.get('circuit_breaker_enabled')}"
            )
        else:
            snippet = json.dumps(data, ensure_ascii=False, default=str)
            if len(snippet) > 800:
                snippet = snippet[:797] + "…"
            blocks.append(f"\n[{name}]\n{snippet}")
    if not found:
        return (
            "No safe status JSON found under cruzbot data/.\n"
            "Paste /status from the trading bot into this chat, or run the paper bot so "
            "bot_heartbeat.json / paper_book_2.json appear."
        )
    blocks.append(
        "\n(Read-only — New Guy never touches cruzbot .env or trading secrets.)"
    )
    return "\n".join(blocks)


def safe_project_slug(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name).strip()).strip("._")
    if not slug:
        return None
    return slug[:60]


def chat_exports_folder(chat_id: str) -> Path:
    """data/exports/newguy/<chat>/[project]/ sticky memory stays global per chat."""
    chat = re.sub(r"[^0-9A-Za-z_-]", "", str(chat_id)) or "chat"
    folder = exports_dir() / chat
    prefs = get_chat_prefs(chat_id)
    proj = safe_project_slug(prefs.get("project_name"))
    if proj:
        folder = folder / proj
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def http_web_search(query: str, *, max_chars: int = 3500) -> str:
    """Pragmatic read-only HTTP search fallback (DuckDuckGo Instant Answer).

    SAFE: HTTP GET only — no shell, no file writes, no .env reads.
    """
    q = (query or "").strip()
    if not q:
        return "Empty search query."
    if len(q) > 300:
        q = q[:300]
    url = (
        "https://api.duckduckgo.com/?q="
        + quote_plus(q)
        + "&format=json&no_html=1&skip_disambig=1"
    )
    try:
        req = Request(
            url,
            headers={"User-Agent": "TheNewGuyCruz/1.0 (read-only research)"},
            method="GET",
        )
        with urlopen(req, timeout=12) as resp:  # noqa: S310 — public HTTP GET
            raw = resp.read(120_000)
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        return (
            f"Search unavailable ({type(exc).__name__}). "
            "Reply with best-effort knowledge and note that live search failed."
        )
    parts: List[str] = []
    abstract = (data.get("AbstractText") or "").strip()
    heading = (data.get("Heading") or "").strip()
    abs_url = (data.get("AbstractURL") or "").strip()
    if heading:
        parts.append(f"Topic: {heading}")
    if abstract:
        parts.append(abstract)
    if abs_url:
        parts.append(f"Source: {abs_url}")
    related = data.get("RelatedTopics") or []
    if isinstance(related, list):
        for item in related[:6]:
            if not isinstance(item, dict):
                continue
            text = (item.get("Text") or "").strip()
            if text:
                parts.append(f"• {text[:240]}")
    if not parts:
        return (
            f"No Instant Answer results for {q!r}. "
            "Answer from knowledge and say live results were empty."
        )
    out = "\n".join(parts)
    if len(out) > max_chars:
        out = out[: max_chars - 1].rstrip() + "…"
    return out


def _extract_responses_text(resp: Any) -> str:
    """Best-effort text extraction from xAI/OpenAI Responses API object."""
    try:
        ot = getattr(resp, "output_text", None)
        if ot:
            return str(ot).strip()
    except Exception:
        pass
    chunks: List[str] = []
    try:
        output = getattr(resp, "output", None) or []
        for item in output:
            content = getattr(item, "content", None)
            if content is None and isinstance(item, dict):
                content = item.get("content")
            if not content:
                continue
            for part in content:
                text = getattr(part, "text", None)
                if text is None and isinstance(part, dict):
                    text = part.get("text")
                if text:
                    chunks.append(str(text))
    except Exception:
        pass
    return "\n".join(chunks).strip()


def _ask_with_web_tools(
    cli: OpenAI,
    *,
    model: str,
    messages: List[Dict[str, Any]],
    temperature: float,
    chat_id: str,
) -> Tuple[str, Any]:
    """Deep-turn tool path: prefer xAI Responses web_search; else function+HTTP.

    Returns (reply_text, usage_or_None).
    """
    # 1) Prefer Responses API built-in web_search (server-side on xAI)
    try:
        input_msgs = []
        for m in messages:
            role = m.get("role") or "user"
            content = m.get("content")
            if isinstance(content, list):
                # flatten multimodal to text for responses tool path
                texts = [
                    str(p.get("text") or "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                content = "\n".join(t for t in texts if t) or "(image/message)"
            input_msgs.append({"role": role, "content": content})
        kwargs: Dict[str, Any] = {
            "model": model,
            "input": input_msgs,
            "tools": [{"type": "web_search"}],
            "temperature": temperature,
        }
        # prompt_cache_key for Responses API sticky routing (documented)
        kwargs["prompt_cache_key"] = f"newguy-{chat_id}"
        resp = cli.responses.create(**kwargs)
        text = _extract_responses_text(resp)
        usage = getattr(resp, "usage", None)
        if text:
            return text, usage
    except Exception:
        pass  # fall through — unsupported shape / model / tools

    # 2) Chat Completions function-calling + local HTTP search (safe fallback)
    try:
        resp = cli.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            tools=[_WEB_SEARCH_FUNCTION_TOOL],
            tool_choice="auto",
            extra_headers={"x-grok-conv-id": f"newguy-{chat_id}"},
        )
        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            follow = list(messages)
            follow.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments or "{}",
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )
            for tc in tool_calls:
                name = getattr(tc.function, "name", "") or ""
                raw_args = getattr(tc.function, "arguments", None) or "{}"
                query = ""
                try:
                    args = json.loads(raw_args)
                    if isinstance(args, dict):
                        query = str(args.get("query") or "")
                except Exception:
                    query = raw_args[:200]
                if name == "web_search":
                    result = http_web_search(query)
                else:
                    result = f"Unsupported tool: {name}"
                follow.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    }
                )
            resp2 = cli.chat.completions.create(
                model=model,
                messages=follow,
                temperature=temperature,
                extra_headers={"x-grok-conv-id": f"newguy-{chat_id}"},
            )
            reply = (resp2.choices[0].message.content or "").strip()
            return reply, getattr(resp2, "usage", None)
        reply = (msg.content or "").strip()
        if reply:
            return reply, getattr(resp, "usage", None)
    except Exception:
        pass

    # 3) Last resort: inject a one-shot HTTP search into a normal completion
    user_tail = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            user_tail = c if isinstance(c, str) else _history_text_for_store(c)  # type: ignore[arg-type]
            break
    if wants_web_search(user_tail):
        snippet = http_web_search(user_tail[:200])
        augmented = list(messages) + [
            {
                "role": "user",
                "content": (
                    "Live search results (fallback; may be incomplete):\n"
                    f"{snippet}\n\nUse these if helpful; cite uncertainty."
                ),
            }
        ]
        resp = _call_completion(
            cli,
            model=model,
            messages=augmented,
            temperature=temperature,
            stream=False,
            chat_id=chat_id,
        )
        reply = (resp.choices[0].message.content or "").strip()
        return reply, getattr(resp, "usage", None)

    resp = _call_completion(
        cli,
        model=model,
        messages=messages,
        temperature=temperature,
        stream=False,
        chat_id=chat_id,
    )
    reply = (resp.choices[0].message.content or "").strip()
    return reply, getattr(resp, "usage", None)


def _call_completion(
    cli: OpenAI,
    *,
    model: str,
    messages: List[Dict[str, Any]],
    temperature: float,
    stream: bool,
    chat_id: Optional[str] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
):
    """Chat Completions wrapper with prompt-cache hint (x-grok-conv-id).

    xAI auto-caches matching message prefixes; sticky routing via
    x-grok-conv-id maximizes hits (docs.x.ai prompt-caching). Unsupported
    kwargs/headers are swallowed and retried clean.
    """
    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": stream,
    }
    if stream:
        kwargs["stream_options"] = {"include_usage": True}
    if tools:
        kwargs["tools"] = tools
    # Prompt cache sticky routing (Chat Completions)
    headers = {"x-grok-conv-id": f"newguy-{chat_id or 'default'}"}
    try:
        return cli.chat.completions.create(**kwargs, extra_headers=headers)
    except TypeError:
        # Older SDKs may not accept extra_headers
        try:
            return cli.chat.completions.create(**kwargs)
        except Exception:
            raise
    except Exception as exc:
        # If API rejects unknown header/body, retry bare
        msg = str(exc).lower()
        if "header" in msg or "extra" in msg or "cache" in msg or "unexpected" in msg:
            try:
                return cli.chat.completions.create(**kwargs)
            except Exception:
                raise exc
        raise


def ask_grok(
    user_message: str = "",
    *,
    chat_id: str = "cli",
    history: Optional[List[Dict[str, Any]]] = None,
    system: Optional[str] = None,
    client: Optional[OpenAI] = None,
    settings: Optional[Dict[str, Any]] = None,
    images_b64: Optional[Sequence[Tuple[str, str]]] = None,
    extra_text: Optional[str] = None,
    content_parts: Optional[List[ContentPart]] = None,
) -> str:
    s = settings or get_settings()
    cli = client or make_client(s)
    key = str(chat_id)
    ensure_hands_off_defaults(key)

    user_content = _build_user_content(
        user_message,
        images_b64=images_b64,
        extra_text=extra_text,
        content_parts=content_parts,
    )
    has_images = bool(images_b64) or (
        isinstance(user_content, list)
        and any(isinstance(p, dict) and p.get("type") == "image_url" for p in user_content)
    )
    has_code_file = bool(extra_text) and (
        "```" in (extra_text or "") or "file:" in (extra_text or "").lower()
        or len(extra_text or "") > 800
    )
    turn = resolve_turn_plan(
        key,
        user_message or (extra_text or ""),
        has_images=has_images,
        has_code_file=has_code_file,
    )

    turn_history = list(history) if history is not None else list(_histories[key])
    sys_prompt = _resolve_system(key, system, turn)
    model, temperature, max_hist, vision_try = _resolve_runtime(
        key, s, has_images=has_images, turn=turn
    )
    # Use recent window for the API call only — never shrink stored memory because /fast is on.
    # When a rolling summary exists, inject it and optionally shrink further for FAST.
    use_fast = bool(turn.get("fast")) if turn else False
    turn_history = _api_history_with_summary(
        key, turn_history, max_hist, use_fast=use_fast
    )

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": sys_prompt},
        *turn_history,
        {"role": "user", "content": user_content},
    ]

    reply = ""
    vision_note = ""
    last_err: Optional[Exception] = None

    try:
        if has_images and vision_try:
            for vmodel in vision_try:
                try:
                    resp = _call_completion(
                        cli, model=vmodel, messages=messages,
                        temperature=temperature, stream=False,
                        chat_id=key,
                    )
                    reply = (resp.choices[0].message.content or "").strip()
                    record_usage(key, getattr(resp, "usage", None))
                    _last_vision_error.pop(key, None)
                    last_err = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    note_quota_error(exc)
                    _last_vision_error[key] = f"{vmodel}: {exc}"
                    continue
            if not reply and last_err is not None:
                text_only = _history_text_for_store(user_content)
                messages[-1] = {"role": "user", "content": text_only}
                vision_note = (
                    f"\n\n[Vision unavailable — tried {', '.join(vision_try)}. "
                    f"Last error: {last_err}. Answering from text only.]"
                )
                resp = _call_completion(
                    cli, model=model, messages=messages,
                    temperature=temperature, stream=False,
                    chat_id=key,
                )
                reply = (resp.choices[0].message.content or "").strip()
                record_usage(key, getattr(resp, "usage", None))
        else:
            use_tools = (
                tools_enabled()
                and bool(turn.get("deep"))
                and wants_web_search(user_message or (extra_text or ""))
                and not has_images
            )
            if use_tools:
                reply, usage = _ask_with_web_tools(
                    cli,
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    chat_id=key,
                )
                record_usage(key, usage)
            else:
                resp = _call_completion(
                    cli, model=model, messages=messages,
                    temperature=temperature, stream=False,
                    chat_id=key,
                )
                reply = (resp.choices[0].message.content or "").strip()
                record_usage(key, getattr(resp, "usage", None))
    except Exception as exc:
        note_quota_error(exc)
        raise

    if not reply:
        reply = "(empty reply from model)"
    if vision_note:
        reply = reply + vision_note

    if history is None:
        store_user = _history_text_for_store(user_content)
        _histories[key].append({"role": "user", "content": store_user})
        _histories[key].append({"role": "assistant", "content": reply})
        _trim_history(key, int(s.get("max_history") or 40))
        _save_histories()
        _maybe_refresh_summary_async(key, client=cli, settings=s)
    return reply


def ask_grok_stream(
    user_message: str = "",
    *,
    chat_id: str = "cli",
    history: Optional[List[Dict[str, Any]]] = None,
    system: Optional[str] = None,
    client: Optional[OpenAI] = None,
    settings: Optional[Dict[str, Any]] = None,
    images_b64: Optional[Sequence[Tuple[str, str]]] = None,
    extra_text: Optional[str] = None,
    content_parts: Optional[List[ContentPart]] = None,
) -> Generator[str, None, str]:
    s = settings or get_settings()
    cli = client or make_client(s)
    key = str(chat_id)
    ensure_hands_off_defaults(key)

    user_content = _build_user_content(
        user_message,
        images_b64=images_b64,
        extra_text=extra_text,
        content_parts=content_parts,
    )
    has_images = bool(images_b64) or (
        isinstance(user_content, list)
        and any(isinstance(p, dict) and p.get("type") == "image_url" for p in user_content)
    )
    has_code_file = bool(extra_text) and (
        "```" in (extra_text or "") or "file:" in (extra_text or "").lower()
        or len(extra_text or "") > 800
    )
    turn = resolve_turn_plan(
        key,
        user_message or (extra_text or ""),
        has_images=has_images,
        has_code_file=has_code_file,
    )

    turn_history = list(history) if history is not None else list(_histories[key])
    sys_prompt = _resolve_system(key, system, turn)
    model, temperature, max_hist, vision_try = _resolve_runtime(
        key, s, has_images=has_images, turn=turn
    )
    # Use recent window for the API call only — never shrink stored memory because /fast is on.
    # When a rolling summary exists, inject it and optionally shrink further for FAST.
    use_fast = bool(turn.get("fast")) if turn else False
    turn_history = _api_history_with_summary(
        key, turn_history, max_hist, use_fast=use_fast
    )

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": sys_prompt},
        *turn_history,
        {"role": "user", "content": user_content},
    ]

    stream_model = vision_try[0] if (has_images and vision_try) else model
    vision_note = ""
    stream = None
    first_err: Optional[Exception] = None

    # Deep + search: tool path (non-stream), yield once
    if (
        tools_enabled()
        and bool(turn.get("deep"))
        and wants_web_search(user_message or (extra_text or ""))
        and not has_images
    ):
        reply, usage = _ask_with_web_tools(
            cli,
            model=model,
            messages=messages,
            temperature=temperature,
            chat_id=key,
        )
        if usage is not None:
            record_usage(key, usage)
        if not reply:
            reply = "(empty reply from model)"
        yield reply
        if history is None:
            store_user = _history_text_for_store(user_content)
            _histories[key].append({"role": "user", "content": store_user})
            _histories[key].append({"role": "assistant", "content": reply})
            _trim_history(key, int(s.get("max_history") or 40))
            _save_histories()
            _maybe_refresh_summary_async(key, client=cli, settings=s)
        return reply

    try:
        stream = _call_completion(
            cli, model=stream_model, messages=messages,
            temperature=temperature, stream=True,
            chat_id=key,
        )
    except Exception as err:
        first_err = err
        note_quota_error(err)
        if has_images and len(vision_try) > 1:
            for vmodel in vision_try[1:]:
                try:
                    stream = _call_completion(
                        cli, model=vmodel, messages=messages,
                        temperature=temperature, stream=True,
                        chat_id=key,
                    )
                    stream_model = vmodel
                    first_err = None
                    break
                except Exception as exc:  # noqa: BLE001
                    first_err = exc
                    note_quota_error(exc)
                    _last_vision_error[key] = f"{vmodel}: {exc}"
            if first_err is not None:
                text_only = _history_text_for_store(user_content)
                messages[-1] = {"role": "user", "content": text_only}
                vision_note = (
                    f"\n\n[Vision unavailable — {first_err}. Answering from text only.]"
                )
                try:
                    stream = _call_completion(
                        cli, model=model, messages=messages,
                        temperature=temperature, stream=True,
                        chat_id=key,
                    )
                except Exception as exc2:
                    note_quota_error(exc2)
                    raise
        else:
            raise

    full_parts: List[str] = []
    last_usage = None
    try:
        for chunk in stream:
            try:
                if getattr(chunk, "usage", None):
                    last_usage = chunk.usage
            except Exception:
                pass
            try:
                delta = chunk.choices[0].delta.content if chunk.choices else None
            except (IndexError, AttributeError):
                delta = None
            if delta:
                full_parts.append(delta)
                yield delta
    except Exception as exc:
        note_quota_error(exc)
        raise

    if last_usage is not None:
        record_usage(key, last_usage)

    reply = "".join(full_parts).strip()
    if not reply:
        reply = "(empty reply from model)"
    if vision_note:
        yield vision_note
        reply = reply + vision_note

    if history is None:
        store_user = _history_text_for_store(user_content)
        _histories[key].append({"role": "user", "content": store_user})
        _histories[key].append({"role": "assistant", "content": reply})
        _trim_history(key, int(s.get("max_history") or 40))
        _save_histories()
        _maybe_refresh_summary_async(key, client=cli, settings=s)
    return reply


def get_last_vision_error(chat_id: str) -> Optional[str]:
    return _last_vision_error.get(str(chat_id))


def uptime_seconds() -> float:
    return time.time() - BOT_STARTED_AT


def preview_text(full: str, limit: int = PREVIEW_CHARS) -> str:
    text = full.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("\n", " "):
        idx = cut.rfind(sep)
        if idx >= limit // 2:
            cut = cut[:idx]
            break
    return cut.rstrip() + "…"


def needs_collapse(full: str, limit: int = PREVIEW_CHARS) -> bool:
    text = full.strip()
    if len(text) > limit:
        return True
    if text.count("\n") >= 5 and len(text) > 80:
        return True
    return False




def exports_dir() -> Path:
    """New Guy downloads only — never shared with trading/cruzbot paths."""
    d = _DATA_DIR / "exports" / "newguy"
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_export_name(name: str, default: str = "export.txt") -> str:
    base = (name or default).strip() or default
    base = base.replace("\\", "/").split("/")[-1]
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or default
    if len(base) > 80:
        stem, dot, ext = base.rpartition(".")
        if dot:
            base = (stem[:60] or "export") + "." + ext[:15]
        else:
            base = base[:80]
    return base


def export_text_file(chat_id: str, content: str, filename: str) -> Path:
    """Write a text/code file under data/exports/newguy/<chat>/[project]/."""
    import time as _time
    folder = chat_exports_folder(chat_id)
    fname = safe_export_name(filename)
    # avoid overwrite
    path = folder / fname
    if path.exists():
        stem = path.stem
        suf = path.suffix
        path = folder / f"{stem}_{int(_time.time())}{suf}"
    path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
    return path


def export_zip(chat_id: str, files: dict, zip_name: str = "export.zip") -> Path:
    """files: {filename: text_content} -> zip path under project folder if set."""
    import io, zipfile, time as _time
    folder = chat_exports_folder(chat_id)
    zname = safe_export_name(zip_name, "export.zip")
    if not zname.lower().endswith(".zip"):
        zname += ".zip"
    path = folder / zname
    if path.exists():
        path = folder / f"{Path(zname).stem}_{int(_time.time())}.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            fn = safe_export_name(str(name))
            data = content if isinstance(content, (bytes, bytearray)) else str(content).encode("utf-8")
            zf.writestr(fn, data)
    return path


def extract_code_fences(text: str) -> list:
    """Return list of (ext_or_lang, code) from markdown fences."""
    out = []
    pattern = re.compile(r'```([\w+-]*)\n(.*?)```', re.S)
    for m in pattern.finditer(text or ""):
        lang = (m.group(1) or "").strip().lower()
        code = m.group(2)
        if code.strip():
            out.append((lang, code))
    return out

def lang_to_ext(lang: str) -> str:
    m = {
        "python": ".py", "py": ".py", "javascript": ".js", "js": ".js",
        "typescript": ".ts", "ts": ".ts", "tsx": ".tsx", "jsx": ".jsx",
        "json": ".json", "bash": ".sh", "sh": ".sh", "shell": ".sh",
        "html": ".html", "css": ".css", "sql": ".sql", "yaml": ".yml",
        "yml": ".yml", "toml": ".toml", "md": ".md", "markdown": ".md",
        "rust": ".rs", "go": ".go", "c": ".c", "cpp": ".cpp", "java": ".java",
    }
    return m.get((lang or "").lower(), ".txt")



def pack_newguy_suitcase(chat_id: str, zip_name: str | None = None) -> Path:
    """Sanitized zip of this New Guy repo for Telegram download (no secrets)."""
    import time as _time
    import zipfile

    root = Path(__file__).resolve().parent
    if root.name != "The-New-Guy-Cruz":
        raise RuntimeError(f"Refusing suitcase pack outside New Guy tree: {root}")
    # Hard wall: never include trading bot trees even if symlinked nearby
    forbidden_markers = ("cruzbot_instance_2", "cruzbot", "ATTACK-SUITCASE", "rebuild-kits")
    chat = re.sub(r"[^0-9A-Za-z_-]", "", str(chat_id)) or "chat"
    folder = exports_dir() / chat
    folder.mkdir(parents=True, exist_ok=True)
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    zname = safe_export_name(zip_name or f"newguy-suitcase-{stamp}.zip", f"newguy-suitcase-{stamp}.zip")
    if not zname.lower().endswith(".zip"):
        zname += ".zip"
    out = folder / zname
    if out.exists():
        out = folder / f"{Path(zname).stem}_{int(_time.time())}.zip"

    skip_dir_names = {
        ".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", "logs",
    }
    skip_file_names = {".env", ".env.local", ".env.production"}
    skip_suffixes = {".pyc", ".pyo", ".log"}
    # never pack prior exports or live chat memory with secrets
    skip_rel_prefixes = ("data/",)

    written = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if any(part in skip_dir_names for part in path.parts):
                continue
            if path.name in skip_file_names:
                continue
            if path.suffix.lower() in skip_suffixes:
                continue
            if rel.startswith(skip_rel_prefixes):
                continue
            if path.stat().st_size > 5 * 1024 * 1024:
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(root)
            except ValueError:
                continue
            if any(m in resolved.as_posix() for m in forbidden_markers):
                continue
            zf.write(path, arcname=rel)
            written += 1
        zf.writestr(
            "SUITCASE_README.txt",
            (
                "The New Guy Cruz ONLY — sanitized suitcase (trading bot NOT included)\n"
                f"Files packed: {written}\n"
                "Excluded: .env, .git, .venv, logs, exports, secrets.\n"
                "Cold start: copy .env.example → .env, fill keys, pip install -r requirements.txt,\n"
                "then: env -u TELEGRAM_BOT_TOKEN .venv/bin/python -u telegram_bot.py\n"
            ),
        )
    return out



def list_exports(chat_id: str, limit: int = 20) -> list:
    folder = chat_exports_folder(chat_id)
    if not folder.is_dir():
        return []
    files = sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    return [p for p in files if p.is_file()][:limit]


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
        except Exception as exc:  # noqa: BLE001
            print(f"Error: {exc}\n", file=sys.stderr)
            continue
        print(f"Cruz> {reply}\n")


_load_histories()

if __name__ == "__main__":
    raise SystemExit(cli_main())
