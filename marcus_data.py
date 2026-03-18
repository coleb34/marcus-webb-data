#!/usr/bin/env python3
"""
marcus_data.py — Data fetching layer for Marcus Webb investor agent.

Handles:
  - Alpaca: portfolio, positions, account value, order execution
  - Finnhub: real-time quotes, company news, basic financials

Environment variables required:
  ALPACA_API_KEY        -- from alpaca.markets (paper trading)
  ALPACA_SECRET_KEY     -- from alpaca.markets (paper trading)
  FINNHUB_API_KEY       -- from finnhub.io (free tier)

Install dependencies:
  pip install requests --break-system-packages
"""

import os
import json
import requests
from datetime import datetime, timedelta
from typing import Optional

# ── Alpaca config ──────────────────────────────────────────────────────────────
# Paper trading endpoint -- safe, no real money
ALPACA_BASE_URL = "https://paper-api.alpaca.markets/v2"
ALPACA_DATA_URL = "https://data.alpaca.markets/v2"

ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
    "Content-Type": "application/json"
}

# ── Finnhub config ─────────────────────────────────────────────────────────────
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")


# ══════════════════════════════════════════════════════════════════════════════
# ALPACA — Portfolio & Account
# ══════════════════════════════════════════════════════════════════════════════

def get_account() -> dict:
    """
    Fetch account overview from Alpaca.
    Returns portfolio value, cash, buying power.
    """
    resp = requests.get(f"{ALPACA_BASE_URL}/account", headers=ALPACA_HEADERS)
    resp.raise_for_status()
    data = resp.json()

    return {
        "portfolio_value": float(data["portfolio_value"]),
        "cash": float(data["cash"]),
        "buying_power": float(data["buying_power"]),
        "equity": float(data["equity"]),
        "last_equity": float(data["last_equity"]),
        "daily_pnl": float(data["equity"]) - float(data["last_equity"]),
        "daily_pnl_pct": (
            (float(data["equity"]) - float(data["last_equity"]))
            / float(data["last_equity"]) * 100
        ) if float(data["last_equity"]) > 0 else 0
    }


def get_positions() -> list[dict]:
    """
    Fetch all open positions from Alpaca.
    Returns list of positions with P&L and weight.
    """
    resp = requests.get(f"{ALPACA_BASE_URL}/positions", headers=ALPACA_HEADERS)
    resp.raise_for_status()
    raw = resp.json()

    positions = []
    for p in raw:
        positions.append({
            "ticker": p["symbol"],
            "qty": float(p["qty"]),
            "market_value": float(p["market_value"]),
            "cost_basis": float(p["cost_basis"]),
            "avg_entry_price": float(p["avg_entry_price"]),
            "current_price": float(p["current_price"]),
            "unrealized_pnl": float(p["unrealized_pl"]),
            "unrealized_pnl_pct": float(p["unrealized_plpc"]) * 100,
            "todays_pnl": float(p["unrealized_intraday_pl"])
        })

    return sorted(positions, key=lambda x: x["market_value"], reverse=True)


def get_portfolio_weights(positions: list[dict], account: dict) -> list[dict]:
    """Add portfolio weight % to each position."""
    total = account["portfolio_value"]
    for p in positions:
        p["weight_pct"] = (p["market_value"] / total * 100) if total > 0 else 0
    return positions


def is_market_open() -> bool:
    """Check if the US market is currently open."""
    resp = requests.get(f"{ALPACA_BASE_URL}/clock", headers=ALPACA_HEADERS)
    resp.raise_for_status()
    return resp.json()["is_open"]


def is_trading_day() -> bool:
    """Check if today is a trading day (handles weekends and holidays)."""
    today = datetime.now().strftime("%Y-%m-%dT00:00:00Z")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")

    resp = requests.get(
        f"{ALPACA_BASE_URL}/calendar",
        headers=ALPACA_HEADERS,
        params={"start": today, "end": tomorrow}
    )
    resp.raise_for_status()
    return len(resp.json()) > 0


