import os, time, json, hmac, base64, hashlib, threading, uuid
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timezone, time as dtime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, Response
from dotenv import load_dotenv

load_dotenv()

# =========================================================
# V8 SETTINGS
# =========================================================
BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

DEMO = os.getenv("OKX_DEMO", "true").lower() == "true"
AUTO_TRADE = os.getenv("AUTO_TRADE", "true").lower() == "true"

BAR = os.getenv("BAR", "5m")
TREND_BAR = os.getenv("TREND_BAR", "15m")

# Final risk model selected from the user's testing:
MARGIN_USDT = Decimal(os.getenv("MARGIN_USDT", "10"))
LEVERAGE = Decimal(os.getenv("LEVERAGE", "3"))
TD_MODE = "isolated"

# Fee/slippage buffer is a calculation buffer, NOT extra leverage.
FEE_BUFFER_USDT = Decimal(os.getenv("FEE_BUFFER_USDT", "0.13"))

# Initial protection. Loss is never widened after entry.
SL_PERCENT = Decimal(os.getenv("SL_PERCENT", "0.50"))
TP_PERCENT = Decimal(os.getenv("TP_PERCENT", "0.80"))

# Step-based profit protection: avoids the old "double loss" behaviour.
STEP_TRIGGER_PCT = Decimal(os.getenv("STEP_TRIGGER_PCT", "0.50"))
STEP_LOCK_PCT = Decimal(os.getenv("STEP_LOCK_PCT", "0.05"))

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "20"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "7"))
MAJOR_MIN_SCORE = int(os.getenv("MAJOR_MIN_SCORE", "8"))

ADX_MIN = Decimal(os.getenv("ADX_MIN", "18"))
VOLUME_MULT = Decimal(os.getenv("VOLUME_MULT", "0.80"))
ATR_MIN_PCT = Decimal(os.getenv("ATR_MIN_PCT", "0.05"))

# Pakistan-time windows observed as strongest by the user.
PKT_TZ = ZoneInfo("Asia/Karachi")
# User-observed PKT windows are retained.
# Additional one-hour high-activity windows are based on UTC research:
# 14:00-15:00 UTC -> 19:00-20:00 PKT
# 16:00-17:00 UTC -> 21:00-22:00 PKT
# UTC-based conversion is used so these remain stable across DST changes.
OBSERVED_SESSION_WINDOWS = (
    (dtime(1, 0), dtime(2, 30), "OBSERVED"),
    (dtime(6, 0), dtime(7, 0), "OBSERVED"),
    (dtime(10, 0), dtime(11, 0), "OBSERVED"),
)

HIGH_VOLUME_UTC_WINDOWS = (
    (dtime(14, 0), dtime(15, 0), "HIGH_VOLUME_UTC_14-15"),
    (dtime(16, 0), dtime(17, 0), "HIGH_VOLUME_UTC_16-17"),
)

SYMBOLS = [
    x.strip()
    for x in os.getenv(
        "SYMBOLS",
        "BTC-USDT-SWAP,"
        "ETH-USDT-SWAP,"
        "XRP-USDT-SWAP,"
        "DOGE-USDT-SWAP,"
        "SOL-USDT-SWAP,"
        "SHIB-USDT-SWAP,"
        "FIL-USDT-SWAP,"
        "NEAR-USDT-SWAP,"
        "ICP-USDT-SWAP,"
        "XAU-USDT-SWAP"
    ).split(",")
    if x.strip()
]

MEME_SYMBOLS = {"DOGE-USDT-SWAP", "SHIB-USDT-SWAP"}

app = Flask(__name__)
session = requests.Session()
state = {}
state_lock = threading.Lock()
order_lock = threading.Lock()
server_offset_ms = 0
worker_started = False
position_mode = "net"


def log(message):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def dec(value):
    return Decimal(str(value))


def fmt(value, places=12):
    if value is None:
        return "-"
    return f"{value:.{places}f}".rstrip("0").rstrip(".")


