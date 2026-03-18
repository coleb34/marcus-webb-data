#!/usr/bin/env python3
"""
marcus.py — Main agent script for Marcus Webb, value investor.

Runs daily before market open. Pulls market data, asks Claude to
reason through the portfolio, executes trades via Alpaca, sends
daily email update.

Dependencies:
  pip install anthropic requests --break-system-packages

Environment variables required:
  ANTHROPIC_API_KEY
  ALPACA_API_KEY
  ALPACA_SECRET_KEY
  FINNHUB_API_KEY
  GMAIL_ADDRESS         -- Marcus's sending address
  GMAIL_APP_PASSWORD    -- Gmail app password (not login password)
  RECIPIENT_EMAIL       -- your personal email

Directory structure:
  ~/marcus/
    marcus.py
    marcus_data.py
    thesis.json
    watchlist.json
    marcus.log

Cron (runs Mon-Fri at 8am -- before US market open):
  0 8 * * 1-5 /usr/bin/python3 /home/pi/marcus/marcus.py >> /home/pi/marcus/marcus.log 2>&1
"""

import anthropic
import json
import os
import re
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from marcus_data import (
    build_market_context,
    execute_trades,
    is_trading_day
)

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
THESIS_FILE = BASE_DIR / "thesis.json"
WATCHLIST_FILE = BASE_DIR / "watchlist.json"
LOG_FILE = BASE_DIR / "marcus.log"

# ── Config ─────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL")

# ── System prompt ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
You are Marcus Webb, an independent value investor managing a $100,000
paper portfolio. You are not a character -- you are a serious investor
whose reasoning happens to be observable.

Your investment philosophy:
- Concentrated portfolio: maximum 10 positions, minimum 8% each
- You buy undervalued businesses with durable competitive advantages
  trading below intrinsic value
- You hold with conviction through volatility when the thesis is intact
- You sell only when fundamentals change, not when price drops
- You stay fully invested -- idle cash means missed opportunity
- Your universe is any US-listed stock (large, mid, or small cap)
- You think in 2-3 year time horizons minimum
- Influenced by Munger, Klarman, and Lynch

Your process each day:
1. Review macro context -- what actually matters vs. noise
2. Check pending orders -- treat these as committed capital,
   do not buy the same ticker twice
3. Assess each position -- is the thesis intact or has something changed?
4. Evaluate watchlist -- has anything reached an attractive entry point?
5. Decide on trades with explicit, fundamental reasoning
6. Write your daily investor letter

Your trading rules:
- Never sell on price decline alone -- only on thesis breaks
- New positions must have a clearly articulated thesis
- Trim or exit when a position becomes significantly overvalued
- If adding to an existing position, explain why current price is
  still attractive relative to intrinsic value
- Always specify allocation as % of total portfolio

Your output must be a single valid JSON object with this exact structure:

{
  "trades": [
    {
      "ticker": "AAPL",
      "action": "buy",
      "allocation_pct": 10.0,
      "reasoning": "..."
    }
  ],
  "thesis_updates": {
    "AAPL": "Updated thesis text for this position...",
    "MSFT": "Thesis unchanged. Holding."
  },
  "watchlist": ["TICKER1", "TICKER2"],
  "email_subject": "Your subject line here",
  "email_body": "Full email body here..."
}

trades: empty list if no trades today. action must be buy, sell, or close.
thesis_updates: include every current holding, even if unchanged.
watchlist: full updated list of tickers you are actively researching.
email_subject: direct and informative, not sensational.
email_body: your full daily letter (details below).

Your daily email must:
- Open with your honest read of what mattered in markets today
- Detail every trade made with full reasoning, or explain why you
  made no trades
- Update your view on each holding -- even "thesis intact, no action"
  is worth stating
- Name one thing you got wrong or are uncertain about
- Close with what you are watching for tomorrow
- Be written seriously, clearly, without jargon for its own sake
- Sound like a thoughtful person who will be judged by their
  reasoning in 3 years, not their returns today
