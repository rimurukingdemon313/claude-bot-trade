"""Shapes live TradeLocker account/position/history data into the exact
JSON contract the dashboard (src/pages/dashboard.tsx) already expects.

This is the module trading.ts calls instead of trading_bot_db.py. Nothing
here is a simulation: balance, PnL, open positions, and trade history are
all read live from TradeLocker on every call. The only thing kept
in-process is a short equity-curve sample buffer, which exists purely for
the dashboard's sparkline chart and is rebuilt from ordersHistory on every
call (not persisted), so a restart never loses real state — it just
regenerates the same curve from TradeLocker's own history the next time
this is invoked.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

import tradelocker_client as tl

STARTING_BALANCE_FALLBACK = 1000.0


def _iso(ms_or_s: Any) -> str | None:
    """TradeLocker timestamps are epoch milliseconds; normalize to ISO 8601 UTC."""

    if ms_or_s is None or ms_or_s == "":
        return None
    try:
        value = float(ms_or_s)
    except (TypeError, ValueError):
        return None
    # Heuristic: values above ~10^12 are milliseconds, otherwise seconds.
    seconds = value / 1000 if value > 10**12 else value
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")


def _num(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _side_label(side: Any) -> str:
    return "LONG" if str(side).lower() == "buy" else "SHORT"


def _instrument_name_map(config: tl.TradeLockerConfig) -> dict[str, str]:
    instruments = tl.get_instruments(config)
    return {
        str(i.get("tradableInstrumentId") or i.get("id")): str(i.get("name", ""))
        for i in instruments
    }


def build_account(config: tl.TradeLockerConfig) -> dict[str, Any]:
    state = tl.get_account_state(config)
    positions = tl.get_positions(config)
    balance = _num(state.get("balance"), STARTING_BALANCE_FALLBACK)
    today_net = _num(state.get("todayNet"))
    open_net_pnl = _num(state.get("openNetPnL"))
    history = tl.get_order_history(config, limit=500)
    filled = [row for row in history if str(row.get("status", "")).upper() == "FILLED"]

    now = datetime.now(timezone.utc)
    month_prefix = now.strftime("%Y-%m")
    monthly_pnl = 0.0
    pnl30d = 0.0
    wins = 0
    losses = 0
    cutoff_30d = now.timestamp() - 30 * 86400
    for row in filled:
        pnl = _num(row.get("realizedPl") or row.get("pnl"))
        closed_at = row.get("lastModified") or row.get("createdDate")
        closed_iso = _iso(closed_at)
        if closed_iso and closed_iso.startswith(month_prefix):
            monthly_pnl += pnl
        ts = _num(closed_at)
        ts_seconds = ts / 1000 if ts > 10**12 else ts
        if ts_seconds >= cutoff_30d:
            pnl30d += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

    trades_count = len(filled) + len(positions)
    peak = max(balance, balance - open_net_pnl if open_net_pnl else balance)
    drawdown = max(0.0, ((peak - balance) / peak) * 100) if peak else 0.0

    return {
        "balance": round(balance, 2),
        "dailyPnl": round(today_net, 2),
        "monthlyPnl": round(monthly_pnl, 2),
        "pnl30d": round(pnl30d, 2),
        "roi30d": round((pnl30d / balance) * 100, 2) if balance else 0.0,
        "trades": trades_count,
        "wins": wins,
        "losses": losses,
        "drawdown": round(drawdown, 2),
        "peak": round(peak, 2),
        "openPositions": len(positions),
    }


def build_open_trade(config: tl.TradeLockerConfig) -> dict[str, Any] | None:
    positions = tl.get_positions(config)
    if not positions:
        return None
    names = _instrument_name_map(config)
    # Most recently opened first, matching the old "most recent OPEN trade"
    # behavior in trading_bot_db.current_state().
    positions.sort(key=lambda p: _num(p.get("openDate")), reverse=True)
    position = positions[0]
    instrument_id = str(position.get("tradableInstrumentId"))
    symbol = names.get(instrument_id, instrument_id)
    entry_price = _num(position.get("avgPrice"))
    quantity = _num(position.get("qty"))
    unrealized = _num(position.get("unrealizedPl"))
    side = str(position.get("side", "buy")).upper()

    return {
        "id": str(position.get("id")),
        "symbol": symbol,
        "side": side,
        "status": "OPEN",
        "entryPrice": entry_price,
        # TradeLocker positions carry SL/TP as linked order ids
        # (stopLossId/takeProfitId), not raw prices; the linked orders are
        # matched against /orders below when available.
        "stopLoss": position.get("_resolvedStopLoss", 0.0),
        "takeProfit": position.get("_resolvedTakeProfit", 0.0),
        "riskAmount": None,
        "quantity": quantity,
        "openedAt": _iso(position.get("openDate")),
        "currentPrice": None,
        "unrealizedPnl": round(unrealized, 2),
        "unrealizedPnlPct": None,
    }


def _attach_sl_tp(config: tl.TradeLockerConfig, open_trade: dict[str, Any] | None) -> None:
    """Fill in real SL/TP prices for the open position from /orders, where
    stopLossId/takeProfitId on the position match an order's id."""

    if open_trade is None:
        return
    positions = tl.get_positions(config)
    position = next((p for p in positions if str(p.get("id")) == open_trade["id"]), None)
    if position is None:
        return
    orders = tl.get_orders(config)
    orders_by_id = {str(o.get("id")): o for o in orders}
    sl_order = orders_by_id.get(str(position.get("stopLossId")))
    tp_order = orders_by_id.get(str(position.get("takeProfitId")))
    if sl_order is not None:
        open_trade["stopLoss"] = _num(sl_order.get("stopPrice") or sl_order.get("price"))
    if tp_order is not None:
        open_trade["takeProfit"] = _num(tp_order.get("price"))