def floor_step(value, step):
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_step(value, step):
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def public_get(path, params=None):
    response = session.get(BASE_URL + path, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX PUBLIC {data.get('code')}: {data.get('msg')}")
    return data


def sync_okx_time():
    global server_offset_ms
    before = int(time.time() * 1000)
    data = public_get("/api/v5/public/time")
    after = int(time.time() * 1000)
    server_ms = int(data["data"][0]["ts"])
    server_offset_ms = server_ms - ((before + after) // 2)
    log(f"OKX TIME SYNCED | offset_ms={server_offset_ms}")


def utc_timestamp():
    ms = int(time.time() * 1000) + server_offset_ms
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def create_signature(timestamp, method, request_path, body=""):
    prehash = timestamp + method.upper() + request_path + body
    digest = hmac.new(SECRET_KEY.encode(), prehash.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def private_request(method, path, payload=None, params=None):
    if not API_KEY or not SECRET_KEY or not PASSPHRASE:
        raise RuntimeError("OKX API credentials missing")

    method = method.upper()
    request_path = path
    if params:
        request_path += "?" + urlencode([(str(k), str(v)) for k, v in params.items()])

    body = "" if payload is None else json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    timestamp = utc_timestamp()
    headers = {
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": create_signature(timestamp, method, request_path, body),
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "OK-ACCESS-TIMESTAMP": timestamp,
    }
    if DEMO:
        headers["x-simulated-trading"] = "1"

    response = session.request(
        method, BASE_URL + path, headers=headers,
        data=body or None, params=params, timeout=15
    )
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}

    if response.status_code >= 400 or data.get("code") != "0":
        raise RuntimeError(f"OKX PRIVATE {response.status_code}: {data}")
    return data


def candles(symbol, bar, limit=160):
    data = public_get("/api/v5/market/candles", {"instId": symbol, "bar": bar, "limit": str(limit)})
    result = []
    for row in reversed(data.get("data", [])):
        result.append({
            "ts": int(row[0]), "open": dec(row[1]), "high": dec(row[2]),
            "low": dec(row[3]), "close": dec(row[4]), "volume": dec(row[5]),
            "confirm": row[8] if len(row) > 8 else "1"
        })
    return result


def ticker(symbol):
    data = public_get("/api/v5/market/ticker", {"instId": symbol})
    rows = data.get("data", [])
    if not rows:
        raise RuntimeError(f"Ticker unavailable: {symbol}")
    return dec(rows[0]["last"])


def mark_price(symbol):
    data = public_get("/api/v5/public/mark-price", {"instType": "SWAP", "instId": symbol})
    rows = data.get("data", [])
    return dec(rows[0]["markPx"]) if rows else ticker(symbol)


def ema(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result
    value = sum(values[:period], Decimal("0")) / Decimal(period)
    result[period - 1] = value
    multiplier = Decimal("2") / Decimal(period + 1)
    for i in range(period, len(values)):
        value = values[i] * multiplier + value * (Decimal("1") - multiplier)
        result[i] = value
    return result


def rsi(values, period):
    result = [None] * len(values)
    if len(values) <= period:
        return result
    gains = [max(values[i] - values[i - 1], Decimal("0")) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], Decimal("0")) for i in range(1, len(values))]
    avg_gain = sum(gains[:period], Decimal("0")) / Decimal(period)
    avg_loss = sum(losses[:period], Decimal("0")) / Decimal(period)

    def rv(gain, loss):
        if loss == 0:
            return Decimal("100")
        rs = gain / loss
        return Decimal("100") - Decimal("100") / (Decimal("1") + rs)

    result[period] = rv(avg_gain, avg_loss)
    for j in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[j]) / Decimal(period)
        avg_loss = (avg_loss * (period - 1) + losses[j]) / Decimal(period)
        result[j + 1] = rv(avg_gain, avg_loss)
    return result


def atr(cs, period=14):
    if len(cs) <= period:
        return None
    trs = []
    for i in range(1, len(cs)):
        trs.append(max(
            cs[i]["high"] - cs[i]["low"],
            abs(cs[i]["high"] - cs[i - 1]["close"]),
            abs(cs[i]["low"] - cs[i - 1]["close"])
        ))
    return sum(trs[-period:], Decimal("0")) / Decimal(period)


def adx(cs, period=14):
    if len(cs) < period + 2:
        return Decimal("0")
    plus = Decimal("0"); minus = Decimal("0"); total_range = Decimal("0")
    for i in range(max(1, len(cs) - period), len(cs)):
        up = cs[i]["high"] - cs[i - 1]["high"]
        down = cs[i - 1]["low"] - cs[i]["low"]
        if up > down and up > 0:
            plus += up
        if down > up and down > 0:
            minus += down
        total_range += cs[i]["high"] - cs[i]["low"]
    if total_range == 0:
        return Decimal("0")
    plus_di = plus / total_range * Decimal("100")
    minus_di = minus / total_range * Decimal("100")
    total = plus_di + minus_di
    return abs(plus_di - minus_di) / total * Decimal("100") if total else Decimal("0")


def macd(values):
    e12 = ema(values, 12); e26 = ema(values, 26)
    line = [e12[i] - e26[i] if e12[i] is not None and e26[i] is not None else None for i in range(len(values))]
    valid = [x for x in line if x is not None]
    sig_valid = ema(valid, 9)
    signal = [None] * (len(values) - len(sig_valid)) + sig_valid
    return line, signal


def vwap(cs, period=20):
    q = cs[-period:]
    volume = sum((x["volume"] for x in q), Decimal("0"))
    if volume == 0:
        return q[-1]["close"]
    return sum(((x["high"] + x["low"] + x["close"]) / Decimal("3")) * x["volume"] for x in q) / volume


