import io
import json
import csv
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import database as db

ANGEL_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
ZERODHA_URL = "https://api.kite.trade/instruments"

def get_session():
    session = requests.Session()
    retries = Retry(total=5, backoff_factor=1, status_forcelist=[502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "*/*",
        "Connection": "keep-alive"
    })
    return session

def try_download_angel():
    print("[*] Trying Angel One instrument master dump (Streamed)...")
    s = get_session()
    with s.get(ANGEL_URL, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        buffer = bytearray()
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                buffer.extend(chunk)
                print(f"    Downloaded {len(buffer) / (1024 * 1024):.1f} MB...", end="\r")
        print("\n[*] Download complete! Parsing JSON...")
        data = json.loads(buffer.decode("utf-8"))

    stocks = []
    for item in data:
        exch = item.get("exch_seg")
        symbol = item.get("symbol", "")
        if exch in ["NSE", "BSE"] and (symbol.endswith("-EQ") or exch == "BSE"):
            stocks.append({
                "token": str(item.get("token")),
                "symbol": symbol,
                "name": item.get("name"),
                "exchange": exch,
                "instrument_type": "EQUITY",
                "lot_size": int(item.get("lotsize", 1) or 1),
                "tick_size": float(item.get("tick_size", 0.05) or 0.05)
            })
    return stocks

def try_download_zerodha():
    print("\n[*] Fallback: Downloading Zerodha NSE & BSE CSV master...")
    s = get_session()
    resp = s.get(ZERODHA_URL, timeout=60)
    resp.raise_for_status()
    
    reader = csv.DictReader(io.StringIO(resp.text))
    stocks = []
    for row in reader:
        exch = row.get("exchange")
        segment = row.get("segment")
        if exch in ["NSE", "BSE"] and segment in ["NSE", "BSE"]:
            stocks.append({
                "token": str(row.get("instrument_token")),
                "symbol": row.get("tradingsymbol"),
                "name": row.get("name"),
                "exchange": exch,
                "instrument_type": row.get("instrument_type", "EQ"),
                "lot_size": int(row.get("lot_size", 1) or 1),
                "tick_size": float(row.get("tick_size", 0.05) or 0.05)
            })
    return stocks

def seed_stocks():
    db.Base.metadata.create_all(bind=db.engine)
    stocks_to_insert = []
    
    try:
        stocks_to_insert = try_download_angel()
    except Exception as e:
        print(f"\n[!] Angel One stream interrupted: {e}")
        try:
            stocks_to_insert = try_download_zerodha()
        except Exception as e2:
            print(f"[!] Zerodha fallback failed too: {e2}")
            return

    if not stocks_to_insert:
        print("[!] No stock records retrieved.")
        return

    print(f"\n[*] Total filtered equity stocks: {len(stocks_to_insert)}")
    session = db.SessionLocal()
    
    print("[*] Clearing old records from stock_master...")
    session.query(db.StockMaster).delete()
    session.commit()

    print("[*] Inserting in batches to database...")
    batch_size = 2000
    for i in range(0, len(stocks_to_insert), batch_size):
        batch = stocks_to_insert[i:i + batch_size]
        session.bulk_insert_mappings(db.StockMaster, batch)
        session.commit()
        print(f"    Inserted {min(i + batch_size, len(stocks_to_insert))} / {len(stocks_to_insert)} stocks...")

    session.close()
    print("[✔] Successfully loaded all stocks into Database!")

if __name__ == "__main__":
    seed_stocks()