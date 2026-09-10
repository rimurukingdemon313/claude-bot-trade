"""SQLite ledger for the paper-only M15 trading simulation.

The API calls this module as a small, process-safe CLI instead of keeping
account state in the browser.  SQLite WAL mode makes the ledger safe for the
scheduled worker and a manual scan arriving close together.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "trading_bot.db"
STARTING_BALANCE = 1000.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS bot_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT OR IGNORE INTO bot_config (key, value) VALUES ('scheduler_enabled', '1');

        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candle_timestamp TEXT NOT NULL UNIQUE,
            scanned_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            data_source TEXT NOT NULL,
            live INTEGER NOT NULL,
            latest_price REAL NOT NULL,
            candle_count INTEGER NOT NULL,
            latest_candle_json TEXT NOT NULL,
            smc_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS smc_setups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
            setup_type TEXT NOT NULL,
            direction TEXT,
            details_json TEXT NOT NULL,
            detected_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ai_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
            decision TEXT NOT NULL,
            confidence INTEGER NOT NULL,
            reasoning TEXT NOT NULL,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            risk_reward_ratio REAL,
            risk_amount REAL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS risk_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
            approved INTEGER NOT NULL,
            state TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            rules_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS paper_trades (
            id TEXT PRIMARY KEY,
            scan_id INTEGER REFERENCES scans(id) ON DELETE SET NULL,
            ai_decision_id INTEGER REFERENCES ai_decisions(id) ON DELETE SET NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
            entry_price REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            risk_amount REAL NOT NULL,
            quantity REAL NOT NULL,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            exit_price REAL,
            pnl REAL,
            pnl_pct REAL,
            close_reason TEXT
        );

        CREATE TABLE IF NOT EXISTS account_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            balance REAL NOT NULL,
            daily_pnl REAL NOT NULL,
            monthly_pnl REAL NOT NULL,
            peak REAL NOT NULL,
            drawdown_pct REAL NOT NULL,
            trades INTEGER NOT NULL,
            wins INTEGER NOT NULL,
            losses INTEGER NOT NULL,
            recorded_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_scans_scanned_at ON scans(scanned_at DESC);
        CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON paper_trades(closed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_setups_scan_id ON smc_setups(scan_id);
        """
    )
    _migrate_add_ai_provider_columns(connection)


def _migrate_add_ai_provider_columns(connection: sqlite3.Connection) -> None:
    """Add ai_provider/ai_model columns to an existing ai_decisions table.

    Uses ALTER TABLE ADD COLUMN (never DROP/CREATE) so this is safe to run
    against a database that already has historical rows — nothing is
    deleted or rewritten, existing trades and decisions are untouched.
    """

    existing_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(ai_decisions)").fetchall()
    }
    if "ai_provider" not in existing_columns:
        connection.execute("ALTER TABLE ai_decisions ADD COLUMN ai_provider TEXT")
    if "ai_model" not in existing_columns:
        connection.execute("ALTER TABLE ai_decisions ADD COLUMN ai_model TEXT")