def get_trend(symbol):
    cs = [x for x in candles(symbol, TREND_BAR, 80) if x["confirm"] == "1"]
    if len(cs) < 22:
        return "flat"
    values = [x["close"] for x in cs]
    e20 = ema(values, 20); i = len(values) - 1
    if e20[i] is None or e20[i - 1] is None:
        return "flat"
    if values[i] > e20[i] and e20[i] > e20[i - 1]:
        return "bull"
    if values[i] < e20[i] and e20[i] < e20[i - 1]:
        return "bear"
    return "flat"


def _time_in_window(t, start, end):
    # End is treated as exclusive to avoid double-counting boundary candles.
    return start <= t < end


def current_session():
    now_pkt = datetime.now(PKT_TZ)
    pkt_time = now_pkt.time()

    # 1) Preserve the user's observed PKT windows exactly.
    for start, end, _label in OBSERVED_SESSION_WINDOWS:
        if _time_in_window(pkt_time, start, end):
            return f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')} PKT | OBSERVED"

    # 2) Add research-backed high-volume one-hour windows using UTC.
    # This avoids hard-coding London/NY DST and keeps the windows tied to
    # the observed global UTC activity pattern.
    now_utc = datetime.now(timezone.utc).time()
    for start, end, label in HIGH_VOLUME_UTC_WINDOWS:
        if _time_in_window(now_utc, start, end):
            pkt_start = (datetime.combine(now_pkt.date(), start, tzinfo=timezone.utc)
                         .astimezone(PKT_TZ).time())
            pkt_end = (datetime.combine(now_pkt.date(), end, tzinfo=timezone.utc)
                       .astimezone(PKT_TZ).time())
            return f"{pkt_start.strftime('%H:%M')}-{pkt_end.strftime('%H:%M')} PKT | {label}"

    return "OFF_SESSION"


def analyze(symbol):
    cs = [x for x in candles(symbol, BAR, 160) if x["confirm"] == "1"]
    if len(cs) < 105:
        return {"signal": "NONE", "score": 0, "reason": "Not enough candles", "session": current_session()}

    values = [x["close"] for x in cs]
    i = len(cs) - 1
    e20 = ema(values, 20); r14 = rsi(values, 14); r100 = rsi(values, 100)
    atr_value = atr(cs); adx_value = adx(cs); macd_line, macd_signal = macd(values)
    vw = vwap(cs); trend15 = get_trend(symbol)

    if any(x is None for x in (e20[i], r14[i], r100[i], macd_line[i], macd_signal[i], atr_value)):
        return {"signal": "NONE", "score": 0, "reason": "Indicator unavailable", "session": current_session()}

    avg_volume = sum((x["volume"] for x in cs[-21:-1]), Decimal("0")) / Decimal("20")
    volume_ratio = cs[i]["volume"] / avg_volume if avg_volume else Decimal("0")
    atr_pct = atr_value / values[i] * Decimal("100")

    buy = 0; sell = 0; reasons = []

    # Exactly ten score components. Maximum score = 10.
    if r14[i] > r100[i]: buy += 1; reasons.append("RSI bullish")
    elif r14[i] < r100[i]: sell += 1; reasons.append("RSI bearish")

    if values[i] > e20[i]: buy += 1; reasons.append("Above EMA20")
    elif values[i] < e20[i]: sell += 1; reasons.append("Below EMA20")

    if e20[i] > e20[i - 1]: buy += 1; reasons.append("EMA slope up")
    elif e20[i] < e20[i - 1]: sell += 1; reasons.append("EMA slope down")

    if macd_line[i] > macd_signal[i]: buy += 1; reasons.append("MACD bullish")
    elif macd_line[i] < macd_signal[i]: sell += 1; reasons.append("MACD bearish")

    if values[i] > vw: buy += 1; reasons.append("Above VWAP")
    elif values[i] < vw: sell += 1; reasons.append("Below VWAP")

    if adx_value >= ADX_MIN:
        if buy > sell: buy += 1; reasons.append("ADX confirmed")
        elif sell > buy: sell += 1; reasons.append("ADX confirmed")

    if volume_ratio >= VOLUME_MULT:
        if buy > sell: buy += 1; reasons.append("Volume confirmed")
        elif sell > buy: sell += 1; reasons.append("Volume confirmed")

    if atr_pct >= ATR_MIN_PCT:
        if buy > sell: buy += 1; reasons.append("ATR OK")
        elif sell > buy: sell += 1; reasons.append("ATR OK")

    if trend15 == "bull": buy += 1; reasons.append("15m bull")
    elif trend15 == "bear": sell += 1; reasons.append("15m bear")

    recent_high = max(x["high"] for x in cs[-21:-1])
    recent_low = min(x["low"] for x in cs[-21:-1])
    prev = cs[i - 1]
    # Structure/fake-breakout is the tenth component.
    fake_break_down = prev["low"] < recent_low and prev["close"] > recent_low
    fake_break_up = prev["high"] > recent_high and prev["close"] < recent_high
    if fake_break_down:
        buy += 1; reasons.append("Fake breakdown rejection")
    elif fake_break_up:
        sell += 1; reasons.append("Fake breakout rejection")
    else:
        reasons.append("No fresh rejection")

    score = max(buy, sell)
    signal = "NONE"
    required = MIN_SCORE if symbol in MEME_SYMBOLS else MAJOR_MIN_SCORE

    if buy > sell and buy >= required:
        signal = "BUY"
    elif sell > buy and sell >= required:
        signal = "SELL"

    if trend15 == "bull" and signal == "SELL":
        signal = "NONE"; reasons.append("Blocked by 15m bullish trend")
    if trend15 == "bear" and signal == "BUY":
        signal = "NONE"; reasons.append("Blocked by 15m bearish trend")

    session = current_session()
    if signal != "NONE" and session == "OFF_SESSION":
        signal = "NONE"; reasons.append("Blocked outside selected PKT session")

    return {
        "signal": signal, "score": score, "buy": buy, "sell": sell,
        "required_score": required, "entry": values[i], "rsi14": r14[i],
        "rsi100": r100[i], "ema20": e20[i], "macd": macd_line[i],
        "macd_signal": macd_signal[i], "vwap": vw, "adx": adx_value,
        "atr_pct": atr_pct, "volume_ratio": volume_ratio, "trend15": trend15,
        "session": session, "reason": " | ".join(reasons)
    }


