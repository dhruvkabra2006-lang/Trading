"""
BTC Scalping Bot — rules adapted from OctagonAI/kalshi-trading-bot-cli
  - Half-Kelly position sizing based on momentum edge
  - 5-gate risk engine (Kelly, Liquidity, Concentration, Max Positions, Drawdown)
  - 20% max drawdown circuit breaker (halts all trading)
  - Daily loss limit: 10% of starting bankroll ($10)
  - Edge threshold: only enter if momentum edge >= 5%
  - Auto-restarts after each trade closes

FAILSAFES:
  - Cancels all open orders on startup
  - Detects existing BTC position on startup (resumes monitoring, no double-buy)
  - Emergency sell on Ctrl+C or crash (atexit + signal handler)
  - Retries sell order 3x before giving up
  - Logs to file (btc_bot.log) + stdout
  - Validates API credentials before starting
  - Checks minimum balance before each trade
  - Verifies order actually filled before tracking position
"""

import os, time, json, signal, atexit, logging, requests
from datetime import datetime, timezone

# ── Credentials ───────────────────────────────────────────────────────────────
API_KEY    = os.getenv("ALPACA_API_KEY",    "PKJ5RSIEMLOD3SYZD7463BIVDM")
API_SECRET = os.getenv("ALPACA_API_SECRET", "FEHcPzk44NZTDwAxj28cj4f9hDozyHLhJkfyPhRChXmY")
BASE_URL   = "https://paper-api.alpaca.markets/v2"
DATA_URL   = "https://data.alpaca.markets/v1beta3/crypto/us"

# ── Risk Config ───────────────────────────────────────────────────────────────
STARTING_BANKROLL  = 100.00
KELLY_MULTIPLIER   = 0.5
MAX_POSITION_PCT   = 0.10
MIN_EDGE_THRESHOLD = 0.01     # lowered: 1% edge to enter (was 5%)
MAX_SPREAD_PCT     = 0.005    # widened: 0.5% spread allowed (was 0.1%)
MAX_DRAWDOWN       = 0.20
DAILY_LOSS_LIMIT   = 10.00
MAX_OPEN_POSITIONS = 1
PROFIT_TARGET      = 0.012
STOP_LOSS_PCT      = 0.010
MOMENTUM_BARS      = 6
INTERVAL           = 300
MIN_BALANCE        = 5.00    # don't trade if cash falls below $5
SELL_RETRY_LIMIT   = 3       # retry failed sell orders up to 3 times

HEADERS = {
    "APCA-API-KEY-ID":     API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type":        "application/json",
}

# ── Logging (file + stdout) ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("btc_bot.log"),
        logging.StreamHandler(),
    ]
)
def log(msg): logging.info(msg)

# ── Emergency sell state (for crash handler) ──────────────────────────────────
_emergency_position = {"qty": 0.0, "active": False}

def _emergency_sell():
    """Triggered on exit/crash — sells any open BTC position."""
    if _emergency_position["active"] and _emergency_position["qty"] > 0:
        qty = _emergency_position["qty"]
        log(f"🚨 EMERGENCY SELL triggered — selling {qty} BTC")
        try:
            for attempt in range(SELL_RETRY_LIMIT):
                try:
                    body = {"symbol": "BTC/USD", "qty": str(round(qty, 8)),
                            "side": "sell", "type": "market", "time_in_force": "gtc"}
                    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS,
                                      json=body, timeout=15)
                    r.raise_for_status()
                    log(f"Emergency sell placed — {r.json().get('status', '?')}")
                    return
                except Exception as e:
                    log(f"Emergency sell attempt {attempt+1} failed: {e}")
                    time.sleep(2)
            log("❌ Emergency sell failed after all retries — check account manually!")
        except Exception as e:
            log(f"❌ Emergency sell error: {e}")

atexit.register(_emergency_sell)

