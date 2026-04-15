"""
BTC Scalping Bot — rules adapted from OctagonAI/kalshi-trading-bot-cli
  - Half-Kelly position sizing based on momentum edge
  - 5-gate risk engine (Kelly, Liquidity, Concentration, Max Positions, Drawdown)
  - 20% max drawdown circuit breaker (halts all trading)
  - Daily loss limit: 10% of starting bankroll
  - Edge threshold: only enter if |momentum edge| >= 5%
  - Auto-restarts after each trade closes
"""

import os, time, json, requests
from datetime import datetime, timezone

# ── Credentials ───────────────────────────────────────────────────────────────
API_KEY    = os.getenv("ALPACA_API_KEY",    "PKGJDEV2I4ZGMQSITU6YNXM3MB")
API_SECRET = os.getenv("ALPACA_API_SECRET", "GtbTTT7yozTz32YKtiPqRiEVEf7pJDE7P5zJ1dYoaofQ")
BASE_URL   = "https://paper-api.alpaca.markets/v2"
DATA_URL   = "https://data.alpaca.markets/v1beta3/crypto/us"

# ── Risk Config (Kalshi-style) ─────────────────────────────────────────────────
STARTING_BANKROLL    = 100.00   # $100 account
KELLY_MULTIPLIER     = 0.5      # Half-Kelly (conservative)
MAX_POSITION_PCT     = 0.10     # Max 10% of bankroll per trade
MIN_EDGE_THRESHOLD   = 0.05     # 5% minimum momentum edge to enter
MAX_SPREAD_PCT       = 0.001    # Max 0.1% bid-ask spread (liquidity gate)
MAX_DRAWDOWN         = 0.20     # 20% drawdown → circuit breaker halts trading
DAILY_LOSS_LIMIT     = 10.00    # $10/day max loss
MAX_OPEN_POSITIONS   = 1        # Only 1 BTC position at a time
PROFIT_TARGET        = 0.012    # +1.2% take profit
STOP_LOSS_PCT        = 0.010    # -1.0% stop loss
MOMENTUM_BARS        = 6        # Number of 5-min bars to measure edge
INTERVAL             = 300      # 5 minutes between checks

HEADERS = {
    "APCA-API-KEY-ID":     API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type":        "application/json",
}

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ── API helpers ───────────────────────────────────────────────────────────────
def get_quote():
    r = requests.get(f"{DATA_URL}/latest/quotes?symbols=BTC%2FUSD", headers=HEADERS, timeout=10)
    r.raise_for_status()
    q = r.json()["quotes"]["BTC/USD"]
    return q["ap"], q["bp"]   # ask, bid

def get_bars(limit=10):
    r = requests.get(
        f"{DATA_URL}/bars?symbols=BTC%2FUSD&timeframe=5Min&limit={limit}",
        headers=HEADERS, timeout=10
    )
    r.raise_for_status()
    return r.json()["bars"]["BTC/USD"]

