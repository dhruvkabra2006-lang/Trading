import os
import time
import json
import requests
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY    = os.getenv("ALPACA_API_KEY",    "PKAZIDZOGFGR2FFN7X3QGB5IJN")
API_SECRET = os.getenv("ALPACA_API_SECRET", "4juc9gRTRkxH2PifbEuRmC7oYnqzKDLGQf5HnFY2H7Fn")
BASE_URL   = "https://paper-api.alpaca.markets/v2"
DATA_URL   = "https://data.alpaca.markets/v1beta3/crypto/us"

SYMBOL        = "BTC/USD"
INITIAL_QTY   = 0.03
PROFIT_TARGET = 0.010   # +1.0% → sell, take profit
STOP_LOSS_PCT = 0.010   # -1.0% → sell, cut loss
TRAIL_TRIGGER = 0.005   # +0.5% → start trailing
TRAIL_OFFSET  = 0.003   # trail floor sits 0.3% below peak
LADDER        = [
    (-0.003, 0.01),     # -0.3% → buy 0.01 more BTC
    (-0.006, 0.01),     # -0.6% → buy 0.01 more BTC
]
INTERVAL = 300          # 5 minutes between checks

HEADERS = {
    "APCA-API-KEY-ID":     API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type":        "application/json",
}

# ── Helpers ───────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def get_btc_price():
    r = requests.get(
        f"{DATA_URL}/latest/quotes?symbols=BTC%2FUSD",
        headers=HEADERS, timeout=10
    )
    r.raise_for_status()
    q = r.json()["quotes"]["BTC/USD"]
    return (q["ap"] + q["bp"]) / 2

def place_order(side, qty):
    body = {
        "symbol":        SYMBOL,
        "qty":           str(round(qty, 8)),
        "side":          side,
        "type":          "market",
        "time_in_force": "gtc",
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=body, timeout=10)
    r.raise_for_status()
    return r.json()

def get_pnl_summary(trades):
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total  = sum(t["pnl"] for t in trades)
    return f"{len(trades)} trades | {len(wins)}W {len(losses)}L | Total P&L: ${total:+.2f}"

# ── Single trade cycle ────────────────────────────────────────────────────────
def run_trade(trade_num):
    price = get_btc_price()
    log(f"━━ Trade #{trade_num} ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log(f"Buying {INITIAL_QTY} BTC at ${price:,.2f}")

    order = place_order("buy", INITIAL_QTY)
    log(f"Order → {order['id']} | {order['status']}")

    entry      = price
    floor      = price * (1 - STOP_LOSS_PCT)
    peak       = price
    trail_on   = False
    total_qty  = INITIAL_QTY
    ladder_hit = set()
    cost_basis = price * INITIAL_QTY

    log(f"Entry ${entry:,.2f} | Stop ${floor:,.2f} | Target ${entry*(1+PROFIT_TARGET):,.2f}")

    while True:
        time.sleep(INTERVAL)
        price = get_btc_price()
        chg   = (price - entry) / entry * 100
        log(f"BTC ${price:,.2f} ({chg:+.2f}%) | Floor ${floor:,.2f} | Qty {total_qty}")

        # ── Profit target ──────────────────────────────────────────────────
        if price >= entry * (1 + PROFIT_TARGET):
            log(f"✅ PROFIT TARGET hit — selling {total_qty} BTC")
            place_order("sell", total_qty)
            pnl = (price - (cost_basis / total_qty)) * total_qty
            log(f"P&L this trade: ${pnl:+.2f}")
            return pnl

        # ── Hard floor ────────────────────────────────────────────────────
        if price <= floor:
            log(f"🔴 STOP LOSS hit — selling {total_qty} BTC")
            place_order("sell", total_qty)
            pnl = (price - (cost_basis / total_qty)) * total_qty
            log(f"P&L this trade: ${pnl:+.2f}")
            return pnl

        # ── Trailing floor ────────────────────────────────────────────────
        if price > peak:
            peak = price
            gain = (peak - entry) / entry
            if gain >= TRAIL_TRIGGER:
                trail_on = True
            if trail_on:
                new_floor = peak * (1 - TRAIL_OFFSET)
                if new_floor > floor:
                    floor = new_floor
                    log(f"📈 Trail floor → ${floor:,.2f}")

        # ── Ladder in ─────────────────────────────────────────────────────
        for (level, qty) in LADDER:
            if level not in ladder_hit:
                if (price - entry) / entry <= level:
                    log(f"📉 Ladder at {level*100:.1f}% — buying {qty} BTC")
                    place_order("buy", qty)
                    cost_basis += price * qty
                    total_qty   = round(total_qty + qty, 8)
                    ladder_hit.add(level)
                    log(f"Avg cost: ${cost_basis/total_qty:,.2f} | Total qty: {total_qty}")

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    log("BTC Scalping Bot started — press Ctrl+C to stop")
    trades     = []
    trade_num  = 1

    while True:
        try:
            pnl = run_trade(trade_num)
            trades.append({"num": trade_num, "pnl": pnl})
            log(get_pnl_summary(trades))
            trade_num += 1
            log("Restarting in 10 seconds...")
            time.sleep(10)

        except KeyboardInterrupt:
            log("Bot stopped by user.")
            if trades:
                log(get_pnl_summary(trades))
            break
        except requests.exceptions.RequestException as e:
            log(f"⚠️  Network error: {e} — retrying in 30s")
            time.sleep(30)
        except Exception as e:
            log(f"❌ Error: {e} — retrying in 30s")
            time.sleep(30)

if __name__ == "__main__":
    main()