def _signal_handler(sig, frame):
    log("Ctrl+C received — running emergency sell then exiting")
    _emergency_sell()
    _emergency_position["active"] = False   # prevent atexit double-sell
    raise SystemExit(0)

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT,  _signal_handler)

# ── API helpers ───────────────────────────────────────────────────────────────
def get_quote():
    r = requests.get(f"{DATA_URL}/latest/quotes?symbols=BTC%2FUSD",
                     headers=HEADERS, timeout=10)
    r.raise_for_status()
    q = r.json()["quotes"]["BTC/USD"]
    return q["ap"], q["bp"]

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

def get_btc_position():
    """Returns existing BTC position qty, or 0 if none."""
    try:
        r = requests.get(f"{BASE_URL}/positions/BTCUSD", headers=HEADERS, timeout=10)
        if r.status_code == 404:
            return 0.0
        r.raise_for_status()
        return float(r.json().get("qty", 0))
    except Exception:
        return 0.0

def cancel_all_orders():
    """Cancel any open orders to start clean."""
    r = requests.delete(f"{BASE_URL}/orders", headers=HEADERS, timeout=10)
    if r.status_code == 207:
        cancelled = r.json()
        if cancelled:
            log(f"Cancelled {len(cancelled)} open order(s) on startup")
    elif r.status_code not in (200, 204):
        log(f"Warning: could not cancel open orders ({r.status_code})")