def get_instrument(symbol):
    data = public_get("/api/v5/public/instruments", {"instType": "SWAP", "instId": symbol})
    rows = data.get("data", [])
    if not rows:
        raise RuntimeError(f"Instrument not found: {symbol}")
    x = rows[0]
    return {
        "ctVal": dec(x["ctVal"]), "lotSz": dec(x["lotSz"]),
        "minSz": dec(x["minSz"]), "tickSz": dec(x["tickSz"]),
        "state": x.get("state", "")
    }


def get_account_config():
    return private_request("GET", "/api/v5/account/config")


def refresh_position_mode():
    global position_mode
    data = get_account_config()
    rows = data.get("data", [])
    if rows:
        raw_mode = str(rows[0].get("posMode", "net")).strip().lower()
    else:
        raw_mode = "net"

    # OKX returns the one-way position mode as `net_mode` on some
    # account configurations and `net` on others. Treat both as the
    # same one-way/net position mode.
    if raw_mode in ("net", "net_mode"):
        position_mode = "net"
    elif raw_mode in ("long_short_mode", "long_short"):
        position_mode = "long_short_mode"
    else:
        raise RuntimeError(f"Unsupported OKX position mode: {raw_mode}")

    log(f"OKX POSITION MODE | API={raw_mode} | USING={position_mode}")


def get_position(symbol):
    data = private_request("GET", "/api/v5/account/positions", params={"instId": symbol})
    for row in data.get("data", []):
        if dec(row.get("pos", "0")) != 0:
            return row
    return None


def set_leverage(symbol):
    payload = {"instId": symbol, "lever": fmt(LEVERAGE), "mgnMode": TD_MODE}
    # Only required in isolated long/short position mode.
    if position_mode == "long_short_mode":
        # Both sides are configured so either direction can be opened.
        for side in ("long", "short"):
            p = dict(payload); p["posSide"] = side
            private_request("POST", "/api/v5/account/set-leverage", payload=p)
        return
    return private_request("POST", "/api/v5/account/set-leverage", payload=payload)


def calculate_order_size(symbol, price, info):
    target_notional = MARGIN_USDT * LEVERAGE
    raw_size = target_notional / (info["ctVal"] * price)
    size = floor_step(raw_size, info["lotSz"])

    if size < info["minSz"]:
        minimum_notional = info["minSz"] * info["ctVal"] * price
        minimum_margin = minimum_notional / LEVERAGE
        raise RuntimeError(
            f"{symbol}: $10 margin cannot meet OKX minimum contract size | "
            f"target_notional=${fmt(target_notional,2)} | "
            f"minimum_notional≈${fmt(minimum_notional,2)} | "
            f"minimum_margin_at_{fmt(LEVERAGE,2)}x≈${fmt(minimum_margin,2)}"
        )
    actual_notional = size * info["ctVal"] * price
    return size, actual_notional


def cancel_algo(symbol, algo_id):
    return private_request("POST", "/api/v5/trade/cancel-algos", payload=[{"instId": symbol, "algoId": str(algo_id)}])


def pending_oco(symbol):
    return private_request(
        "GET", "/api/v5/trade/orders-algo-pending",
        params={"instType": "SWAP", "instId": symbol, "ordType": "oco"}
    )