def get_recent_orders(limit: int = 20) -> list[dict]:
    """Fetch recent orders for logging and email context."""
    resp = requests.get(
        f"{ALPACA_BASE_URL}/orders",
        headers=ALPACA_HEADERS,
        params={"status": "all", "limit": limit, "direction": "desc"}
    )
    resp.raise_for_status()

    orders = []
    for o in resp.json():
        orders.append({
            "ticker": o["symbol"],
            "side": o["side"],
            "qty": o.get("qty"),
            "filled_qty": o.get("filled_qty"),
            "filled_avg_price": o.get("filled_avg_price"),
            "status": o["status"],
            "submitted_at": o["submitted_at"],
            "type": o["order_type"]
        })
    return orders

# check for pending orders

def get_pending_orders() -> list[dict]:
    """Fetch all open/pending orders not yet filled."""
    resp = requests.get(
        f"{ALPACA_BASE_URL}/orders",
        headers=ALPACA_HEADERS,
        params={"status": "open", "limit": 50, "direction": "desc"}
    )
    resp.raise_for_status()

    orders = []
    for o in resp.json():
        orders.append({
            "ticker": o["symbol"],
            "side": o["side"],
            "notional": o.get("notional"),
            "qty": o.get("qty"),
            "status": o["status"],
            "submitted_at": o["submitted_at"]
        })
    return orders

# ══════════════════════════════════════════════════════════════════════════════
# ALPACA — Order Execution
# ══════════════════════════════════════════════════════════════════════════════

def place_order(ticker: str, side: str, notional: Optional[float] = None,
                qty: Optional[float] = None) -> dict:
    """
    Place a market order via Alpaca paper trading.

    Args:
        ticker:   stock symbol e.g. "AAPL"
        side:     "buy" or "sell"
        notional: dollar amount to trade (use this OR qty, not both)
        qty:      number of shares (use this OR notional, not both)

    Returns dict with order confirmation or error.
    """
    if not notional and not qty:
        raise ValueError("Must provide either notional or qty")

    payload = {
        "symbol": ticker,
        "side": side,
        "type": "market",
        "time_in_force": "day"
    }

    if notional:
        payload["notional"] = str(round(notional, 2))
    else:
        payload["qty"] = str(qty)

    resp = requests.post(
        f"{ALPACA_BASE_URL}/orders",
        headers=ALPACA_HEADERS,
        json=payload
    )

    if resp.status_code not in (200, 201):
        return {
            "success": False,
            "ticker": ticker,
            "error": resp.text
        }

    data = resp.json()
    return {
        "success": True,
        "ticker": ticker,
        "side": side,
        "order_id": data["id"],
        "status": data["status"],
        "notional": notional,
        "qty": qty
    }


def close_position(ticker: str) -> dict:
    """Fully close an open position."""
    resp = requests.delete(
        f"{ALPACA_BASE_URL}/positions/{ticker}",
        headers=ALPACA_HEADERS
    )

    if resp.status_code not in (200, 201):
        return {"success": False, "ticker": ticker, "error": resp.text}

    return {"success": True, "ticker": ticker, "action": "closed"}