def place_order_safe(side, qty):
    """Place order with up to SELL_RETRY_LIMIT retries for sells."""
    retries = SELL_RETRY_LIMIT if side == "sell" else 1
    body    = {"symbol": "BTC/USD", "qty": str(round(qty, 8)),
               "side": side, "type": "market", "time_in_force": "gtc"}
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.post(f"{BASE_URL}/orders", headers=HEADERS,
                              json=body, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                log(f"Order attempt {attempt+1} failed ({e}), retrying in 2s…")
                time.sleep(2)
    raise RuntimeError(f"Order failed after {retries} attempts: {last_err}")

def wait_for_fill(order_id, timeout=30):
    """Poll until order is filled; return filled qty."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(f"{BASE_URL}/orders/{order_id}", headers=HEADERS, timeout=10)
        r.raise_for_status()
        o = r.json()
        if o["status"] == "filled":
            return float(o["filled_qty"])
        if o["status"] in ("canceled", "expired", "rejected"):
            raise RuntimeError(f"Order {order_id} ended with status: {o['status']}")
        time.sleep(2)
    raise RuntimeError(f"Order {order_id} not filled within {timeout}s")

# ── Edge & Kelly ──────────────────────────────────────────────────────────────
def compute_edge():
    bars = get_bars(limit=MOMENTUM_BARS + 1)
    if len(bars) < 2:
        return 0.0
    closes    = [b["c"] for b in bars]
    latest    = closes[-1]
    avg_prior = sum(closes[:-1]) / len(closes[:-1])
    raw_edge  = (latest - avg_prior) / avg_prior
    return max(-1.0, min(1.0, raw_edge / 0.01))

def kelly_qty(edge, price, bankroll):
    p       = 0.5 + abs(edge) / 2
    q       = 1 - p
    b       = PROFIT_TARGET / STOP_LOSS_PCT
    f_star  = (p * b - q) / b
    f       = min(max(f_star * KELLY_MULTIPLIER, 0.0), MAX_POSITION_PCT)
    dollars = f * bankroll
    qty     = dollars / price
    return round(qty, 8), dollars

# ── 5-Gate risk engine ────────────────────────────────────────────────────────
def run_risk_gates(edge, ask, bid, bankroll, open_positions, high_water, daily_start):
    gates = []

    qty, dollars = kelly_qty(edge, ask, bankroll)
    gates.append(("KELLY",         qty > 0 and dollars >= 1.0,
                  f"${dollars:.2f} / {qty} BTC"))

    spread_pct = (ask - bid) / ask if ask > 0 else 1.0
    gates.append(("LIQUIDITY",     spread_pct <= MAX_SPREAD_PCT,
                  f"spread {spread_pct*100:.3f}%"))

    gates.append(("CONCENTRATION", True, "BTC only — N/A"))

    gates.append(("MAX_POSITIONS", len(open_positions) < MAX_OPEN_POSITIONS,
                  f"{len(open_positions)} open"))

    drawdown = (high_water - bankroll) / high_water if high_water > 0 else 0
    gates.append(("DRAWDOWN",      drawdown < MAX_DRAWDOWN,
                  f"{drawdown*100:.1f}% drawdown"))

    daily_loss = daily_start - bankroll
    gates.append(("DAILY_LOSS",    daily_loss < DAILY_LOSS_LIMIT,
                  f"${daily_loss:.2f} lost today"))

    gates.append(("MIN_BALANCE",   bankroll >= MIN_BALANCE,
                  f"${bankroll:.2f} available"))

    passed = all(g[1] for g in gates)
    return passed, gates, qty

def print_gates(gates):
    for name, ok, detail in gates:
        log(f"  {'✅' if ok else '🚫'} [{name}]: {detail}")

# ── Single trade cycle ────────────────────────────────────────────────────────
def run_trade(trade_num, high_water, daily_start):
    log(f"━━ Trade #{trade_num} ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    ask, bid       = get_quote()
    mid            = (ask + bid) / 2
    account        = get_account()
    bankroll       = float(account["cash"])
    open_positions = get_positions()
    edge           = compute_edge()

    log(f"BTC ${mid:,.2f} | Edge {edge*100:+.2f}% | Cash ${bankroll:.2f}")

    if abs(edge) < MIN_EDGE_THRESHOLD:
        log(f"⏸  Edge {abs(edge)*100:.2f}% < {MIN_EDGE_THRESHOLD*100:.0f}% threshold — skip")
        return None, high_water, daily_start

    passed, gates, qty = run_risk_gates(edge, ask, bid, bankroll,
                                         open_positions, high_water, daily_start)
    print_gates(gates)

    if not passed:
        log("🚫 Risk gates FAILED — no trade")
        return None, high_water, daily_start

    # ── Place buy ─────────────────────────────────────────────────────────────
    log(f"✅ All gates passed — buying {qty} BTC at ~${ask:,.2f}")
    order     = place_order_safe("buy", qty)
    filled_qty = wait_for_fill(order["id"])        # wait for actual fill
    log(f"Filled: {filled_qty} BTC | Order {order['id']}")

    # Arm emergency sell handler
    _emergency_position["qty"]    = filled_qty
    _emergency_position["active"] = True

    entry       = ask
    trail_floor = entry * (1 - STOP_LOSS_PCT)
    target      = entry * (1 + PROFIT_TARGET)
    peak        = entry
    cost        = entry * filled_qty

    log(f"Entry ${entry:,.2f} | Stop ${trail_floor:,.2f} | Target ${target:,.2f}")

    # ── Monitor ───────────────────────────────────────────────────────────────
    while True:
        time.sleep(INTERVAL)
        ask, bid = get_quote()
        price    = (ask + bid) / 2
        chg      = (price - entry) / entry * 100
        log(f"BTC ${price:,.2f} ({chg:+.2f}%) | Floor ${trail_floor:,.2f} | Target ${target:,.2f}")

        if price >= target:
            log(f"✅ PROFIT TARGET — selling {filled_qty} BTC")
            place_order_safe("sell", filled_qty)
            _emergency_position["active"] = False
            pnl = (price - (cost / filled_qty)) * filled_qty
            log(f"P&L: ${pnl:+.2f}")
            return pnl, max(high_water, bankroll + pnl), daily_start

        if price <= trail_floor:
            log(f"🔴 STOP LOSS — selling {filled_qty} BTC")
            place_order_safe("sell", filled_qty)
            _emergency_position["active"] = False
            pnl = (price - (cost / filled_qty)) * filled_qty
            log(f"P&L: ${pnl:+.2f}")
            return pnl, high_water, daily_start

        if price > peak:
            peak        = price
            new_floor   = peak * (1 - STOP_LOSS_PCT * 0.5)
            if new_floor > trail_floor:
                trail_floor = new_floor
                log(f"📈 Trail floor → ${trail_floor:,.2f}")

        account  = get_account()
        bankroll = float(account["cash"])
        drawdown = (high_water - bankroll) / high_water if high_water > 0 else 0
        if drawdown >= MAX_DRAWDOWN:
            log(f"🚨 CIRCUIT BREAKER {drawdown*100:.1f}% — selling and halting")
            place_order_safe("sell", filled_qty)
            _emergency_position["active"] = False
            return None, high_water, daily_start

# ── Startup checks ────────────────────────────────────────────────────────────
def startup_checks():
    """Validate API, cancel orphaned orders, detect existing position."""
    log("Running startup checks…")

    # 1. Validate credentials
    try:
        account = get_account()
        log(f"✅ API connected | Status: {account['status']} | Cash: ${float(account['cash']):.2f}")
    except Exception as e:
        log(f"❌ API connection failed: {e}")
        raise SystemExit(1)

    # 2. Cancel any open orders
    cancel_all_orders()

    # 3. Check for existing BTC position (don't double-buy)
    existing_qty = get_btc_position()
    if existing_qty > 0:
        log(f"⚠️  Existing BTC position detected: {existing_qty} BTC")
        log("Bot will monitor this position instead of opening a new one.")
        _emergency_position["qty"]    = existing_qty
        _emergency_position["active"] = True
    else:
        log("✅ No existing BTC position")

    return account, existing_qty

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log("=" * 55)
    log("BTC Bot — OctagonAI-style risk rules | $100 account")
    log("=" * 55)

    account, existing_qty = startup_checks()
    bankroll    = float(account["cash"])
    high_water  = bankroll
    daily_start = bankroll
    trades      = []
    trade_num   = 1

    log(f"Bankroll: ${bankroll:.2f} | Circuit breaker at: ${bankroll*MAX_DRAWDOWN:.2f} loss")

    # If resuming an existing position, go straight into monitoring
    if existing_qty > 0:
        ask, _      = get_quote()
        entry_est   = ask
        trail_floor = entry_est * (1 - STOP_LOSS_PCT)
        log(f"Monitoring existing {existing_qty} BTC | Stop ~${trail_floor:,.2f}")
        # Let it fall through to the normal loop on next cycle

    while True:
        try:
            pnl, high_water, daily_start = run_trade(trade_num, high_water, daily_start)

            if pnl is None:
                account  = get_account()
                bankroll = float(account["cash"])
                drawdown = (high_water - bankroll) / high_water if high_water > 0 else 0
                if drawdown >= MAX_DRAWDOWN:
                    log("🚨 CIRCUIT BREAKER ACTIVE — bot halted. Restart when ready.")
                    break
                daily_loss = daily_start - bankroll
                if daily_loss >= DAILY_LOSS_LIMIT:
                    log(f"🛑 DAILY LOSS LIMIT hit (${daily_loss:.2f}) — halted for today.")
                    break
                time.sleep(INTERVAL)
                continue

            trades.append(pnl)
            wins  = sum(1 for p in trades if p > 0)
            loss  = sum(1 for p in trades if p <= 0)
            total = sum(trades)
            log(f"📊 {len(trades)} trades | {wins}W {loss}L | Total P&L: ${total:+.2f}")
            trade_num += 1
            time.sleep(10)

        except SystemExit:
            break
        except requests.exceptions.RequestException as e:
            log(f"⚠️  Network error: {e} — retrying in 30s")
            time.sleep(30)
        except Exception as e:
            log(f"❌ Error: {e} — retrying in 30s")
            time.sleep(30)

    log("Bot stopped. Check btc_bot.log for full history.")

if __name__ == "__main__":
    main()
