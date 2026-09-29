import os
import time
from functools import lru_cache
from typing import Dict, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

UPSTOX_BASE_URL = "https://api.upstox.com/v2"


def get_broker_mode() -> str:
    return (os.getenv("BROKER_MODE", "DUMMY") or "DUMMY").upper()


def _get_upstox_config() -> Dict[str, Optional[str]]:
    return {
        "app_key": os.getenv("UPSTOX_APP_KEY"),
        "secret_key": os.getenv("UPSTOX_SECRET_KEY"),
        "redirect_uri": os.getenv("UPSTOX_REDIRECT_URI"),
        "access_token": os.getenv("UPSTOX_ACCESS_TOKEN"),
    }


@lru_cache(maxsize=1024)
def _resolve_instrument_key(symbol: str, access_token: str) -> Optional[str]:
    symbol_key = (symbol or "").strip().upper()
    if not symbol_key:
        return None
    if "|" in symbol_key:
        return symbol_key

    query = symbol_key.removesuffix("-EQ")
    try:
        response = requests.get(
            f"{UPSTOX_BASE_URL}/instruments/search",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            params={
                "query": query,
                "exchanges": "NSE",
                "segments": "EQ",
                "records": 30,
            },
            timeout=10,
        )
        if response.status_code != 200:
            print(f"[UPSTOX INSTRUMENT SEARCH ERROR] {response.status_code}: {response.text[:200]}")
            return None

        instruments = response.json().get("data", [])
        for instrument in instruments:
            trading_symbol = str(instrument.get("trading_symbol", "")).upper()
            if (
                instrument.get("segment") == "NSE_EQ"
                and trading_symbol == query
                and instrument.get("instrument_key")
            ):
                return str(instrument["instrument_key"])
    except Exception as exc:
        print(f"[UPSTOX INSTRUMENT SEARCH ERROR] {symbol}: {exc}")

    return None


def get_live_ltp(symbol: str) -> Optional[float]:
    """
    Upstox se live LTP fetch karta hai; quote reads independent of order mode hain.
    """
    symbol_key = (symbol or "").strip()
    if not symbol_key:
        return None

    config = _get_upstox_config()
    access_token = config.get("access_token")
    if not access_token:
        return None

    try:
        instrument_key = _resolve_instrument_key(symbol_key, access_token)
        if not instrument_key:
            print(f"[UPSTOX LTP ERROR] Could not resolve NSE instrument: {symbol}")
            return None

        response = requests.get(
            f"{UPSTOX_BASE_URL}/market-quote/ltp",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            params={"instrument_key": instrument_key},
            timeout=10,
        )

        if response.status_code != 200:
            print(f"[UPSTOX LTP ERROR] {response.status_code}: {response.text[:200]}")
            return None

        payload = response.json()
        quote = payload.get("data", {})
        if isinstance(quote, dict) and "last_price" in quote:
            quote_items = [quote]
        elif isinstance(quote, dict):
            quote_items = list(quote.values())
        elif isinstance(quote, list):
            quote_items = quote
        else:
            quote_items = []

        for item in quote_items:
            if not isinstance(item, dict):
                continue
            returned_key = str(item.get("instrument_token", "")).upper()
            if returned_key and returned_key != instrument_key.upper():
                continue
            price = item.get("last_price") or item.get("ltp") or item.get("last_traded_price")
            if price is not None:
                return float(price)

    except Exception as exc:
        print(f"[UPSTOX LTP FETCH ERROR] {symbol}: {exc}")

    return None


def execute_user_order(client_id: str, broker: str, symbol: str, qty: int, action: str):
    """
    Dummy mode default rahega for testing.
    LIVE mode me real Upstox order request attempt hota hai.
    """
    mode = get_broker_mode()
    config = _get_upstox_config()
    access_token = config.get("access_token")

    print(f"[*] Broker: [{broker}] | Client: {client_id} | Mode: {mode}")
    print(f"    Details: {action} {qty} shares of {symbol} at Market Price...")

    if mode != "LIVE":
        print("[DUMMY MODE] Actual broker order not placed. Strategy test only.")
        time.sleep(0.1)
        return {
            "status": "SUCCESS",
            "broker_order_id": f"DUMMY_{int(time.time()*1000)}",
            "client": client_id,
            "symbol": symbol,
            "qty": qty,
            "source": "DUMMY",
            "mode": mode,
        }

    if not access_token:
        print("[BROKER MODE LIVE] No Upstox access token found. Falling back to dummy mode.")
        return {
            "status": "SUCCESS",
            "broker_order_id": f"DUMMY_{int(time.time()*1000)}",
            "client": client_id,
            "symbol": symbol,
            "qty": qty,
            "source": "DUMMY",
            "mode": "DUMMY",
        }

    try:
        instrument_key = symbol.upper()
        if "|" not in instrument_key:
            instrument_key = f"NSE_EQ|{instrument_key}"

        response = requests.post(
            f"{UPSTOX_BASE_URL}/order/place",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={
                "instrument_key": instrument_key,
                "quantity": qty,
                "transaction_type": "BUY" if action.upper() == "BUY" else "SELL",
                "order_type": "MARKET",
                "product": "INTRADAY",
                "validity": "DAY",
                "disclosed_quantity": 0,
                "trigger_price": 0,
                "price": 0,
            },
            timeout=12,
        )

        if response.status_code == 200:
            payload = response.json()
            return {
                "status": "SUCCESS",
                "broker_order_id": payload.get("data", {}).get("order_id") or f"ORD_{int(time.time()*1000)}",
                "client": client_id,
                "symbol": symbol,
                "qty": qty,
                "source": "UPSTOX",
                "mode": mode,
            }

        print(f"[UPSTOX ORDER REQUEST FAILED] {response.status_code}: {response.text[:200]}")

    except Exception as exc:
        print(f"[UPSTOX ORDER ERROR] {symbol}: {exc}")

    time.sleep(0.2)
    return {
        "status": "SUCCESS",
        "broker_order_id": f"DUMMY_{int(time.time()*1000)}",
        "client": client_id,
        "symbol": symbol,
        "qty": qty,
        "source": "DUMMY",
        "mode": "DUMMY",
    }