# The New Guy Cruz

Intelligent 2-way chat with **Grok / xAI** — Telegram is the recommended free UX.  
CLI works offline from your terminal; SMS (Twilio) is an optional paid secondary path.

## Features

- Multi-turn conversation memory (bounded per chat)
- Persona: *The New Guy Cruz* — sharp, helpful, not corporate
- Telegram: `/start` `/reset` `/help` + normal messages → Grok
- Long Telegram replies collapse (~800 chars) with inline **▼ Show more** / **▲ Hide**
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
TELEGRAM_BOT_TOKEN=123456:ABC...
# Optional: lock the bot to your Telegram user/chat id(s)
TELEGRAM_CHAT_ID=
TEMPERATURE=0.7
MAX_HISTORY=20
```

> **Never commit `.env`.** It is listed in `.gitignore`.

### 4. Run

```bash
python telegram_bot.py
```

Open Telegram, find your bot, tap **Start**, and chat.  
Long answers arrive collapsed — tap **▼ Show more** to expand, **▲ Hide** to collapse again.

Optional allowlist: set `TELEGRAM_CHAT_ID` to your numeric chat id (or comma-separated ids).  
Message the bot once, then check logs / use a bot like `@userinfobot` to learn your id.

### Codespaces / remote

Same steps: copy `.env.example` → `.env`, fill secrets as Codespace secrets or a local `.env`, then:

```bash
pip install -r requirements.txt
python telegram_bot.py
```

Keep the process running while you chat (terminal / `tmux` / Codespace port-free long-poll).

## CLI (no Telegram)

```bash
python bot.py
```

Commands: `/reset`, `/quit`

## SMS (optional, paid)

Twilio is **not** required. If you want SMS:

```bash
pip install twilio
# set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, SMS_TO_NUMBER in .env
python sms_bot.py
```

If Twilio env vars are missing, `sms_bot.py` exits with a clear error and points you back to Telegram.

## Project layout

| File | Role |
|------|------|
| `bot.py` | Shared `ask_grok`, memory, CLI |
| `telegram_bot.py` | 2-way Telegram + collapse UI |
| `sms_bot.py` | Optional Twilio SMS stub |
| `.env.example` | Env template (no secrets) |
| `requirements.txt` | Dependencies |

## Models

Default `XAI_MODEL=grok-3`. You can also try `grok-2-latest` or other models listed in the [xAI docs](https://docs.x.ai/).  
API base URL defaults to `https://api.x.ai/v1` (override with `XAI_BASE_URL` if needed).

## License

Personal / project use — keep your API keys private.
