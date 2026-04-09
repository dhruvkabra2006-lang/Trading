import requests
import json

API_KEY = "PKAZIDZOGFGR2FFN7X3QGB5IJN"
API_SECRET = "4juc9gRTRkxH2PifbEuRmC7oYnqzKDLGQf5HnFY2H7Fn"
BASE_URL = "https://paper-api.alpaca.markets/v2"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type": "application/json",
}


def get_account():
    resp = requests.get(f"{BASE_URL}/account", headers=HEADERS)
    resp.raise_for_status()
    return resp.json()


def buy_stock(symbol, qty):
    order = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
    }
    resp = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=order)
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    account = get_account()
    print(f"Account status: {account['status']}")
    print(f"Buying power:   ${float(account['buying_power']):.2f}")

    order = buy_stock("AAPL", 1)
    print(f"\nOrder placed!")
    print(f"  ID:     {order['id']}")
    print(f"  Symbol: {order['symbol']}")
    print(f"  Qty:    {order['qty']}")
    print(f"  Side:   {order['side']}")
    print(f"  Type:   {order['order_type']}")
    print(f"  Status: {order['status']}")