def execute_trades(trades: list[dict], account: dict) -> list[dict]:
    """
    Execute a list of trades from Claude's output.

    Expected trade format:
      {
        "ticker": "AAPL",
        "action": "buy" | "sell" | "close",
        "allocation_pct": 12.5,   # % of portfolio (for buys)
        "reasoning": "..."
      }

    Returns list of execution results.
    """
    results = []
    portfolio_value = account["portfolio_value"]

    # Track available cash across this batch of trades
    # Use non-marginable buying power to strictly avoid margin
    available_cash = account.get("buying_power", account["cash"])
    # If cash is negative we're already on margin -- block all buys
    if available_cash < 0:
        available_cash = 0

    for trade in trades:
        ticker = trade["ticker"].upper()
        action = trade["action"].lower()

        try:
            if action == "close":
                result = close_position(ticker)

            elif action == "sell":
                notional = (trade.get("allocation_pct", 0) / 100) * portfolio_value
                result = place_order(ticker, "sell", notional=notional)
                # Selling frees up cash
                available_cash += notional

            elif action == "buy":
                notional = (trade.get("allocation_pct", 0) / 100) * portfolio_value

                # GUARDRAIL: never buy more than available cash
                if notional > available_cash:
                    result = {
                        "success": False,
                        "ticker": ticker,
                        "error": (
                            f"Skipped: would require ${notional:,.2f} "
                            f"but only ${available_cash:,.2f} cash available. "
                            f"Refusing to buy on margin."
                        )
                    }
                else:
                    result = place_order(ticker, "buy", notional=notional)
                    available_cash -= notional

            else:
                result = {"success": False, "ticker": ticker,
                          "error": f"Unknown action: {action}"}

            result["reasoning"] = trade.get("reasoning", "")
            results.append(result)

        except Exception as e:
            results.append({
                "success": False,
                "ticker": ticker,
                "action": action,
                "error": str(e)
            })

    return results

# ══════════════════════════════════════════════════════════════════════════════
# FINNHUB — Market Data & News
# ══════════════════════════════════════════════════════════════════════════════

def get_quote(ticker: str) -> dict:
    """Get real-time quote for a ticker."""
    resp = requests.get(
        f"{FINNHUB_BASE_URL}/quote",
        params={"symbol": ticker, "token": FINNHUB_KEY}
    )
    resp.raise_for_status()
    data = resp.json()

    return {
        "ticker": ticker,
        "current": data.get("c"),
        "open": data.get("o"),
        "high": data.get("h"),
        "low": data.get("l"),
        "prev_close": data.get("pc"),
        "change_pct": (
            ((data["c"] - data["pc"]) / data["pc"] * 100)
            if data.get("pc") and data.get("c") else None
        )
    }


def get_company_news(ticker: str, days_back: int = 3) -> list[dict]:
    """
    Fetch recent news for a ticker.
    Returns up to 10 articles from the past N days.
    """
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    resp = requests.get(
        f"{FINNHUB_BASE_URL}/company-news",
        params={"symbol": ticker, "from": start, "to": end, "token": FINNHUB_KEY}
    )
    resp.raise_for_status()
    articles = resp.json()

    # Return most recent 10, trimmed for token efficiency
    return [
        {
            "headline": a["headline"],
            "summary": a.get("summary", "")[:300],
            "source": a.get("source"),
            "datetime": datetime.fromtimestamp(a["datetime"]).strftime("%Y-%m-%d %H:%M")
        }
        for a in articles[:10]
    ]


def get_basic_financials(ticker: str) -> dict:
    """
    Fetch key financial metrics for a ticker.
    P/E, P/B, EPS, revenue growth, margins etc.
    """
    resp = requests.get(
        f"{FINNHUB_BASE_URL}/stock/metric",
        params={"symbol": ticker, "metric": "all", "token": FINNHUB_KEY}
    )
    resp.raise_for_status()
    data = resp.json().get("metric", {})

    # Pull the most relevant value metrics
    return {
        "ticker": ticker,
        "pe_ttm": data.get("peTTM"),
        "pb": data.get("pbAnnual"),
        "ps_ttm": data.get("psTTM"),
        "ev_ebitda": data.get("currentEv/freeCashFlowTTM"),
        "eps_ttm": data.get("epsTTM"),
        "revenue_growth_ttm": data.get("revenueGrowthTTMYoy"),
        "gross_margin": data.get("grossMarginTTM"),
        "net_margin": data.get("netMarginTTM"),
        "roe": data.get("roeTTM"),
        "debt_equity": data.get("totalDebt/totalEquityAnnual"),
        "current_ratio": data.get("currentRatioAnnual"),
        "52w_high": data.get("52WeekHigh"),
        "52w_low": data.get("52WeekLow"),
        "52w_high_date": data.get("52WeekHighDate"),
        "52w_low_date": data.get("52WeekLowDate"),
        "beta": data.get("beta"),
        "market_cap": data.get("marketCapitalization")
    }


