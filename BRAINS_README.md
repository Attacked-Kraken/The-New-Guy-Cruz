# Brains — The New Guy Cruz

What this bot is, how memory works, and how it talks to the desktop assistant.

## Role

**The New Guy Cruz** is the Telegram front-end to Grok / xAI. It does **not**
trade, restart cruzbot, or hold trading secrets. Desktop / box / repo work is
handed to **Attacked Kraken** via `/ops` (or silent ops when you ask).

## Rate plans (brains)

- **Auto** (default): classifies each message → Fast or Deep
- **Fast**: cheaper model, short history window, tight replies
- **Deep**: fuller reasoning; optional **web_search** tools when you ask for
  current / search info (`NEWGUY_TOOLS=1`)

## Memory layers

1. **Sticky personal memory** (`/remember`) — survives `/reset`, injected into
   the system prefix for prompt-cache friendliness
2. **Rolling chat summary** — compresses older turns; cleared by `/reset`
3. **Recent history** — bounded message window sent to the API

## Prompt cache

System messages put **sticky + persona first**, then rate-plan addons. Chat
Completions calls send `x-grok-conv-id: newguy-<chat>` for sticky cache routing
(unsupported headers are swallowed and retried clean).

## Downloads

Generated files land in:

```text
data/exports/newguy/<chat_id>/
data/exports/newguy/<chat_id>/<project>/   # after /project <name>
```

## Ops inbox

`/ops` and silent handoff append scrubbed lines to `data/ops_inbox.jsonl`.
On desktop, tell Attacked Kraken: **check ops**.

## Paper status

`/paper` reads only public-ish JSON under `/workspace/cruzbot_instance_2/data`
(heartbeat, paper book, circuit breaker, WF snapshot). Tokens are scrubbed.
Never reads cruzbot `.env`.

## Cold rebuild checklist

1. Copy `.env.example` → `.env` and fill keys (no secrets in git)
2. `pip install -r requirements.txt`
3. `env -u TELEGRAM_BOT_TOKEN .venv/bin/python -u telegram_bot.py`
4. `/cmds` in Telegram · `/remember` lean prefs if needed
5. Desktop work → `/ops` or “do it on the computer”
