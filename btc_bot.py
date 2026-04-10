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
INITIAL_QTY   = 0.03          # BTC to buy on start
STOP_LOSS_PCT = 0.02           # 2% hard floor  (tight for 5-min cycle)
TRAIL_TRIGGER = 0.01           # start trailing after +1% gain
TRAIL_STEP    = 0.01           # move floor up every +1% gain
TRAIL_OFFSET  = 0.005          # floor sits 0.5% below peak  (very tight)
PROFIT_TARGET = 0.03           # sell everything at +3% gain
INTERVAL      = 300            # seconds between checks (5 min)

# Ladder: (% drop from entry, qty to buy)
LADDER = [
    (-0.005, 0.01),  # -0.5% → buy 0.01 BTC
    (-0.010, 0.01),  # -1.0% → buy 0.01 BTC
]

STATE_FILE = "bot_state.json"

HEADERS = {
    "APCA-API-KEY-ID":     API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type":        "application/json",
}

# ── Helpers ───────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def get_btc_price():
    r = requests.get(f"{DATA_URL}/latest/quotes?symbols=BTC%2FUSD", headers=HEADERS, timeout=10)
    r.raise_for_status()
    q = r.json()["quotes"]["BTC/USD"]
    return (q["ap"] + q["bp"]) / 2   # mid-price

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

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
            s["ladder_triggered"] = set(s.get("ladder_triggered", []))
            return s
    return None

# ── Main bot ──────────────────────────────────────────────────────────────────
def run():
    state = load_state()

    if state and state.get("active"):
        log("Resuming from saved state.")
        log(f"  Entry price : ${state['entry_price']:,.2f}")
        log(f"  Current floor: ${state['floor']:,.2f}")
        log(f"  Total BTC held: {state['total_qty']}")
    else:
        price = get_btc_price()
        log(f"BTC price: ${price:,.2f}")
        log(f"⚠️  NOTE: 0.03 BTC ≈ ${price * 0.03:,.2f}. Make sure your account has enough balance.")
        log(f"Placing initial buy: {INITIAL_QTY} BTC…")

        order = place_order("buy", INITIAL_QTY)
        log(f"Order placed → ID: {order['id']} | Status: {order['status']}")

        floor = price * (1 - STOP_LOSS_PCT)
        state = {
            "active":            True,
            "entry_price":       price,
            "floor":             floor,
            "peak_price":        price,
            "last_trail_level":  price,   # price at which we last moved the floor
            "total_qty":         INITIAL_QTY,
            "ladder_triggered":  set(),
        }
        save_state({**state, "ladder_triggered": list(state["ladder_triggered"])})

        log(f"Entry: ${price:,.2f} | Stop-loss floor: ${floor:,.2f} (10% below entry)")

    # ── Loop ──────────────────────────────────────────────────────────────────
    while state["active"]:
        time.sleep(INTERVAL)

        try:
            price = get_btc_price()
            entry = state["entry_price"]
            change_pct = (price - entry) / entry * 100
            log(f"BTC ${price:,.2f}  ({change_pct:+.2f}% from entry)  |  Floor ${state['floor']:,.2f}  |  Holding {state['total_qty']} BTC")

            # ── 1. Check profit target ────────────────────────────────────
            gain = (price - entry) / entry
            if gain >= PROFIT_TARGET:
                log(f"🟢 PROFIT TARGET +{gain*100:.2f}% hit at ${price:,.2f} — selling all {state['total_qty']} BTC")
                order = place_order("sell", state["total_qty"])
                log(f"Sell order → ID: {order['id']} | Status: {order['status']}")
                state["active"] = False
                save_state({**state, "ladder_triggered": list(state["ladder_triggered"])})
                log("Profit target reached. Bot stopped.")
                break

            # ── 2. Check hard floor ────────────────────────────────────────
            if price <= state["floor"]:
                log(f"🔴 FLOOR HIT at ${price:,.2f} — selling all {state['total_qty']} BTC")
                order = place_order("sell", state["total_qty"])
                log(f"Sell order → ID: {order['id']} | Status: {order['status']}")
                state["active"] = False
                save_state({**state, "ladder_triggered": list(state["ladder_triggered"])})
                log("Strategy complete. Bot stopped.")
                break

            # ── 3. Update trailing floor ───────────────────────────────────
            if price > state["peak_price"]:
                state["peak_price"] = price
                gain_from_entry = (price - entry) / entry

                if gain_from_entry >= TRAIL_TRIGGER:
                    # Move floor up every TRAIL_STEP above the last trail level
                    gain_from_last = (price - state["last_trail_level"]) / state["last_trail_level"]
                    if gain_from_last >= TRAIL_STEP or state["last_trail_level"] == entry:
                        new_floor = price * (1 - TRAIL_OFFSET)
                        if new_floor > state["floor"]:
                            state["floor"] = new_floor
                            state["last_trail_level"] = price
                            log(f"📈 Trailing floor raised to ${new_floor:,.2f} (price at ${price:,.2f})")

            # ── 4. Ladder in on dips ───────────────────────────────────────
            for (level, qty) in LADDER:
                if level not in state["ladder_triggered"]:
                    drop = (price - entry) / entry
                    if drop <= level:
                        log(f"📉 Ladder in at {level*100:.0f}% drop — buying {qty} BTC at ${price:,.2f}")
                        order = place_order("buy", qty)
                        log(f"Buy order → ID: {order['id']} | Status: {order['status']}")
                        state["total_qty"] = round(state["total_qty"] + qty, 8)
                        state["ladder_triggered"].add(level)
                        log(f"Total BTC held: {state['total_qty']}")

            save_state({**state, "ladder_triggered": list(state["ladder_triggered"])})

        except requests.exceptions.RequestException as e:
            log(f"⚠️  Network error: {e} — will retry next interval")
        except Exception as e:
            log(f"❌ Unexpected error: {e}")


if __name__ == "__main__":
    run()