def cancel_existing_protection(symbol):
    ids = set()
    try:
        for row in pending_oco(symbol).get("data", []):
            if row.get("algoId"):
                ids.add(str(row["algoId"]))
    except Exception as e:
        log(f"OCO pending check warning | {symbol} | {e}")

    try:
        pos = get_position(symbol)
        if pos:
            for row in pos.get("closeOrderAlgo", []) or []:
                if row.get("algoId") and str(row.get("closeFraction", "")) == "1":
                    ids.add(str(row["algoId"]))
    except Exception as e:
        log(f"OCO position check warning | {symbol} | {e}")

    for algo_id in ids:
        try:
            cancel_algo(symbol, algo_id)
            log(f"OLD OCO CANCELLED | {symbol} | algoId={algo_id}")
        except Exception as e:
            log(f"OCO CANCEL WARNING | {symbol} | {e}")


def position_side_from_position(pos):
    if position_mode == "long_short_mode":
        return "long" if pos.get("posSide") == "long" else "short"
    return "buy" if dec(pos.get("pos", "0")) > 0 else "sell"


def calculate_initial_sl_tp(side, entry, tick):
    if side == "buy":
        sl = floor_step(entry * (Decimal("1") - SL_PERCENT / Decimal("100")), tick)
        tp = floor_step(entry * (Decimal("1") + TP_PERCENT / Decimal("100")), tick)
    else:
        sl = ceil_step(entry * (Decimal("1") + SL_PERCENT / Decimal("100")), tick)
        tp = ceil_step(entry * (Decimal("1") - TP_PERCENT / Decimal("100")), tick)
    return sl, tp


def place_full_position_oco(symbol, side, sl_price, tp_price, tick):
    if side == "buy":
        sl_price = floor_step(sl_price, tick); tp_price = floor_step(tp_price, tick); close_side = "sell"
    else:
        sl_price = ceil_step(sl_price, tick); tp_price = ceil_step(tp_price, tick); close_side = "buy"

    payload = {
        "instId": symbol,
        "tdMode": TD_MODE,
        "side": close_side,
        "ordType": "oco",
        "reduceOnly": True,
        "closeFraction": "1",
        "tpTriggerPx": fmt(tp_price), "tpOrdPx": "-1", "tpTriggerPxType": "mark",
        "slTriggerPx": fmt(sl_price), "slOrdPx": "-1", "slTriggerPxType": "mark",
        "algoClOrdId": "p" + uuid.uuid4().hex[:30],
    }
    if position_mode == "long_short_mode":
        payload["posSide"] = "long" if side == "buy" else "short"

    # In net mode OKX requires reduceOnly=true when closeFraction=1.
    result = private_request("POST", "/api/v5/trade/order-algo", payload=payload)
    row = (result.get("data") or [{}])[0]
    if row.get("sCode") not in (None, "", "0"):
        raise RuntimeError(f"OCO rejected | sCode={row.get('sCode')} | {row.get('sMsg')}")
    if not row.get("algoId"):
        raise RuntimeError("OCO response has no algoId")
    return result


def protection_exists(symbol):
    try:
        pos = get_position(symbol)
        if pos:
            for row in pos.get("closeOrderAlgo", []) or []:
                if str(row.get("closeFraction", "")) == "1" and row.get("algoId"):
                    return True
    except Exception:
        pass
    try:
        for row in pending_oco(symbol).get("data", []):
            if row.get("algoId"):
                return True
    except Exception:
        pass
    return False


def emergency_close(symbol):
    payload = {"instId": symbol, "mgnMode": TD_MODE, "autoCxl": True}
    return private_request("POST", "/api/v5/trade/close-position", payload=payload)