def build_trades(config: tl.TradeLockerConfig, limit: int = 50) -> list[dict[str, Any]]:
    names = _instrument_name_map(config)
    history = tl.get_order_history(config, limit=limit)
    positions = tl.get_positions(config)
    rows: list[dict[str, Any]] = []

    for position in positions:
        instrument_id = str(position.get("tradableInstrumentId"))
        rows.append(
            {
                "id": str(position.get("id")),
                "time": _iso(position.get("openDate")),
                "symbol": names.get(instrument_id, instrument_id),
                "side": _side_label(position.get("side")),
                "setup": "Open position",
                "result": "OPEN",
                "pnl": round(_num(position.get("unrealizedPl")), 2),
                "pnlPct": 0.0,
                "confidence": 0,
                "entryPrice": _num(position.get("avgPrice")),
                "stopLoss": None,
                "takeProfit": None,
                "openedAt": _iso(position.get("openDate")),
                "closedAt": None,
            }
        )

    for row in history:
        if str(row.get("status", "")).upper() != "FILLED":
            continue
        instrument_id = str(row.get("tradableInstrumentId"))
        pnl = _num(row.get("realizedPl") or row.get("pnl"))
        rows.append(
            {
                "id": str(row.get("id")),
                "time": _iso(row.get("lastModified") or row.get("createdDate")),
                "symbol": names.get(instrument_id, instrument_id),
                "side": _side_label(row.get("side")),
                "setup": "TradeLocker fill",
                "result": "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "OPEN",
                "pnl": round(pnl, 2),
                "pnlPct": 0.0,
                "confidence": 0,
                "entryPrice": _num(row.get("avgPrice") or row.get("price")),
                "stopLoss": _num(row.get("stopLoss")) if row.get("stopLoss") else None,
                "takeProfit": _num(row.get("takeProfit")) if row.get("takeProfit") else None,
                "openedAt": _iso(row.get("createdDate")),
                "closedAt": _iso(row.get("lastModified")),
            }
        )

    rows.sort(key=lambda r: r.get("time") or "", reverse=True)
    return rows[:limit]


def build_equity_curve(account: dict[str, Any], trades: list[dict[str, Any]]) -> list[float]:
    """Reconstruct a simple equity curve by walking closed trades backward
    from the current balance. Not persisted — recomputed fresh from
    TradeLocker's own trade history on every call, so it survives restarts
    by definition (there is no separate state to lose)."""

    closed = [t for t in trades if t.get("result") in ("WIN", "LOSS")]
    closed.sort(key=lambda t: t.get("time") or "")
    running = account["balance"] - sum(t["pnl"] for t in closed)
    curve = [round(running, 2)]
    for trade in closed:
        running += trade["pnl"]
        curve.append(round(running, 2))
    return curve[-30:] if len(curve) > 1 else curve


def current_state(config: tl.TradeLockerConfig) -> dict[str, Any]:
    account = build_account(config)
    open_trade = build_open_trade(config)
    _attach_sl_tp(config, open_trade)
    trades = build_trades(config)
    equity_curve = build_equity_curve(account, trades)
    return {
        "account": account,
        "openTrade": open_trade,
        "trades": trades,
        "equityCurve": equity_curve,
        # latestAiDecision / latestRiskDecision / latestScan are populated
        # by trading.ts from the current in-memory AI+risk cycle result,
        # not by TradeLocker (TradeLocker has no concept of "AI decision").
        "latestAiDecision": None,
        "latestRiskDecision": None,
        "latestScan": None,
        "schedulerEnabled": True,
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python3 tradelocker_state.py <state>")
    action = sys.argv[1]
    config = tl.TradeLockerConfig.from_env()
    if action == "state":
        result = current_state(config)
    else:
        raise SystemExit(f"unknown action: {action}")
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