def get_account():
    r = requests.get(f"{BASE_URL}/account", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()

def get_positions():
    r = requests.get(f"{BASE_URL}/positions", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()

def place_order(side, qty):
    body = {"symbol": "BTC/USD", "qty": str(round(qty, 8)),
            "side": side, "type": "market", "time_in_force": "gtc"}
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=body, timeout=10)
    r.raise_for_status()
    return r.json()

# ── Edge & Kelly ──────────────────────────────────────────────────────────────
def compute_edge():
    """
    Momentum edge: compare latest close to average of prior bars.
    Returns a float in (-1, 1). Positive = bullish, negative = bearish.
    """
    bars = get_bars(limit=MOMENTUM_BARS + 1)
    if len(bars) < 2:
        return 0.0
    closes     = [b["c"] for b in bars]
    latest     = closes[-1]
    avg_prior  = sum(closes[:-1]) / len(closes[:-1])
    raw_edge   = (latest - avg_prior) / avg_prior   # e.g. +0.003 = +0.3% above avg
    # Normalise: treat 1% move as ~full edge signal
    edge = max(-1.0, min(1.0, raw_edge / 0.01))
    return edge

def kelly_qty(edge, price, bankroll):
    """
    Half-Kelly sizing.
    For a symmetric bet (equal profit target & stop loss, b ≈ 1):
      win prob p = 0.5 + edge/2   (edge mapped from [-1,1] to a prob adjustment)
      Kelly f* = p - (1-p) = 2p - 1 = edge
    Apply Half-Kelly multiplier and cap at MAX_POSITION_PCT.
    """
    p       = 0.5 + abs(edge) / 2          # implied win probability
    q       = 1 - p
    b       = PROFIT_TARGET / STOP_LOSS_PCT # reward-to-risk ratio
    f_star  = (p * b - q) / b              # raw Kelly fraction
    f       = f_star * KELLY_MULTIPLIER    # Half-Kelly
    f       = min(f, MAX_POSITION_PCT)     # cap at 10% of bankroll
    f       = max(f, 0.0)
    dollars = f * bankroll
    qty     = dollars / price
    return round(qty, 8), dollars

# ── Risk gates (Kalshi 5-gate system) ─────────────────────────────────────────
def run_risk_gates(edge, ask, bid, bankroll, open_positions, high_water, daily_start):
    gates = []

    # Gate 1 — Kelly sizing
    qty, dollars = kelly_qty(edge, ask, bankroll)
    if qty <= 0 or dollars < 1.0:
        gates.append(("KELLY",        False, f"position too small (${dollars:.2f})"))
    else:
        gates.append(("KELLY",        True,  f"${dollars:.2f} / {qty} BTC"))

    # Gate 2 — Liquidity (spread)
    spread_pct = (ask - bid) / ask if ask > 0 else 1.0
    if spread_pct > MAX_SPREAD_PCT:
        gates.append(("LIQUIDITY",    False, f"spread {spread_pct*100:.3f}% > {MAX_SPREAD_PCT*100:.2f}%"))
    else:
        gates.append(("LIQUIDITY",    True,  f"spread {spread_pct*100:.3f}%"))

    # Gate 3 — Concentration (single asset, always pass)
    gates.append(("CONCENTRATION",    True,  "BTC only — N/A"))

    # Gate 4 — Max open positions
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        gates.append(("MAX_POSITIONS", False, f"{len(open_positions)} positions open"))
    else:
        gates.append(("MAX_POSITIONS", True,  f"{len(open_positions)} open"))

    # Gate 5 — Drawdown circuit breaker
    drawdown = (high_water - bankroll) / high_water if high_water > 0 else 0
    if drawdown >= MAX_DRAWDOWN:
        gates.append(("DRAWDOWN",      False, f"{drawdown*100:.1f}% drawdown — CIRCUIT BREAKER"))
    else:
        gates.append(("DRAWDOWN",      True,  f"{drawdown*100:.1f}% drawdown"))

    # Daily loss limit check
    daily_loss = daily_start - bankroll
    if daily_loss >= DAILY_LOSS_LIMIT:
        gates.append(("DAILY_LOSS",    False, f"${daily_loss:.2f} lost today (limit ${DAILY_LOSS_LIMIT})"))
    else:
        gates.append(("DAILY_LOSS",    True,  f"${daily_loss:.2f} lost today"))

    passed = all(g[1] for g in gates)
    return passed, gates, qty

def print_gates(gates):
    for name, ok, detail in gates:
        icon = "✅" if ok else "🚫"
        log(f"  {icon} Gate [{name}]: {detail}")

# ── Single trade cycle ─────────────────────────────────────────────────────────
def run_trade(trade_num, high_water, daily_start):
    log(f"━━ Trade #{trade_num} ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    ask, bid       = get_quote()
    mid            = (ask + bid) / 2
    account        = get_account()
    bankroll       = float(account["cash"])
    open_positions = get_positions()
    edge           = compute_edge()
    direction      = "BUY" if edge > 0 else "SELL/SKIP"

    log(f"BTC ${mid:,.2f}  |  Edge: {edge*100:+.2f}%  ({direction})  |  Cash: ${bankroll:.2f}")

    # ── Edge threshold ────────────────────────────────────────────────────────
    if abs(edge) < MIN_EDGE_THRESHOLD:
        log(f"⏸  Edge {abs(edge)*100:.2f}% below minimum {MIN_EDGE_THRESHOLD*100:.0f}% — skipping this cycle")
        return None, high_water, daily_start

    if edge < 0:
        log("⏸  Negative edge (bearish momentum) — no long entry, skipping")
        return None, high_water, daily_start

    # ── Run risk gates ────────────────────────────────────────────────────────
    passed, gates, qty = run_risk_gates(edge, ask, bid, bankroll, open_positions, high_water, daily_start)
    print_gates(gates)

    if not passed:
        log("🚫 Risk gates FAILED — no trade this cycle")
        return None, high_water, daily_start

    # ── Enter position ────────────────────────────────────────────────────────
    log(f"✅ All gates passed — buying {qty} BTC at ~${ask:,.2f}")
    order  = place_order("buy", qty)
    log(f"Order → {order['id']} | {order['status']}")

    entry       = ask
    floor       = entry * (1 - STOP_LOSS_PCT)
    target      = entry * (1 + PROFIT_TARGET)
    peak        = entry
    trail_floor = floor
    cost        = entry * qty
    total_qty   = qty

    log(f"Entry ${entry:,.2f} | Stop ${floor:,.2f} | Target ${target:,.2f}")

    # ── Monitor position ──────────────────────────────────────────────────────
    while True:
        time.sleep(INTERVAL)
        ask, bid = get_quote()
        price    = (ask + bid) / 2
        chg      = (price - entry) / entry * 100
        log(f"BTC ${price:,.2f} ({chg:+.2f}%) | Floor ${trail_floor:,.2f} | Target ${target:,.2f}")

        # Profit target
        if price >= target:
            log(f"✅ PROFIT TARGET hit — selling {total_qty} BTC")
            place_order("sell", total_qty)
            pnl = (price - (cost / total_qty)) * total_qty
            log(f"P&L: ${pnl:+.2f}")
            return pnl, max(high_water, bankroll + pnl), daily_start

        # Stop loss
        if price <= trail_floor:
            log(f"🔴 STOP LOSS hit — selling {total_qty} BTC")
            place_order("sell", total_qty)
            pnl = (price - (cost / total_qty)) * total_qty
            log(f"P&L: ${pnl:+.2f}")
            return pnl, high_water, daily_start

        # Trailing floor: move up if price rises
        if price > peak:
            peak      = price
            new_trail = peak * (1 - STOP_LOSS_PCT * 0.5)   # trail at half stop-loss distance
            if new_trail > trail_floor:
                trail_floor = new_trail
                log(f"📈 Trail floor raised to ${trail_floor:,.2f}")

        # Re-check drawdown each cycle
        account  = get_account()
        bankroll = float(account["cash"])
        drawdown = (high_water - bankroll) / high_water if high_water > 0 else 0
        if drawdown >= MAX_DRAWDOWN:
            log(f"🚨 CIRCUIT BREAKER — {drawdown*100:.1f}% drawdown. Selling and halting.")
            place_order("sell", total_qty)
            return None, high_water, daily_start   # None signals halt

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log("BTC Bot started (OctagonAI-style risk rules) — Ctrl+C to stop")
    account     = get_account()
    bankroll    = float(account["cash"])
    high_water  = bankroll
    daily_start = bankroll
    trades      = []
    trade_num   = 1

    log(f"Bankroll: ${bankroll:.2f} | Max drawdown: ${bankroll*MAX_DRAWDOWN:.2f} | Daily loss limit: ${DAILY_LOSS_LIMIT:.2f}")

    while True:
        try:
            pnl, high_water, daily_start = run_trade(trade_num, high_water, daily_start)

            if pnl is None:
                # Circuit breaker or skipped cycle
                account  = get_account()
                bankroll = float(account["cash"])
                drawdown = (high_water - bankroll) / high_water if high_water > 0 else 0
                if drawdown >= MAX_DRAWDOWN:
                    log("🚨 CIRCUIT BREAKER ACTIVE. Bot halted. Restart manually when ready.")
                    break
                log("Waiting for next cycle...")
                time.sleep(INTERVAL)
                continue

            trades.append(pnl)
            wins   = sum(1 for p in trades if p > 0)
            losses = sum(1 for p in trades if p <= 0)
            total  = sum(trades)
            log(f"📊 {len(trades)} trades | {wins}W {losses}L | Total P&L: ${total:+.2f}")
            trade_num += 1
            time.sleep(10)

        except KeyboardInterrupt:
            log("Bot stopped.")
            if trades:
                log(f"Final: {len(trades)} trades | P&L: ${sum(trades):+.2f}")
            break
        except requests.exceptions.RequestException as e:
            log(f"⚠️  Network error: {e} — retrying in 30s")
            time.sleep(30)
        except Exception as e:
            log(f"❌ Error: {e} — retrying in 30s")
            time.sleep(30)

if __name__ == "__main__":
    main()
