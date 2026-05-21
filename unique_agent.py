import os
import time
import json
import html
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import websocket


BOT = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = str(os.getenv("TELEGRAM_CHAT_ID"))
COOLDOWN_DAYS = int(os.getenv("COOLDOWN_DAYS") or "60")
COOLDOWN_SECONDS = COOLDOWN_DAYS * 86400
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES") or "60")
DATA_FILE = "/data/seen_names_tickers.json"

seen_mints = set()
db = {"names": {}, "tickers": {}}

start_time = time.time()
ws_connected = False
tokens_seen = 0
alerts_sent = 0
last_coin = "None yet"
last_alert = "None yet"


def n():
    return chr(10)


def esc(value):
    return html.escape(str(value or ""))


def normalize(value):
    return str(value or "").strip().lower()


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def uptime():
    seconds = int(time.time() - start_time)
    return f"{seconds // 3600} hr {(seconds % 3600) // 60} min"


def money(value):
    try:
        return "$" + format(float(value), ",.0f")
    except Exception:
        return "Unknown"


def load_db():
    global db
    try:
        with open(DATA_FILE, "r") as file:
            db = json.load(file)
    except Exception:
        db = {"names": {}, "tickers": {}}


def save_db():
    try:
        with open(DATA_FILE, "w") as file:
            json.dump(db, file)
    except Exception as error:
        print("save db error", error, flush=True)


def cleanup_old_entries():
    cutoff = time.time() - COOLDOWN_SECONDS
    db["names"] = {key: value for key, value in db.get("names", {}).items() if value >= cutoff}
    db["tickers"] = {key: value for key, value in db.get("tickers", {}).items() if value >= cutoff}
    save_db()


load_db()
cleanup_old_entries()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"running")


threading.Thread(
    target=lambda: HTTPServer(("0.0.0.0", 3000), HealthHandler).serve_forever(),
    daemon=True,
).start()