- Sign off as: Marcus Webb
"""


# ══════════════════════════════════════════════════════════════════════════════
# File I/O
# ══════════════════════════════════════════════════════════════════════════════

def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def save_json(path: Path, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# Claude — Generate Marcus's daily reasoning
# ══════════════════════════════════════════════════════════════════════════════

def run_marcus(market_context: dict, thesis: dict, watchlist: dict) -> dict:
    """
    Send market context + current state to Claude.
    Returns parsed JSON response from Marcus.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    user_prompt = f"""
Today is {market_context['date']}.

CURRENT PORTFOLIO SUMMARY
Portfolio value: ${market_context['account']['portfolio_value']:,.2f}
Cash: ${market_context['account']['cash']:,.2f} ({market_context['cash_pct']:.1f}%)
Daily P&L: ${market_context['account']['daily_pnl']:,.2f} ({market_context['account']['daily_pnl_pct']:.2f}%)
Open positions: {market_context['position_count']}

CURRENT POSITIONS
{json.dumps(market_context['positions'], indent=2)}

PENDING ORDERS (submitted but not yet filled)
{json.dumps(market_context['pending_orders'], indent=2)}

WATCHLIST DATA
{json.dumps(market_context['watchlist'], indent=2)}

MARKET NEWS (past 24 hours)
{json.dumps(market_context['market_news'], indent=2)}

YOUR CURRENT THESIS DOCUMENT
{json.dumps(thesis, indent=2)}

YOUR CURRENT WATCHLIST
{json.dumps(watchlist, indent=2)}

Based on all of the above, reason through your portfolio and respond
with your structured JSON output. Remember: output only valid JSON,
no preamble, no markdown fences.
"""

    print("Sending context to Claude...")

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}]
    )

    raw = message.content[0].text.strip()

    # Strip markdown fences if present
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"^```\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Could not parse Claude response as JSON: {e}")
        print(f"Raw response:\n{raw[:500]}")
        raise


# ══════════════════════════════════════════════════════════════════════════════
# State updates
# ══════════════════════════════════════════════════════════════════════════════

def update_thesis(thesis: dict, thesis_updates: dict) -> dict:
    """Merge Claude's thesis updates into the thesis document."""
    if "positions" not in thesis:
        thesis["positions"] = {}

    for ticker, update_text in thesis_updates.items():
        thesis["positions"][ticker] = {
            "thesis": update_text,
            "last_updated": datetime.now().strftime("%Y-%m-%d")
        }

    thesis["last_run"] = datetime.now().strftime("%Y-%m-%d")
    return thesis


def update_watchlist(watchlist: dict, tickers: list) -> dict:
    """Replace watchlist tickers with Claude's updated list."""
    watchlist["tickers"] = tickers
    watchlist["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    return watchlist


# ══════════════════════════════════════════════════════════════════════════════
# Email
# ══════════════════════════════════════════════════════════════════════════════

def send_email(subject: str, body: str, trade_results: list[dict],
               account: dict):
    """
    Send Marcus's daily email.
    Appends a trade execution summary below the letter body.
    """
    # Build trade execution summary
    if trade_results:
        trade_summary = "\n\n---\nTRADE EXECUTION LOG\n"
        for t in trade_results:
            if t["success"]:
                action = t.get("action") or t.get("side", "?")
                trade_summary += (
                    f"  ✓ {action.upper()} {t['ticker']}"
                    f" — Order submitted\n"
                )
            else:
                trade_summary += (
                    f"  ✗ {t['ticker']} FAILED: {t.get('error', 'unknown')}\n"
                )
    else:
        trade_summary = "\n\n---\nNo trades executed today."

    # Append portfolio snapshot
    portfolio_snapshot = (
        f"\n\n---\nPORTFOLIO SNAPSHOT\n"
        f"Total value:  ${account['portfolio_value']:>12,.2f}\n"
        f"Cash:         ${account['cash']:>12,.2f}\n"
        f"Daily P&L:    ${account['daily_pnl']:>+12,.2f} "
        f"({account['daily_pnl_pct']:+.2f}%)\n"
        f"Run time:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )

    full_body = body + trade_summary + portfolio_snapshot

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Marcus Webb <{GMAIL_ADDRESS}>"
    recipients = [RECIPIENT_EMAIL, "mwod12@gmail.com", "cdb120@gmail.com"]
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(full_body, "plain"))

    print(f"Sending email: {subject}")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, recipients, msg.as_string())

    print("Email sent.")


