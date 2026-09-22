# The New Guy Cruz

Intelligent 2-way chat with **Grok / xAI** — Telegram is the recommended free UX.  
CLI works offline from your terminal; SMS (Twilio) is an optional paid secondary path.

## Features

- Multi-turn conversation memory (bounded per chat)
- Persona: *The New Guy Cruz* — sharp, helpful, not corporate
- **Photos** and **code/files** — vision for images; text extraction for `.py`, `.js`, `.md`, etc.
- **Streaming** replies (placeholder message edited as tokens arrive)
- Chat toggles: `/fast`, `/compact`, `/model`, `/prefs`
- Command menu via BotFather-style `set_my_commands`
- Long Telegram replies collapse (~280 chars preview) with inline **▼ Show more** / **▲ Hide**
- `/upgrade` — ask Grok for concrete upgrade ideas (reads local README; **never** auto-commits or pushes)
- Secrets only via environment / `.env` (never commit real keys)

## Quick start (Telegram)

### 1. Create a bot token (free)

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`, pick a name and username
3. Copy the HTTP API token BotFather gives you

### 2. Get an xAI API key

1. Sign up / log in at [console.x.ai](https://console.x.ai/)
2. Create an API key

### 3. Configure env

```bash
cd The-New-Guy-Cruz
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```env
XAI_API_KEY=xai-...
XAI_MODEL=grok-3
XAI_VISION_MODEL=grok-4.6
TELEGRAM_BOT_TOKEN=123456:ABC...
# Optional: lock the bot to your Telegram user/chat id(s)
TELEGRAM_CHAT_ID=
TEMPERATURE=0.7
MAX_HISTORY=20
```

> **Never commit `.env`.** It is listed in `.gitignore`.

### 4. Run

```bash
# Prefer unsetting a stale parent TELEGRAM_BOT_TOKEN so .env wins:
env -u TELEGRAM_BOT_TOKEN python telegram_bot.py
```

Open Telegram, find your bot, tap **Start**, and chat.  
Send photos or `.py` / `.md` documents. Long answers arrive collapsed — tap **▼ Show more**.

Optional allowlist: set `TELEGRAM_CHAT_ID` to your numeric chat id (or comma-separated ids).

### Codespaces / remote

Same steps: copy `.env.example` → `.env`, fill secrets, then:

```bash
pip install -r requirements.txt
env -u TELEGRAM_BOT_TOKEN python telegram_bot.py
```

## Commands

| Command | What it does |
|---------|----------------|
| `/start` | Greet + reset memory |
| `/help` | Help (photos, files, toggles) |
| `/reset` | Clear conversation memory |
| `/fast` | Toggle fast mode (tighter system, lower temp, shorter history; may use `XAI_FAST_MODEL`) |
| `/compact` | Toggle compact replies (~600 chars) |
| `/model [name]` | Show or set model for this chat (`clear` resets to env default) |
| `/prefs` | Show toggles |
| `/status` | Model, vision model, fast/compact, history length, uptime |
| `/upgrade [notes]` | Ranked upgrade ideas (optional notes + README context) |
| `/cmds` | List commands |
| `/keyboard on\|off` | Show/hide reply keyboard (Fast \| Compact \| Prefs \| Upgrade \| Reset) |

## Models

- Default text: `XAI_MODEL` (typically `grok-3`) — kept for cost unless you `/model` or use `/fast`.
- Vision (when images are present): `XAI_VISION_MODEL` default `grok-4.6`, then `grok-2-vision-1212`, then text-only with a clear note.
- Fast (optional): `XAI_FAST_MODEL` default `grok-4.20` when `/fast` is on and there are no images.

API base URL defaults to `https://api.x.ai/v1` (override with `XAI_BASE_URL`).

## What this bot does **not** do

- **No auto git commit or push from Telegram.** `/upgrade` and chat can *suggest* changes; use Grok Bot / Attacked Kraken Reboot (or your own checkout) for real repo edits.
- Does not touch other bots (e.g. trading `cruzbot`).

## CLI (no Telegram)

```bash
python bot.py
```

Commands: `/reset`, `/quit`

## SMS (optional, paid)

Twilio is **not** required. If you want SMS:

```bash
pip install twilio
# set TWILIO_* and SMS_TO_NUMBER in .env
python sms_bot.py
```

## Project layout

| File | Role |
|------|------|
| `bot.py` | Shared `ask_grok` / `ask_grok_stream`, prefs, vision, CLI |
| `telegram_bot.py` | Telegram: text/photo/docs, streaming, commands |
| `sms_bot.py` | Optional Twilio SMS stub |
| `.env.example` | Env template (no secrets) |
| `requirements.txt` | Dependencies |

## License

Personal / project use — keep your API keys private.
