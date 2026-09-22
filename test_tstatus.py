"""Unit test for /tstatus formatter (no Telegram network)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from bot import format_tstatus


def test_format_tstatus_reads_live_data():
    text = format_tstatus()
    assert "Trading /tstatus" in text or "No cruzbot" in text
    if "No cruzbot" in text:
        return
    assert "mode=" in text
    assert "cash=" in text
    assert "CB paused=" in text
    assert "TG=" in text
    # No secrets
    assert "TOKEN" not in text.upper() or "tg_listener" in text.lower()
    assert "api_key" not in text.lower()


def test_format_tstatus_with_fixture(tmp_path, monkeypatch):
    data = tmp_path
    (data / "bot_heartbeat.json").write_text(
        json.dumps(
            {
                "ts": "2026-09-22T19:00:00+00:00",
                "unix": time.time() - 5,
                "mode": "PAPER",
                "pid": 12345,
                "open_positions": 2,
                "tg_listener": "ok",
            }
        )
    )
    (data / "paper_book_2.json").write_text(
        json.dumps(
            {
                "cash": 1000.5,
                "positions": {"BTC-USD": {}},
                "consecutive_losses": 0,
                "closed_trades": [
                    {"symbol": "ETH-USD", "pnl": 12.5, "won": True},
                ],
            }
        )
    )
    monkeypatch.setattr("bot.CRUZBOT_INSTANCE_DATA", data)
    text = format_tstatus()
    assert "mode=PAPER" in text
    assert "pid=12345" in text
    assert "open=2" in text or "open=1" in text
    assert "$1,000.50" in text or "1000.5" in text
    assert "CB paused=" in text
    assert "ETH-USD" in text
