# Telegram setup — The New Guy Cruz

Cold start on a fresh box (no secrets in this file).

## 1. Prerequisites

- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- An xAI API key from [console.x.ai](https://console.x.ai/)

## 2. Install

```bash
cd The-New-Guy-Cruz
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` (never commit it):

| Key | Purpose |
|-----|---------|
| `XAI_API_KEY` | Required — Grok API |
| `XAI_MODEL` | Default text model (e.g. `grok-4.7`) |
| `XAI_VISION_MODEL` | Image model |
| `XAI_FAST_MODEL` | Cheap/fast model for `/plan fast` / Auto→fast |
| `XAI_DEEP_MODEL` | Deep model for `/plan deep` / Auto→deep |
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `TELEGRAM_CHAT_ID` | Optional allowlist (comma-separated) |
| `NEWGUY_TOOLS` | `1` (default) enables web_search on Deep turns |
| `XAI_MANAGEMENT_KEY` / `XAI_TEAM_ID` | Optional prepaid `/balance` |

## 3. Run (do not restart from Telegram — ops does that)

```bash
env -u TELEGRAM_BOT_TOKEN .venv/bin/python -u telegram_bot.py
```

Unsetting a stale parent `TELEGRAM_BOT_TOKEN` lets `.env` win.

## 4. First chat

1. Open Telegram → your bot → **Start**
2. `/cmds` for the full list
3. Rate plans: **Auto** (default) · **Fast** · **Deep**
4. Sticky memory: `/remember` · `/memory` · `/forget`
5. Desktop handoff: `/ops <ask>` or say “do it on the computer”
6. Downloads: ask for code → ⬇️ Download · `/files` · `/project <name>`

## 5. Safety

- Never put cruzbot / trading `.env` values into this bot
- Exports live under `data/exports/newguy/<chat>/[project]/`
- Suitcase `/attack` packs **New Guy only** (no trading tree, no `.env`)