def send_message(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            json={
                "chat_id": CHAT,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=10,
        )
    except Exception as error:
        print("telegram message error", error, flush=True)


def send_alert(coin, mint, reason):
    global alerts_sent, last_alert

    name = coin.get("name", "Unknown")
    symbol = coin.get("symbol", "Unknown")
    image = coin.get("image_uri") or coin.get("image") or ""
    pump_link = f"https://pump.fun/coin/{mint}"

    caption = (
        "🆕 <b>Unique Pump.fun Name/Ticker Alert</b>" + n() + n()
        + "🧠 <b>Reason:</b> " + esc(reason) + n()
        + "🪙 <b>Name:</b> " + esc(name) + n()
        + "🏷 <b>Ticker:</b> " + esc(symbol) + n()
        + "💰 <b>Market Cap:</b> " + money(coin.get("usd_market_cap")) + n() + n()
        + "🚀 <b>Pump.fun:</b>" + n() + pump_link + n() + n()
        + "🧬 <b>CA:</b>" + n() + "<code>" + esc(mint) + "</code>"
    )

    url = f"https://api.telegram.org/bot{BOT}/" + ("sendPhoto" if image else "sendMessage")
    payload = {"chat_id": CHAT, "parse_mode": "HTML"}

    if image:
        payload.update({"photo": image, "caption": caption})
    else:
        payload.update({"text": caption, "disable_web_page_preview": False})

    try:
        requests.post(url, json=payload, timeout=10)
        alerts_sent += 1
        last_alert = f"{name} / {symbol}"
    except Exception as error:
        print("telegram alert error", error, flush=True)


def fetch_coin(mint):
    try:
        response = requests.get(
            f"https://frontend-api-v3.pump.fun/coins/{mint}?sync=true",
            timeout=10,
        )
        data = response.json()
        return data.get("data", data)
    except Exception as error:
        print("coin fetch error", error, flush=True)
        return None


def check_coin(mint):
    global tokens_seen, last_coin

    time.sleep(2)

    coin = fetch_coin(mint)
    if not coin:
        return

    name_raw = coin.get("name", "")
    symbol_raw = coin.get("symbol", "")
    name = normalize(name_raw)
    symbol = normalize(symbol_raw)

    if not name and not symbol:
        return

    cleanup_old_entries()

    name_seen = bool(name and name in db["names"])
    symbol_seen = bool(symbol and symbol in db["tickers"])

    if name_seen or symbol_seen:
        print(f"suppressed duplicate/cooldown: {name} / {symbol}", flush=True)
        return

    reason = "name and ticker have not alerted in cooldown"
    if name and not symbol:
        reason = "name has not alerted in cooldown"
    elif symbol and not name:
        reason = "ticker has not alerted in cooldown"

    timestamp = time.time()
    if name:
        db["names"][name] = timestamp
    if symbol:
        db["tickers"][symbol] = timestamp
    save_db()

    send_alert(coin, mint, reason)


def status_text():
    return (
        "✅ <b>Unique Agent Status</b>" + n() + n()
        + "🔌 <b>Websocket:</b> " + ("connected" if ws_connected else "not connected") + n()
        + "⏱ <b>Uptime:</b> " + uptime() + n()
        + "👀 <b>Tokens scanned:</b> " + str(tokens_seen) + n()
        + "🚨 <b>Alerts sent:</b> " + str(alerts_sent) + n()
        + "📛 <b>Names on cooldown:</b> " + str(len(db.get("names", {}))) + n()
        + "🏷 <b>Tickers on cooldown:</b> " + str(len(db.get("tickers", {}))) + n() + n()
        + "🪙 <b>Last coin:</b>" + n() + "<code>" + esc(last_coin) + "</code>" + n() + n()
        + "📣 <b>Last alert:</b> " + esc(last_alert) + n()
        + "🕒 <b>Checked:</b> " + now_utc()
    )


def command_loop():
    offset = 0

    while True:
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{BOT}/getUpdates",
                params={"timeout": 25, "offset": offset},
                timeout=30,
            )

            for update in response.json().get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message", {})
                chat_id = str(message.get("chat", {}).get("id", ""))
                text = (message.get("text") or "").strip().lower()

                if chat_id != CHAT:
                    continue

                if text in ["/status", "status", "/ping", "ping"]:
                    send_message(status_text())

                elif text in ["/restart", "restart"]:
                    send_message("♻️ Restarting unique agent now...")
                    time.sleep(1)
                    os._exit(0)

                elif text in ["/help", "help"]:
                    send_message(
                        "🤖 <b>Commands</b>" + n() + n()
                        + "/status - health check" + n()
                        + "/ping - same as status" + n()
                        + "/restart - restart the bot" + n()
                        + "/help - show commands"
                    )

        except Exception as error:
            print("command error", error, flush=True)
            time.sleep(5)


def heartbeat_loop():
    while True:
        time.sleep(HEARTBEAT_MINUTES * 60)
        send_message("🟢 <b>Regular Unique Agent Checkup</b>" + n() + n() + status_text())


def on_open(ws):
    global ws_connected

    ws_connected = True
    print("connected", flush=True)
    ws.send(json.dumps({"method": "subscribeNewToken"}))


def on_close(ws, *args):
    global ws_connected

    ws_connected = False
    print("closed", flush=True)


def on_message(ws, message):
    global tokens_seen, last_coin

    try:
        event = json.loads(message)
        mint = event.get("mint") or event.get("mintAddress") or event.get("ca")

        if not mint or mint in seen_mints:
            return

        seen_mints.add(mint)
        tokens_seen += 1
        last_coin = mint

        threading.Thread(target=check_coin, args=(mint,), daemon=True).start()

    except Exception as error:
        print("message error", error, flush=True)


threading.Thread(target=command_loop, daemon=True).start()
threading.Thread(target=heartbeat_loop, daemon=True).start()

while True:
    print("connecting", flush=True)

    websocket.WebSocketApp(
        "wss://pumpportal.fun/api/data",
        on_open=on_open,
        on_message=on_message,
        on_close=on_close,
    ).run_forever()

    print("reconnecting", flush=True)
    time.sleep(5)