def place_order(symbol, analysis):
    if not AUTO_TRADE:
        return {"status": "BLOCKED", "reason": "AUTO_TRADE=false"}
    if not DEMO:
        return {"status": "BLOCKED", "reason": "Live trading disabled"}
    if analysis.get("signal") not in ("BUY", "SELL"):
        return {"status": "NO_TRADE", "reason": "Signal below threshold/session filter"}

    with order_lock:
        if get_position(symbol):
            return {"status": "BLOCKED", "reason": "Existing position"}

        side = "buy" if analysis["signal"] == "BUY" else "sell"
        price = ticker(symbol)
        info = get_instrument(symbol)
        if info["state"] != "live":
            raise RuntimeError(f"Instrument not live: {info['state']}")

        set_leverage(symbol)
        size, actual_notional = calculate_order_size(symbol, price, info)

        payload = {
            "instId": symbol, "tdMode": TD_MODE, "side": side,
            "ordType": "market", "sz": fmt(size),
            "clOrdId": "bot" + uuid.uuid4().hex[:24]
        }
        if position_mode == "long_short_mode":
            payload["posSide"] = "long" if side == "buy" else "short"

        log(
            f"ORDER SUBMIT | {symbol} | {side.upper()} | "
            f"margin=${fmt(MARGIN_USDT,2)} | {fmt(LEVERAGE,2)}x | "
            f"target=${fmt(MARGIN_USDT*LEVERAGE,2)} | actual=${fmt(actual_notional,2)} | sz={fmt(size)}"
        )
        result = private_request("POST", "/api/v5/trade/order", payload=payload)
        row = (result.get("data") or [{}])[0]
        if row.get("sCode") not in (None, "", "0"):
            raise RuntimeError(f"ORDER REJECTED | {row.get('sCode')} | {row.get('sMsg')}")

        filled = None
        for _ in range(10):
            time.sleep(1)
            filled = get_position(symbol)
            if filled:
                break
        if not filled:
            raise RuntimeError("ENTRY SENT BUT POSITION WAS NOT CONFIRMED")

        entry = dec(filled.get("avgPx") or price)
        sl, tp = calculate_initial_sl_tp(side, entry, info["tickSz"])
        cancel_existing_protection(symbol)

        last_error = None
        for attempt in range(1, 4):
            try:
                oco = place_full_position_oco(symbol, side, sl, tp, info["tickSz"])
                time.sleep(1)
                if not protection_exists(symbol):
                    raise RuntimeError("OCO submitted but full-position protection was not verified")
                with state_lock:
                    state.setdefault(symbol, {}).update({
                        "entry_price": entry, "current_sl": sl, "current_tp": tp,
                        "position_size": dec(filled.get("pos", "0")),
                        "protection": "ACTIVE", "protection_algo": oco,
                        "fee_buffer_usdt": FEE_BUFFER_USDT,
                    })
                log(f"TRADE PROTECTED | {symbol} | {side.upper()} | ENTRY={fmt(entry)} | SL={fmt(sl)} | TP={fmt(tp)}")
                return {
                    "status": "ORDER_AND_PROTECTION_ACTIVE", "symbol": symbol,
                    "side": side, "size": fmt(size), "entry": fmt(entry),
                    "sl": fmt(sl), "tp": fmt(tp), "actual_notional": fmt(actual_notional, 2),
                    "fee_buffer_usdt": fmt(FEE_BUFFER_USDT, 2), "ordId": row.get("ordId", "")
                }
            except Exception as e:
                last_error = e
                log(f"PROTECTION RETRY | {symbol} | {attempt}/3 | {e}")
                if attempt < 3:
                    time.sleep(5)

        log(f"CRITICAL PROTECTION FAILURE | {symbol} | {last_error}")
        try:
            emergency_close(symbol)
            return {"status": "EMERGENCY_CLOSED", "reason": "Protection failed", "error": str(last_error)}
        except Exception as close_error:
            raise RuntimeError(f"CRITICAL: POSITION OPEN, PROTECTION FAILED, EMERGENCY CLOSE FAILED | protection={last_error} | close={close_error}")


def manage_position(symbol):
    pos = get_position(symbol)
    if not pos:
        return

    avg = dec(pos.get("avgPx", "0"))
    if avg <= 0:
        return

    side = "buy" if position_side_from_position(pos) == "buy" or position_side_from_position(pos) == "long" else "sell"
    price = mark_price(symbol)
    info = get_instrument(symbol)
    tick = info["tickSz"]

    if side == "buy":
        profit_pct = (price - avg) / avg * Decimal("100")
    else:
        profit_pct = (avg - price) / avg * Decimal("100")

    with state_lock:
        saved = state.get(symbol, {}).copy()

    current_sl = saved.get("current_sl")
    current_tp = saved.get("current_tp")
    if current_sl is None or current_tp is None:
        current_sl, current_tp = calculate_initial_sl_tp(side, avg, tick)

    # Step ratchet. Once SL moves, it can NEVER move back toward a larger loss.
    new_sl = current_sl
    new_tp = current_tp
    steps = int((profit_pct / STEP_TRIGGER_PCT).to_integral_value(rounding=ROUND_DOWN)) if profit_pct > 0 else 0

    if steps >= 1:
        if side == "buy":
            candidate_sl = avg * (Decimal("1") + Decimal(steps - 1) * STEP_TRIGGER_PCT / Decimal("100") + STEP_LOCK_PCT / Decimal("100"))
            candidate_tp = avg * (Decimal("1") + Decimal(steps + 1) * STEP_TRIGGER_PCT / Decimal("100"))
            new_sl = max(current_sl, floor_step(candidate_sl, tick))
            new_tp = max(current_tp, floor_step(candidate_tp, tick))
        else:
            candidate_sl = avg * (Decimal("1") - Decimal(steps - 1) * STEP_TRIGGER_PCT / Decimal("100") - STEP_LOCK_PCT / Decimal("100"))
            candidate_tp = avg * (Decimal("1") - Decimal(steps + 1) * STEP_TRIGGER_PCT / Decimal("100"))
            new_sl = min(current_sl, ceil_step(candidate_sl, tick))
            new_tp = min(current_tp, ceil_step(candidate_tp, tick))

    # Never widen risk.
    if side == "buy" and new_sl < current_sl:
        new_sl = current_sl
    if side == "sell" and new_sl > current_sl:
        new_sl = current_sl

    active = protection_exists(symbol)
    if not active:
        cancel_existing_protection(symbol)
        try:
            place_full_position_oco(symbol, side, new_sl, new_tp, tick)
            time.sleep(1)
            active = protection_exists(symbol)
        except Exception as e:
            log(f"PROTECTION RESTORE ERROR | {symbol} | {e}")

        if not active:
            try:
                emergency_close(symbol)
                log(f"EMERGENCY CLOSE | {symbol} | protection restore failed")
            except Exception as e:
                log(f"CRITICAL EMERGENCY CLOSE FAILED | {symbol} | {e}")
            return

    changed = new_sl != current_sl or new_tp != current_tp
    if changed:
        log(f"STEP OCO UPDATE | {symbol} | profit={fmt(profit_pct,4)}% | SL {fmt(current_sl)} -> {fmt(new_sl)} | TP {fmt(current_tp)} -> {fmt(new_tp)}")
        old_sl, old_tp = current_sl, current_tp
        cancel_existing_protection(symbol)
        time.sleep(0.4)
        try:
            place_full_position_oco(symbol, side, new_sl, new_tp, tick)
            time.sleep(1)
            if not protection_exists(symbol):
                raise RuntimeError("new OCO verification failed")
            current_sl, current_tp = new_sl, new_tp
        except Exception as e:
            log(f"STEP OCO UPDATE FAILED | {symbol} | {e}")
            try:
                place_full_position_oco(symbol, side, old_sl, old_tp, tick)
                time.sleep(1)
                if not protection_exists(symbol):
                    raise RuntimeError("old OCO verification failed")
                current_sl, current_tp = old_sl, old_tp
            except Exception as restore_error:
                log(f"CRITICAL OLD OCO RESTORE FAILED | {symbol} | {restore_error}")
                try:
                    emergency_close(symbol)
                except Exception as close_error:
                    log(f"CRITICAL EMERGENCY CLOSE FAILED | {symbol} | {close_error}")
                return

    with state_lock:
        state.setdefault(symbol, {}).update({
            "entry_price": avg, "mark_price": price,
            "profit_pct": profit_pct, "current_sl": current_sl,
            "current_tp": current_tp, "protection": "ACTIVE" if active else "MISSING",
            "position_size": dec(pos.get("pos", "0")),
            "fee_buffer_usdt": FEE_BUFFER_USDT,
        })


