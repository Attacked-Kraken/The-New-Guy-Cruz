# Telegram commands — The New Guy Cruz

## Chat & memory

| Command | What it does |
|---------|----------------|
| `/start` | Greet + reset conversation history (sticky memory kept) |
| `/reset` | Clear history + rolling summary |
| `/memory` | Show sticky facts + chat summary |
| `/remember <fact>` | Save sticky fact (survives `/reset`) |
| `/forget [last]` | Clear sticky memory (or last fact) |

## Rate plans

| Command | What it does |
|---------|----------------|
| `/plan auto` | Per-message Fast/Deep (default, lean burn) |
| `/plan fast` / `/fast` | Cheap / tight replies |
| `/plan deep` / `/deep` | Maximum intelligence |
| `/auto` | Shortcut for Auto |
| `/compact` | Soft ~600-char bias |

Deep / Auto→deep turns can call **web_search** when you ask for current info (`NEWGUY_TOOLS=1`).

## Ops & desktop

| Command | What it does |
|---------|----------------|
| `/ops <ask>` | Queue work for Attacked Kraken (desktop) |
| `/ops` | List open ops for this chat |

Saying “do it on the computer” / desktop / box / repo / restart / deploy auto-queues ops and appends:  
`Queued for Attacked Kraken — say check ops there.`

## Trading status (read-only)

| Command | What it does |
|---------|----------------|
| `/paper` | Read scrubbed status JSON from cruzbot paper instance (never `.env`) |
| `/tstatus` | Short Codespace-style trading status (read-only; backup UX) |

If no status files exist, paste `/status` from the trading bot into this chat.

## Exports & projects

| Command | What it does |
|---------|----------------|
| `/project <name>` | Scope exports to `data/exports/newguy/<chat>/<project>/` |
| `/project clear` | Back to chat-level exports |
| `/files` | List downloadable exports |
| `/download` | Send last code package |
| `/attack` | Sanitized New Guy suitcase zip |

Sticky memory stays **global per chat**; only export paths are project-scoped.

## Credits & health

| Command | What it does |
|---------|----------------|
| `/balance` | Prepaid credits + usage |
| `/reload` | Re-read `.env` + refresh command menu |
| `/status` / `/prefs` | Health / toggles |
| `/model [name]` | Per-chat model override |
| `/upgrade` | Ranked upgrade ideas |
| `/keyboard on\|off` | Reply keyboard |
| `/help` / `/cmds` | Help / full list |