# ══════════════════════════════════════════════════════════════════════════════
# Validation
# ══════════════════════════════════════════════════════════════════════════════

def validate_env():
    """Check all required environment variables are set."""
    required = [
        "ANTHROPIC_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
        "FINNHUB_API_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD",
        "RECIPIENT_EMAIL"
    ]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        raise EnvironmentError(
            f"Missing environment variables: {', '.join(missing)}\n"
            f"Add them to ~/.bashrc and run: source ~/.bashrc"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print(f"\n{'='*60}")
    print(f"Marcus Webb Agent — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    # Validate environment
    validate_env()

    # Skip weekends and holidays
    if not is_trading_day():
        print("Not a trading day. Exiting.")
        return

    # Load state files
    thesis = load_json(THESIS_FILE)
    watchlist = load_json(WATCHLIST_FILE)
    current_watchlist_tickers = watchlist.get("tickers", [])

    # Fetch all market data
    print("\nFetching market data...")
    market_context = build_market_context(watchlist=current_watchlist_tickers)
    account = market_context["account"]

    print(f"\nPortfolio value: ${account['portfolio_value']:,.2f}")
    print(f"Daily P&L: ${account['daily_pnl']:+,.2f} ({account['daily_pnl_pct']:+.2f}%)")
    print(f"Positions: {market_context['position_count']}")

    # Run Claude
    print("\nRunning Marcus...")
    response = run_marcus(market_context, thesis, watchlist)

    # Execute trades
    trades = response.get("trades", [])
    trade_results = []
    if trades:
        print(f"\nExecuting {len(trades)} trade(s)...")
        trade_results = execute_trades(trades, account)
        for r in trade_results:
            status = "OK" if r["success"] else "FAILED"
            print(f"  [{status}] {r.get('action', '?').upper()} {r['ticker']}")
    else:
        print("\nNo trades today.")

    # Update state
    updated_thesis = update_thesis(thesis, response.get("thesis_updates", {}))
    updated_watchlist = update_watchlist(watchlist, response.get("watchlist", []))

    save_json(THESIS_FILE, updated_thesis)
    save_json(WATCHLIST_FILE, updated_watchlist)
    print("State files updated.")

    # Send email
    send_email(
        subject=response["email_subject"],
        body=response["email_body"],
        trade_results=trade_results,
        account=account
    )
    # Push public summary for portfolio website
    import subprocess
    try:
        summary = {
            "portfolio_value": account["portfolio_value"],
            "cash": account["cash"],
            "daily_pnl": account["daily_pnl"],
            "daily_pnl_pct": account["daily_pnl_pct"],
            "starting_capital": 100000,
            "total_return_pct": round(
                ((account["portfolio_value"] - 100000) / 100000) * 100, 2
            ),
            "position_count": market_context["position_count"],
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "positions": [
                {
                    "ticker": p["ticker"],
                    "weight_pct": round(p["weight_pct"], 1),
                    "unrealized_pnl_pct": round(p["unrealized_pnl_pct"], 2),
                }
                for p in market_context["positions"]
            ],
            "todays_trades": [
                {
                    "ticker": t["ticker"],
                    "action": t.get("action", t.get("side", "?")),
                    "success": t["success"],
                }
                for t in trade_results
            ],
        }
        summary_path = BASE_DIR / "public_summary.json"
        save_json(summary_path, summary)
        subprocess.run(
            ["git", "-C", str(BASE_DIR), "add", "public_summary.json"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(BASE_DIR), "commit", "-m",
             f"update {summary['last_updated']}"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(BASE_DIR), "push"],
            check=True,
        )
        print("Public summary pushed to GitHub.")
    except Exception as e:
        print(f"WARNING: Failed to push summary: {e}")
    print(f"\nDone. Marcus has spoken.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