def startup_checks():
    global worker_started
    log("====================================================")
    log("OKX SCALPING BOT V8")
    log("DEMO + ISOLATED + 3X + $10 MARGIN + FULL OCO")
    log(f"DEMO={DEMO} | AUTO_TRADE={AUTO_TRADE}")
    log(f"MARGIN=${MARGIN_USDT} | LEVERAGE={LEVERAGE}x | TARGET NOTIONAL=${MARGIN_USDT*LEVERAGE}")
    log(f"TD_MODE={TD_MODE} | SL={SL_PERCENT}% | TP={TP_PERCENT}% | FEE BUFFER=${FEE_BUFFER_USDT}")
    log(f"STEP TRIGGER={STEP_TRIGGER_PCT}% | STEP LOCK={STEP_LOCK_PCT}%")
    log(f"SESSIONS PKT=01:00-02:30, 06:00-07:00, 10:00-11:00 + UTC HIGH-VOLUME 14:00-15:00 and 16:00-17:00 (PKT 19:00-20:00 and 21:00-22:00)")
    log(f"SYMBOLS={SYMBOLS}")
    log("====================================================")

    sync_okx_time()
    refresh_position_mode()
    public_get("/api/v5/market/ticker", {"instId": "BTC-USDT-SWAP"})
    private_request("GET", "/api/v5/account/balance")
    with state_lock:
        state["public_api"] = "CONNECTED"
        state["private_api"] = "CONNECTED"
    worker_started = True