def get_market_news(days_back: int = 1) -> list[dict]:
    """Fetch general market news (macro context for Marcus)."""
    resp = requests.get(
        f"{FINNHUB_BASE_URL}/news",
        params={"category": "general", "token": FINNHUB_KEY}
    )
    resp.raise_for_status()
    articles = resp.json()

    cutoff = datetime.now() - timedelta(days=days_back)
    recent = [
        a for a in articles
        if datetime.fromtimestamp(a["datetime"]) > cutoff
    ]

    return [
        {
            "headline": a["headline"],
            "summary": a.get("summary", "")[:300],
            "source": a.get("source"),
            "datetime": datetime.fromtimestamp(a["datetime"]).strftime("%Y-%m-%d %H:%M")
        }
        for a in recent[:8]
    ]


# ══════════════════════════════════════════════════════════════════════════════
# AGGREGATOR — Full daily context bundle for Claude
# ══════════════════════════════════════════════════════════════════════════════

def build_market_context(watchlist: list[str] = []) -> dict:
    """
    Build the complete daily context bundle.
    Called once per day before handing off to Claude.

    Returns everything Marcus needs to make decisions:
      - account summary
      - positions with news and financials
      - watchlist with quotes and news
      - macro market news
    """
    print("Fetching account data...")
    account = get_account()

    print("Fetching positions...")
    positions = get_positions()
    positions = get_portfolio_weights(positions, account)

    print("Fetching position data...")
    enriched_positions = []
    for p in positions:
        ticker = p["ticker"]
        print(f"  {ticker}...")
        p["quote"] = get_quote(ticker)
        p["news"] = get_company_news(ticker, days_back=2)
        p["financials"] = get_basic_financials(ticker)
        enriched_positions.append(p)

    print("Fetching watchlist data...")
    watchlist_data = []
    for ticker in watchlist:
        print(f"  {ticker}...")
        watchlist_data.append({
            "ticker": ticker,
            "quote": get_quote(ticker),
            "news": get_company_news(ticker, days_back=3),
            "financials": get_basic_financials(ticker)
        })

    print("Fetching market news...")
    market_news = get_market_news(days_back=1)

    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "account": account,
        "positions": enriched_positions,
        "watchlist": watchlist_data,
        "market_news": market_news,
        "pending_orders": get_pending_orders(),
        "position_count": len(positions),
        "cash_pct": (account["cash"] / account["portfolio_value"] * 100)
                    if account["portfolio_value"] > 0 else 0
    }


# ══════════════════════════════════════════════════════════════════════════════
# Quick test — run directly to verify API connections
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("Testing Alpaca connection...")
    try:
        account = get_account()
        print(f"  Portfolio value: ${account['portfolio_value']:,.2f}")
        print(f"  Cash: ${account['cash']:,.2f}")
        print(f"  Daily P&L: ${account['daily_pnl']:,.2f}")
        print("  Alpaca OK")
    except Exception as e:
        print(f"  Alpaca ERROR: {e}")

    print("\nTesting Finnhub connection...")
    try:
        quote = get_quote("AAPL")
        print(f"  AAPL current price: ${quote['current']}")
        print(f"  AAPL change: {quote['change_pct']:.2f}%")
        print("  Finnhub OK")
    except Exception as e:
        print(f"  Finnhub ERROR: {e}")

    print("\nTesting market news...")
    try:
        news = get_market_news()
        print(f"  Retrieved {len(news)} articles")
        if news:
            print(f"  Latest: {news[0]['headline'][:60]}...")
        print("  News OK")
    except Exception as e:
        print(f"  News ERROR: {e}")

    print("\nAll connection tests complete.")