"""TradeLocker REST API client.

Replaces the old paper-trading SQLite ledger as the single source of truth
for account balance, open positions, and trade history. Every call in this
module hits TradeLocker's backend-api directly — nothing about account
state is cached to disk, so a Railway restart or redeploy never loses real
data (it is always re-read live from TradeLocker).

Environment variables required:
    TRADELOCKER_EMAIL
    TRADELOCKER_PASSWORD
    TRADELOCKER_SERVER
    TRADELOCKER_ACC_ID      (the numeric accountId shown in the account
                             switcher after the '#', used in the URL path)
    TRADELOCKER_URL         (e.g. https://demo.tradelocker.com/backend-api)

TradeLocker API shape notes (from https://public-api.tradelocker.com):
  - Every /trade/* response is wrapped as {"s": "ok", "d": {...}}.
  - Every /trade/* request needs header 'accNum' = the account's accNum
    (NOT the same as accountId — resolved once via /auth/jwt/all-accounts
    and cached in-memory alongside the token).
  - positions / orders / ordersHistory come back as positional arrays
    (["7277...", "206", ...]) whose column order is defined by
    /trade/config (positionsConfig / ordersConfig / ordersHistoryConfig).
    This module fetches /trade/config once per process and zips columns
    with values into named dicts, so the rest of the code never touches
    raw indices.
  - accountDetailsData (from /trade/accounts/{id}/state) is a flat
    positional number array, named the same way via accountDetailsConfig.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_BASE_URL = "https://demo.tradelocker.com/backend-api"


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass
class TradeLockerConfig:
    email: str
    password: str
    server: str
    acc_id: str
    base_url: str

    @classmethod
    def from_env(cls) -> "TradeLockerConfig":
        return cls(
            email=_env("TRADELOCKER_EMAIL"),
            password=_env("TRADELOCKER_PASSWORD"),
            server=_env("TRADELOCKER_SERVER"),
            acc_id=_env("TRADELOCKER_ACC_ID"),
            base_url=os.environ.get("TRADELOCKER_URL", DEFAULT_BASE_URL).rstrip("/"),
        )


class TradeLockerError(RuntimeError):
    """Raised for any non-2xx response, transport failure, or bad payload shape."""


# In-memory session cache. A fresh python3 process is spawned per CLI call
# (mirroring the old trading_bot_db.py subprocess model), so re-login on
# every invocation is the norm; this cache only helps if a single process
# makes several calls (e.g. the small helper functions below calling each
# other within one `main()` run).
_session: dict[str, Any] = {
    "access_token": None,
    "refresh_token": None,
    "expires_at": 0.0,
    "acc_num": None,
    "config": None,
}


def _http(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise TradeLockerError(
            f"TradeLocker {method} {url} failed ({exc.code}): {detail[:500]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise TradeLockerError(f"TradeLocker {method} {url} unreachable: {exc}") from exc


def _unwrap(payload: Any) -> Any:
    """Every /trade/* response is {"s": "ok", "d": {...}}. Unwrap to 'd'."""

    if isinstance(payload, Mapping) and "d" in payload and "s" in payload:
        if payload.get("s") != "ok":
            raise TradeLockerError(f"TradeLocker returned non-ok status: {payload}")
        return payload["d"]
    return payload


def _login(config: TradeLockerConfig) -> None:
    """POST /auth/jwt/token — obtain a fresh access + refresh token pair."""

    result = _http(
        "POST",
        f"{config.base_url}/auth/jwt/token",
        body={"email": config.email, "password": config.password, "server": config.server},
    )
    access_token = result.get("accessToken")
    refresh_token = result.get("refreshToken")
    if not access_token:
        raise TradeLockerError(f"TradeLocker login did not return an accessToken: {result}")
    _session["access_token"] = access_token
    _session["refresh_token"] = refresh_token
    # expireDate is provided, but access tokens are short-lived (~1hr on
    # demo, can be shorter); refresh 2 minutes early to avoid a request
    # straddling expiry.
    _session["expires_at"] = time.time() + 55 * 60
    _session["acc_num"] = None  # force re-resolution against the new session
    _session["config"] = None


def _refresh(config: TradeLockerConfig) -> bool:
    """POST /auth/jwt/refresh — exchange the refresh token for a new access token.

    Returns False on failure so the caller falls back to a full re-login
    instead of raising mid-request.
    """

    refresh_token = _session.get("refresh_token")
    if not refresh_token:
        return False
    try:
        result = _http(
            "POST",
            f"{config.base_url}/auth/jwt/refresh",
            body={"refreshToken": refresh_token},
        )
    except TradeLockerError:
        return False
    access_token = result.get("accessToken")
    if not access_token:
        return False
    _session["access_token"] = access_token
    if result.get("refreshToken"):
        _session["refresh_token"] = result["refreshToken"]
    _session["expires_at"] = time.time() + 55 * 60
    return True


def _resolve_acc_num(config: TradeLockerConfig) -> str:
    """GET /auth/jwt/all-accounts — map our accountId to its accNum.

    accountId (path parameter) and accNum (required header on every
    /trade/* call) are different identifiers in TradeLocker's model; this
    resolves the second from the first exactly once per session.
    """

    if _session["acc_num"] is not None:
        return _session["acc_num"]
    result = _http(
        "GET",
        f"{config.base_url}/auth/jwt/all-accounts",
        headers={"Authorization": f"Bearer {_session['access_token']}"},
    )
    accounts = result.get("accounts", [])
    match = next((a for a in accounts if str(a.get("id")) == str(config.acc_id)), None)
    if match is None and accounts:
        # Fall back to the first account rather than hard-failing, in case
        # TRADELOCKER_ACC_ID was actually already an accNum.
        match = accounts[0]
    if match is None:
        raise TradeLockerError(
            f"Could not find account id {config.acc_id} in /auth/jwt/all-accounts response: {result}"
        )
    acc_num = str(match.get("accNum"))
    _session["acc_num"] = acc_num
    return acc_num


def _ensure_session(config: TradeLockerConfig) -> None:
    if not _session["access_token"]:
        _login(config)
    elif time.time() >= _session["expires_at"]:
        if not _refresh(config):
            _login(config)
    if _session["acc_num"] is None:
        _resolve_acc_num(config)


def _authed(
    config: TradeLockerConfig,
    method: str,
    path: str,
    *,
    body: Mapping[str, Any] | None = None,
    query: Mapping[str, str] | None = None,
    _retried: bool = False,
) -> Any:
    _ensure_session(config)
    url = f"{config.base_url}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    headers = {
        "Authorization": f"Bearer {_session['access_token']}",
        "accNum": _session["acc_num"],
    }
    try:
        return _unwrap(_http(method, url, headers=headers, body=body))
    except TradeLockerError as exc:
        # A 401/403 mid-session means the token was invalidated server
        # side (not just expired). Force one full re-login and retry, so a
        # long-running Railway process self-heals instead of dying.
        if not _retried and ("(401)" in str(exc) or "(403)" in str(exc)):
            _login(config)
            return _authed(config, method, path, body=body, query=query, _retried=True)
        raise


def _get_config(config: TradeLockerConfig) -> dict[str, Any]:
    """GET /trade/config — cached per process; defines column order for
    positions / orders / ordersHistory / accountDetails."""

    if _session["config"] is not None:
        return _session["config"]
    result = _authed(config, "GET", "/trade/config")
    _session["config"] = result
    return result


def _columns(panel_config: Mapping[str, Any]) -> list[str]:
    return [str(col.get("id")) for col in panel_config.get("columns", [])]


def _rows_to_dicts(rows: list[list[Any]], columns: list[str]) -> list[dict[str, Any]]:
    return [dict(zip(columns, row)) for row in rows]


def _num(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Public account / market data
# --------------------------------------------------------------------------


def get_account_details(config: TradeLockerConfig) -> dict[str, Any]:
    """GET /trade/accounts/{accountId} — static account info (currency etc)."""

    return _authed(config, "GET", f"/trade/accounts/{config.acc_id}")


def get_account_state(config: TradeLockerConfig) -> dict[str, Any]:
    """Balance, PnL, margin, etc — named via accountDetailsConfig columns."""

    cfg = _get_config(config)
    columns = _columns(cfg.get("accountDetailsConfig", {}))
    result = _authed(config, "GET", f"/trade/accounts/{config.acc_id}/state")
    values = result.get("accountDetailsData", [])
    return dict(zip(columns, values))


def get_positions(config: TradeLockerConfig) -> list[dict[str, Any]]:
    """All currently open positions, named via positionsConfig columns."""

    cfg = _get_config(config)
    columns = _columns(cfg.get("positionsConfig", {}))
    result = _authed(config, "GET", f"/trade/accounts/{config.acc_id}/positions")
    return _rows_to_dicts(result.get("positions", []), columns)


def get_orders(config: TradeLockerConfig) -> list[dict[str, Any]]:
    """Pending/working (non-final) orders, named via ordersConfig columns."""

    cfg = _get_config(config)
    columns = _columns(cfg.get("ordersConfig", {}))
    result = _authed(config, "GET", f"/trade/accounts/{config.acc_id}/orders")
    return _rows_to_dicts(result.get("orders", []), columns)


def get_order_history(config: TradeLockerConfig, limit: int = 50) -> list[dict[str, Any]]:
    """Closed/final orders (filled, canceled, rejected) — used to build the
    trade log and realized PnL history. Named via ordersHistoryConfig."""

    cfg = _get_config(config)
    columns = _columns(cfg.get("ordersHistoryConfig", {}))
    result = _authed(
        config, "GET", f"/trade/accounts/{config.acc_id}/ordersHistory"
    )
    rows = _rows_to_dicts(result.get("ordersHistory", []), columns)
    # Most-recent-first, matching the old paper_trades ordering the
    # dashboard expects.
    rows.sort(key=lambda r: _num(r.get("lastModified") or r.get("createdDate")), reverse=True)
    return rows[:limit]


def get_instruments(config: TradeLockerConfig) -> list[dict[str, Any]]:
    result = _authed(config, "GET", f"/trade/accounts/{config.acc_id}/instruments")
    return result.get("instruments", [])


def find_instrument(config: TradeLockerConfig, symbol: str) -> dict[str, Any] | None:
    """Resolve a human symbol like 'EURUSD' to its tradableInstrumentId and
    TRADE routeId (both required by place_market_order)."""

    target = symbol.upper().replace("/", "")
    for instrument in get_instruments(config):
        name = str(instrument.get("name", "")).upper().replace("/", "")
        if name != target:
            continue
        trade_route = next(
            (r for r in instrument.get("routes", []) if r.get("type") == "TRADE"),
            None,
        )
        return {
            "tradableInstrumentId": instrument.get("tradableInstrumentId") or instrument.get("id"),
            "routeId": trade_route.get("id") if trade_route else None,
        }
    return None


def get_quote(config: TradeLockerConfig, instrument_id: int, route_id: int) -> dict[str, Any]:
    """Latest bid/ask for an instrument."""

    return _authed(
        config,
        "GET",
        f"/trade/accounts/{config.acc_id}/instruments/{instrument_id}/quotes",
        query={"routeId": str(route_id)},
    )


# --------------------------------------------------------------------------
# Order execution
# --------------------------------------------------------------------------


def place_market_order(
    config: TradeLockerConfig,
    *,
    tradable_instrument_id: int,
    route_id: int,
    side: str,
    quantity: float,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    strategy_id: str | None = None,
) -> dict[str, Any]:
    """POST /trade/accounts/{accountId}/orders — market order with attached SL/TP."""

    body: dict[str, Any] = {
        "tradableInstrumentId": tradable_instrument_id,
        "routeId": route_id,
        "qty": quantity,
        "side": side.lower(),  # "buy" | "sell"
        "type": "market",
        "validity": "IOC",
        "price": 0,
    }
    if stop_loss is not None:
        body["stopLoss"] = stop_loss
        body["stopLossType"] = "absolute"
    if take_profit is not None:
        body["takeProfit"] = take_profit
        body["takeProfitType"] = "absolute"
    if strategy_id:
        body["strategyId"] = strategy_id
    return _authed(config, "POST", f"/trade/accounts/{config.acc_id}/orders", body=body)


def close_position(config: TradeLockerConfig, position_id: str) -> dict[str, Any]:
    """DELETE /trade/positions/{positionId} — qty 0 fully closes the position."""

    return _authed(config, "DELETE", f"/trade/positions/{position_id}", body={"qty": 0})


def close_all_positions(config: TradeLockerConfig) -> dict[str, Any]:
    return _authed(config, "DELETE", f"/trade/accounts/{config.acc_id}/positions")


# --------------------------------------------------------------------------
# CLI entry point — trading.ts shells out to this file the same way it
# used to shell out to trading_bot_db.py.
# --------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(
            "usage: python3 tradelocker_client.py "
            "<state|positions|orders|history|instrument|place-order|close-position> '<json>'"
        )
    action = sys.argv[1]
    payload = json.loads(sys.argv[2])
    config = TradeLockerConfig.from_env()

    if action == "state":
        result = get_account_state(config)
    elif action == "positions":
        result = get_positions(config)
    elif action == "orders":
        result = get_orders(config)
    elif action == "history":
        result = get_order_history(config, limit=int(payload.get("limit", 50)))
    elif action == "instrument":
        result = find_instrument(config, str(payload["symbol"]))
    elif action == "place-order":
        result = place_market_order(
            config,
            tradable_instrument_id=int(payload["tradableInstrumentId"]),
            route_id=int(payload["routeId"]),
            side=str(payload["side"]),
            quantity=float(payload["quantity"]),
            stop_loss=payload.get("stopLoss"),
            take_profit=payload.get("takeProfit"),
            strategy_id=payload.get("strategyId"),
        )
    elif action == "close-position":
        result = close_position(config, str(payload["positionId"]))
    else:
        raise SystemExit(f"unknown action: {action}")

    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