def worker():
    try:
        startup_checks()
    except Exception as e:
        log(f"STARTUP ERROR | {type(e).__name__}: {e}")
        with state_lock:
            state["public_api"] = "ERROR"
            state["private_api"] = "ERROR: " + str(e)
        return

    while True:
        for symbol in SYMBOLS:
            try:
                with state_lock:
                    state.setdefault(symbol, {})["last_activity"] = "CHECKING " + symbol

                pos = get_position(symbol)
                if pos:
                    manage_position(symbol)
                    continue

                analysis = analyze(symbol)
                with state_lock:
                    state.setdefault(symbol, {}).update(analysis)
                    state[symbol]["last_checked"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                log(f"{symbol}: {analysis['signal']} {analysis.get('score',0)}/10 | required={analysis.get('required_score','-')} | session={analysis.get('session')} | {analysis.get('reason','')}")

                if analysis.get("signal") in ("BUY", "SELL") and analysis.get("score", 0) >= analysis.get("required_score", MIN_SCORE):
                    try:
                        result = place_order(symbol, analysis)
                        log("TRADE RESULT | " + json.dumps(result, default=str))
                        with state_lock:
                            state[symbol]["trade_status"] = result.get("status", "UNKNOWN")
                            state[symbol]["trade_result"] = result
                    except Exception as e:
                        log(f"TRADE ERROR | {symbol} | {type(e).__name__}: {e}")
                        with state_lock:
                            state[symbol]["trade_status"] = "ERROR"
                            state[symbol]["trade_error"] = str(e)
                else:
                    with state_lock:
                        state[symbol]["trade_status"] = "NO TRADE"
            except Exception as e:
                log(f"{symbol} ERROR | {type(e).__name__}: {e}")
                with state_lock:
                    state.setdefault(symbol, {})["trade_status"] = "ERROR"
                    state[symbol]["trade_error"] = str(e)
        time.sleep(POLL_SECONDS)


HTML = """
<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>OKX Scalping Bot V8</title><style>
body{font-family:Arial;background:#101216;color:#eee;margin:0;padding:14px}.card{background:#1a1e24;border:1px solid #303640;border-radius:10px;padding:10px;margin-bottom:8px}.wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:7px;border-bottom:1px solid #303640;text-align:left;white-space:nowrap}th{background:#191d23;position:sticky;top:0}.buy{color:#43d17a;font-weight:bold}.sell{color:#ff6262;font-weight:bold}.active{color:#43d17a;font-weight:bold}.danger{color:#ff6262;font-weight:bold}
</style></head><body><h2>OKX Scalping Bot V8</h2><div id='top'></div><div id='activity'>Loading...</div><div class='wrap'><table><thead><tr>
<th>Pair</th><th>Signal</th><th>Score</th><th>Entry</th><th>Mark</th><th>P/L%</th><th>SL</th><th>TP</th><th>Protection</th><th>15m</th><th>Session</th><th>Status</th></tr></thead><tbody id='rows'></tbody></table></div>
<script>
async function refresh(){try{const s=await fetch('/api/status').then(r=>r.json());document.getElementById('top').innerHTML='<div class="card">Mode: <b>'+s.mode+'</b> | Margin: <b>$'+s.margin+'</b> | Leverage: <b>'+s.leverage+'x</b> | Target: <b>$'+s.notional+'</b> | TD: <b>'+s.td_mode+'</b> | Fee buffer: <b>$'+s.fee_buffer+'</b></div><div class="card">Public API: <b>'+s.public_api+'</b> | Private API: <b>'+s.private_api+'</b> | Position mode: <b>'+s.position_mode+'</b></div>';document.getElementById('activity').textContent='Last activity: '+s.last_activity+' | Updated: '+s.updated;let h='';for(const [sym,x] of Object.entries(s.symbols)){let sig=x.signal||'NONE';let c=sig==='BUY'?'buy':sig==='SELL'?'sell':'';let p=x.protection||'NONE';let pc=p==='ACTIVE'?'active':'danger';h+='<tr><td>'+sym+'</td><td class="'+c+'">'+sig+'</td><td>'+((x.score??0)+'/10')+'</td><td>'+(x.entry_price||x.entry||'-')+'</td><td>'+(x.mark_price||'-')+'</td><td>'+(x.profit_pct||'-')+'</td><td>'+(x.current_sl||'-')+'</td><td>'+(x.current_tp||'-')+'</td><td class="'+pc+'">'+p+'</td><td>'+(x.trend15||'-')+'</td><td>'+(x.session||'-')+'</td><td>'+(x.trade_status||'WAITING')+'</td></tr>'}document.getElementById('rows').innerHTML=h}catch(e){document.getElementById('activity').textContent='Dashboard error: '+e}}refresh();setInterval(refresh,5000);
</script></body></html>
"""


@app.get("/")
def home():
    return Response(HTML, mimetype="text/html")


@app.get("/api/status")
def api_status():
    with state_lock:
        symbols = {k: v.copy() for k, v in state.items() if k in SYMBOLS}
        public_api = state.get("public_api", "STARTING")
        private_api = state.get("private_api", "STARTING")
    activity = "STARTING"
    for symbol in SYMBOLS:
        value = symbols.get(symbol, {}).get("last_activity")
        if value:
            activity = value
    return jsonify({
        "bot": "OKX Scalping Bot V8", "status": "running" if worker_started else "starting",
        "mode": "DEMO" if DEMO else "LIVE BLOCKED", "demo": DEMO, "auto_trade": AUTO_TRADE,
        "margin": str(MARGIN_USDT), "leverage": str(LEVERAGE), "notional": str(MARGIN_USDT * LEVERAGE),
        "td_mode": TD_MODE, "fee_buffer": str(FEE_BUFFER_USDT), "sl_percent": str(SL_PERCENT),
        "tp_percent": str(TP_PERCENT), "step_trigger": str(STEP_TRIGGER_PCT), "step_lock": str(STEP_LOCK_PCT),
        "position_mode": position_mode, "public_api": public_api, "private_api": private_api,
        "last_activity": activity, "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "symbols": symbols
    })


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "bot": "V8", "demo": DEMO, "auto_trade": AUTO_TRADE})


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True)