def as_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def as_number(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result


def row_json(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def account(connection: sqlite3.Connection) -> dict[str, Any]:
    closed = connection.execute(
        "SELECT status, pnl, closed_at FROM paper_trades WHERE status = 'CLOSED' ORDER BY closed_at ASC"
    ).fetchall()
    open_count = connection.execute(
        "SELECT COUNT(*) AS count FROM paper_trades WHERE status = 'OPEN'"
    ).fetchone()["count"]
    balance = STARTING_BALANCE + sum(float(row["pnl"] or 0) for row in closed)
    wins = sum(1 for row in closed if float(row["pnl"] or 0) > 0)
    losses = sum(1 for row in closed if float(row["pnl"] or 0) < 0)
    trades = len(closed) + int(open_count)
    today = datetime.now(timezone.utc).date().isoformat()
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    daily_pnl = sum(
        float(row["pnl"] or 0)
        for row in closed
        if str(row["closed_at"] or "").startswith(today)
    )
    monthly_pnl = sum(
        float(row["pnl"] or 0)
        for row in closed
        if str(row["closed_at"] or "").startswith(month)
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    roi_30d_pnl = sum(
        float(row["pnl"] or 0)
        for row in closed
        if row["closed_at"]
        and datetime.fromisoformat(str(row["closed_at"]).replace("Z", "+00:00")) >= cutoff
    )
    snapshots = connection.execute(
        "SELECT peak, balance FROM account_snapshots ORDER BY id ASC"
    ).fetchall()
    peak = max([STARTING_BALANCE, *(float(row["peak"]) for row in snapshots), balance])
    drawdown = max(0.0, ((peak - balance) / peak) * 100) if peak else 0.0
    return {
        "balance": round(balance, 8),
        "dailyPnl": round(daily_pnl, 8),
        "monthlyPnl": round(monthly_pnl, 8),
        "pnl30d": round(roi_30d_pnl, 8),
        "roi30d": round((roi_30d_pnl / STARTING_BALANCE) * 100, 8),
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "drawdown": round(drawdown, 8),
        "peak": round(peak, 8),
        "openPositions": int(open_count),
    }


def insert_snapshot(connection: sqlite3.Connection) -> None:
    values = account(connection)
    connection.execute(
        """
        INSERT INTO account_snapshots
        (balance, daily_pnl, monthly_pnl, peak, drawdown_pct, trades, wins, losses, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            values["balance"],
            values["dailyPnl"],
            values["monthlyPnl"],
            values["peak"],
            values["drawdown"],
            values["trades"],
            values["wins"],
            values["losses"],
            now_iso(),
        ),
    )


def serialize_trade(row: sqlite3.Row | None, current_price: float | None = None) -> dict[str, Any] | None:
    if row is None:
        return None
    trade = dict(row)
    trade.update(
        {
            "entryPrice": trade.pop("entry_price"),
            "stopLoss": trade.pop("stop_loss"),
            "takeProfit": trade.pop("take_profit"),
            "riskAmount": trade.pop("risk_amount"),
            "openedAt": trade.pop("opened_at"),
            "closedAt": trade.pop("closed_at"),
            "exitPrice": trade.pop("exit_price"),
            "pnl": trade.get("pnl"),
            "pnlPct": trade.pop("pnl_pct"),
            "closeReason": trade.pop("close_reason"),
        }
    )
    if current_price is not None and trade["status"] == "OPEN":
        direction = 1 if trade["side"] == "BUY" else -1
        unrealized = (current_price - trade["entryPrice"]) * direction * trade["quantity"]
        trade["currentPrice"] = current_price
        trade["unrealizedPnl"] = round(unrealized, 8)
        trade["unrealizedPnlPct"] = round((unrealized / STARTING_BALANCE) * 100, 8)
    return trade


def serialize_log(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    pnl = float(value["pnl"] or 0)
    return {
        "id": value["id"],
        "time": value["closed_at"] or value["opened_at"],
        "symbol": value["symbol"],
        "side": "LONG" if value["side"] == "BUY" else "SHORT",
        "setup": value.get("close_reason") or "Gemini + SMC approved",
        "result": (
            "OPEN"
            if value["status"] == "OPEN"
            else "WIN"
            if pnl > 0
            else "LOSS"
        ),
        "pnl": round(pnl, 8),
        "pnlPct": round(float(value["pnl_pct"] or 0), 8),
        "confidence": value.get("confidence", 0),
        "entryPrice": value["entry_price"],
        "stopLoss": value["stop_loss"],
        "takeProfit": value["take_profit"],
        "openedAt": value["opened_at"],
        "closedAt": value["closed_at"],
    }


def current_state(connection: sqlite3.Connection, limit: int = 50) -> dict[str, Any]:
    active_row = connection.execute(
        "SELECT * FROM paper_trades WHERE status = 'OPEN' ORDER BY opened_at DESC LIMIT 1"
    ).fetchone()
    latest_price: float | None = None
    if active_row is not None:
        symbol_price_row = connection.execute(
            "SELECT latest_price FROM scans WHERE symbol = ? ORDER BY id DESC LIMIT 1",
            (active_row["symbol"],),
        ).fetchone()
        latest_price = float(symbol_price_row["latest_price"]) if symbol_price_row else None
    trade_rows = connection.execute(
        """
        SELECT paper_trades.*, COALESCE(ai_decisions.confidence, 0) AS confidence
        FROM paper_trades
        LEFT JOIN ai_decisions ON ai_decisions.id = paper_trades.ai_decision_id
        ORDER BY COALESCE(paper_trades.closed_at, paper_trades.opened_at) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    scan_row = connection.execute(
        "SELECT * FROM scans ORDER BY id DESC LIMIT 1"
    ).fetchone()
    decision_row = connection.execute(
        """
        SELECT ai_decisions.*, risk_decisions.approved, risk_decisions.state AS risk_state,
               risk_decisions.reasons_json, risk_decisions.rules_json
        FROM ai_decisions
        LEFT JOIN risk_decisions ON risk_decisions.scan_id = ai_decisions.scan_id
        ORDER BY ai_decisions.id DESC
        LIMIT 1
        """
    ).fetchone()
    equity_rows = connection.execute(
        "SELECT balance FROM account_snapshots ORDER BY id ASC LIMIT 30"
    ).fetchall()
    values = account(connection)
    scheduler_enabled = connection.execute(
        "SELECT value FROM bot_config WHERE key = 'scheduler_enabled'"
    ).fetchone()
    return {
        "account": values,
        "openTrade": serialize_trade(active_row, latest_price),
        "trades": [serialize_log(row) for row in trade_rows],
        "equityCurve": (
            [STARTING_BALANCE, *[round(float(row["balance"]), 8) for row in equity_rows]]
            if equity_rows
            else [STARTING_BALANCE]
        )[-30:],
        "latestAiDecision": (
            {
                "decision": decision_row["decision"],
                "confidence": decision_row["confidence"],
                "reasoning": decision_row["reasoning"],
                "entryPrice": decision_row["entry_price"],
                "stopLoss": decision_row["stop_loss"],
                "takeProfit": decision_row["take_profit"],
                "riskRewardRatio": decision_row["risk_reward_ratio"],
                "riskAmount": decision_row["risk_amount"],
                "aiProvider": decision_row["ai_provider"],
                "aiModel": decision_row["ai_model"],
            }
            if decision_row
            else None
        ),
        "latestRiskDecision": (
            {
                "approved": bool(decision_row["approved"]),
                "state": decision_row["risk_state"],
                "reasons": json.loads(decision_row["reasons_json"]),
                "rules": json.loads(decision_row["rules_json"]),
            }
            if decision_row
            else None
        ),
        "latestScan": (
            {
                "id": scan_row["id"],
                "candleTimestamp": scan_row["candle_timestamp"],
                "scannedAt": scan_row["scanned_at"],
                "latestPrice": scan_row["latest_price"],
                "dataSource": scan_row["data_source"],
                "live": bool(scan_row["live"]),
            }
            if scan_row
            else None
        ),
        "schedulerEnabled": bool(scheduler_enabled and scheduler_enabled["value"] == "1"),
    }


def settle(connection: sqlite3.Connection, payload: Mapping[str, Any]) -> dict[str, Any]:
    # payload may be either:
    #  - legacy single-symbol shape: {"latestPrice": ..., "latestCandle": ...}
    #  - multi-symbol shape: {"quotes": {"EURUSD": {"latestPrice": ..., "latestCandle": ...}, ...}}
    quotes: dict[str, Mapping[str, Any]] = {}
    if isinstance(payload.get("quotes"), Mapping):
        quotes = dict(payload["quotes"])
    elif payload.get("latestPrice") is not None:
        quotes = {"__default__": payload}

    closed: list[dict[str, Any]] = []
    rows = connection.execute(
        "SELECT * FROM paper_trades WHERE status = 'OPEN' ORDER BY opened_at ASC"
    ).fetchall()
    for row in rows:
        quote = quotes.get(row["symbol"], quotes.get("__default__"))
        if quote is None:
            continue
        latest_price = as_number(quote.get("latestPrice"))
        latest_candle = quote.get("latestCandle")
        if latest_price is None:
            continue
        if not isinstance(latest_candle, Mapping):
            latest_candle = {}
        high = as_number(latest_candle.get("high"), latest_price) or latest_price
        low = as_number(latest_candle.get("low"), latest_price) or latest_price
        side = row["side"]
        stop = float(row["stop_loss"])
        target = float(row["take_profit"])
        exit_price: float | None = None
        reason: str | None = None
        if side == "BUY":
            if low <= stop:
                exit_price, reason = stop, "Stop loss hit"
            elif high >= target:
                exit_price, reason = target, "Take profit hit"
        else:
            if high >= stop:
                exit_price, reason = stop, "Stop loss hit"
            elif low <= target:
                exit_price, reason = target, "Take profit hit"
        if exit_price is None:
            continue
        direction = 1 if side == "BUY" else -1
        pnl = (exit_price - float(row["entry_price"])) * direction * float(row["quantity"])
        pnl_pct = (pnl / STARTING_BALANCE) * 100
        closed_at = now_iso()
        connection.execute(
            """
            UPDATE paper_trades
            SET status = 'CLOSED', closed_at = ?, exit_price = ?, pnl = ?, pnl_pct = ?, close_reason = ?
            WHERE id = ?
            """,
            (closed_at, exit_price, pnl, pnl_pct, reason, row["id"]),
        )
        closed.append(
            {
                "id": row["id"],
                "exitPrice": exit_price,
                "pnl": round(pnl, 8),
                "pnlPct": round(pnl_pct, 8),
                "closeReason": reason,
                "closedAt": closed_at,
            }
        )
    if closed:
        insert_snapshot(connection)
    return {"closedTrades": closed, "account": account(connection)}


def record_cycle(connection: sqlite3.Connection, payload: Mapping[str, Any]) -> dict[str, Any]:
    scan = payload["scan"]
    ai = payload["ai"]
    risk = payload["risk"]
    candle_timestamp = str(scan["candleTimestamp"])
    existing = connection.execute(
        "SELECT id FROM scans WHERE candle_timestamp = ?", (candle_timestamp,)
    ).fetchone()
    if existing:
        return {"duplicate": True, **current_state(connection)}

    scan_time = str(scan.get("scannedAt") or now_iso())
    cursor = connection.execute(
        """
        INSERT INTO scans
        (candle_timestamp, scanned_at, symbol, timeframe, data_source, live, latest_price,
         candle_count, latest_candle_json, smc_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candle_timestamp,
            scan_time,
            str(scan.get("symbol", "EURUSD")),
            str(scan.get("timeframe", "M15")),
            str(scan.get("dataSource", "Yahoo Finance live chart data")),
            int(bool(scan.get("live", True))),
            float(scan["latestPrice"]),
            int(scan.get("candleCount", 0)),
            as_json(scan.get("latestCandle", {})),
            as_json(scan.get("smc", {})),
        ),
    )
    scan_id = cursor.lastrowid
    smc = scan.get("smc", {})
    setup_groups = (
        ("LIQUIDITY_SWEEP", smc.get("liquiditySweeps", [])),
        ("STRUCTURE_BREAK", smc.get("structureBreaks", [])),
        ("FAIR_VALUE_GAP", smc.get("fairValueGaps", [])),
        ("ORDER_BLOCK", smc.get("orderBlocks", [])),
    )
    for setup_type, entries in setup_groups:
        if not isinstance(entries, list):
            continue
        for entry in entries:
            details = entry if isinstance(entry, Mapping) else {"value": entry}
            direction = details.get("direction") or details.get("side")
            connection.execute(
                """
                INSERT INTO smc_setups
                (scan_id, setup_type, direction, details_json, detected_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (scan_id, setup_type, str(direction) if direction else None, as_json(details), scan_time),
            )

    ai_cursor = connection.execute(
        """
        INSERT INTO ai_decisions
        (scan_id, decision, confidence, reasoning, entry_price, stop_loss, take_profit,
         risk_reward_ratio, risk_amount, ai_provider, ai_model, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scan_id,
            str(ai.get("decision", "NO TRADE")),
            int(ai.get("confidence", 0)),
            str(ai.get("reasoning", "")),
            as_number(ai.get("entryPrice")),
            as_number(ai.get("stopLoss")),
            as_number(ai.get("takeProfit")),
            as_number(ai.get("riskRewardRatio")),
            as_number(ai.get("riskAmount")),
            ai.get("aiProvider"),
            ai.get("aiModel"),
            now_iso(),
        ),
    )
    ai_id = ai_cursor.lastrowid
    connection.execute(
        """
        INSERT INTO risk_decisions
        (scan_id, approved, state, reasons_json, rules_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            scan_id,
            int(bool(risk.get("approved"))),
            str(risk.get("state", "NO TRADE")),
            as_json(risk.get("reasons", [])),
            as_json(risk.get("rules", {})),
            now_iso(),
        ),
    )

    paper_trade: dict[str, Any] | None = None
    if bool(risk.get("approved")):
        entry = as_number(ai.get("entryPrice"))
        stop = as_number(ai.get("stopLoss"))
        target = as_number(ai.get("takeProfit"))
        risk_amount = as_number(ai.get("riskAmount"), 5.0)
        if entry is None or stop is None or target is None or risk_amount is None:
            raise ValueError("approved trade is missing executable paper-trade levels")
        distance = abs(entry - stop)
        if distance <= 0:
            raise ValueError("approved trade has zero entry-to-stop distance")
        trade_id = f"paper-{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        opened_at = now_iso()
        connection.execute(
            """
            INSERT INTO paper_trades
            (id, scan_id, ai_decision_id, symbol, side, status, entry_price, stop_loss,
             take_profit, risk_amount, quantity, opened_at)
            VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_id,
                scan_id,
                ai_id,
                str(scan.get("symbol", "EURUSD")),
                str(ai["decision"]),
                entry,
                stop,
                target,
                risk_amount,
                risk_amount / distance,
                opened_at,
            ),
        )
        paper_trade = serialize_trade(
            connection.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone(),
            float(scan["latestPrice"]),
        )

    insert_snapshot(connection)
    return {
        "duplicate": False,
        "scanId": scan_id,
        "paperTrade": paper_trade,
        **current_state(connection),
    }


def reset(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.executescript(
        """
        DELETE FROM account_snapshots;
        DELETE FROM paper_trades;
        DELETE FROM risk_decisions;
        DELETE FROM ai_decisions;
        DELETE FROM smc_setups;
        DELETE FROM scans;
        """
    )
    insert_snapshot(connection)
    return current_state(connection)


def set_scheduler(connection: sqlite3.Connection, payload: Mapping[str, Any]) -> dict[str, Any]:
    enabled = bool(payload.get("enabled"))
    connection.execute(
        "INSERT INTO bot_config (key, value) VALUES ('scheduler_enabled', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("1" if enabled else "0",),
    )
    return current_state(connection)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: python3 trading_bot_db.py <state|settle|record-cycle|scheduler|reset> '<json>'")
    action = sys.argv[1]
    payload = json.loads(sys.argv[2])
    with connect() as connection:
        initialize(connection)
        if action == "state":
            result = current_state(connection)
        elif action == "settle":
            result = settle(connection, payload)
        elif action == "record-cycle":
            result = record_cycle(connection, payload)
        elif action == "scheduler":
            result = set_scheduler(connection, payload)
        elif action == "reset":
            result = reset(connection)
        else:
            raise SystemExit(f"unknown database action: {action}")
        connection.commit()
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
