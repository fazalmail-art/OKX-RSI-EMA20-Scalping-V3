import os
import time
import json
import hmac
import base64
import hashlib
import threading
import uuid

from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, Response
from dotenv import load_dotenv

load_dotenv()

# =========================================================
# SETTINGS
# =========================================================

BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")

API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

DEMO = os.getenv("OKX_DEMO", "true").lower() == "true"
AUTO_TRADE = os.getenv("AUTO_TRADE", "true").lower() == "true"

BAR = os.getenv("BAR", "5m")
TREND_BAR = os.getenv("TREND_BAR", "15m")

MARGIN_USDT = Decimal(os.getenv("MARGIN_USDT", "20"))
LEVERAGE = Decimal(os.getenv("LEVERAGE", "5"))

SL_PERCENT = Decimal(os.getenv("SL_PERCENT", "0.4"))
TP_PERCENT = Decimal(os.getenv("TP_PERCENT", "0.8"))

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "20"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "7"))

ADX_MIN = Decimal(os.getenv("ADX_MIN", "18"))
VOLUME_MULT = Decimal(os.getenv("VOLUME_MULT", "0.8"))
ATR_MIN_PCT = Decimal(os.getenv("ATR_MIN_PCT", "0.05"))

TD_MODE = os.getenv("TD_MODE", "isolated")

# =========================================================
# DYNAMIC PROTECTION
# =========================================================

BREAK_EVEN_TRIGGER_PCT = Decimal(
    os.getenv("BREAK_EVEN_TRIGGER_PCT", "0.30")
)

BREAK_EVEN_OFFSET_PCT = Decimal(
    os.getenv("BREAK_EVEN_OFFSET_PCT", "0.05")
)

TRAIL_START_PCT = Decimal(
    os.getenv("TRAIL_START_PCT", "0.50")
)

TRAIL_DISTANCE_PCT = Decimal(
    os.getenv("TRAIL_DISTANCE_PCT", "0.30")
)

PROTECTION_RETRY_SECONDS = int(
    os.getenv("PROTECTION_RETRY_SECONDS", "5")
)

# =========================================================
# SYMBOLS
# =========================================================

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
        "XAU-USDT-SWAP"
    ).split(",")
    if x.strip()
]

# =========================================================
# APP / STATE
# =========================================================

app = Flask(__name__)
session = requests.Session()

state = {}
state_lock = threading.Lock()
order_lock = threading.Lock()

server_offset_ms = 0
worker_started = False


# =========================================================
# LOGGING
# =========================================================

def log(message):
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"{message}",
        flush=True
    )


# =========================================================
# DECIMAL HELPERS
# =========================================================

def dec(value):
    return Decimal(str(value))


def fmt(value, places=12):
    if value is None:
        return "-"

    return (
        f"{value:.{places}f}"
        .rstrip("0")
        .rstrip(".")
    )


def floor_step(value, step):
    if step <= 0:
        return value

    return (
        value / step
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * step


def ceil_step(value, step):
    if step <= 0:
        return value

    return (
        value / step
    ).to_integral_value(
        rounding=ROUND_UP
    ) * step


# =========================================================
# PUBLIC REQUEST
# =========================================================

def public_get(path, params=None, raw=False):

    response = session.get(
        BASE_URL + path,
        params=params,
        timeout=15
    )

    response.raise_for_status()

    data = response.json()

    if raw:
        return data

    if data.get("code") != "0":
        raise RuntimeError(
            f"OKX PUBLIC ERROR "
            f"{data.get('code')}: "
            f"{data.get('msg')}"
        )

    return data


# =========================================================
# SERVER TIME
# =========================================================

def sync_okx_time():

    global server_offset_ms

    local_before = int(time.time() * 1000)

    data = public_get(
        "/api/v5/public/time",
        raw=True
    )

    local_after = int(time.time() * 1000)

    server_ms = int(data["data"][0]["ts"])

    local_mid = (
        local_before + local_after
    ) // 2

    server_offset_ms = (
        server_ms - local_mid
    )

    log(
        "OKX TIME SYNCED | "
        f"offset_ms={server_offset_ms}"
    )


def utc_timestamp():

    current_ms = (
        int(time.time() * 1000)
        + server_offset_ms
    )

    dt = datetime.fromtimestamp(
        current_ms / 1000,
        tz=timezone.utc
    )

    return (
        dt.isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# =========================================================
# SIGNATURE
# =========================================================

def create_signature(
    timestamp,
    method,
    request_path,
    body=""
):

    prehash = (
        timestamp
        + method.upper()
        + request_path
        + body
    )

    digest = hmac.new(
        SECRET_KEY.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256
    ).digest()

    return base64.b64encode(
        digest
    ).decode()


# =========================================================
# PRIVATE REQUEST
# =========================================================

def private_request(
    method,
    path,
    payload=None,
    params=None
):

    if not API_KEY:
        raise RuntimeError("OKX_API_KEY missing")

    if not SECRET_KEY:
        raise RuntimeError("OKX_SECRET_KEY missing")

    if not PASSPHRASE:
        raise RuntimeError("OKX_PASSPHRASE missing")

    method = method.upper()

    request_path = path

    if params:

        query = urlencode([
            (str(k), str(v))
            for k, v in params.items()
        ])

        request_path += "?" + query

    body = ""

    if payload is not None:

        body = json.dumps(
            payload,
            separators=(",", ":"),
            ensure_ascii=False
        )

    timestamp = utc_timestamp()

    signature = create_signature(
        timestamp,
        method,
        request_path,
        body
    )

    headers = {
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "OK-ACCESS-TIMESTAMP": timestamp
    }

    if DEMO:
        headers["x-simulated-trading"] = "1"

    response = session.request(
        method,
        BASE_URL + path,
        headers=headers,
        data=body if body else None,
        params=params,
        timeout=15
    )

    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}

    if response.status_code >= 400:
        raise RuntimeError(
            f"OKX HTTP {response.status_code}: {data}"
        )

    if data.get("code") != "0":
        raise RuntimeError(
            f"OKX PRIVATE ERROR "
            f"{data.get('code')}: "
            f"{data.get('msg')}"
        )

    return data


# =========================================================
# MARKET DATA
# =========================================================

def get_ticker(symbol):

    data = public_get(
        "/api/v5/market/ticker",
        {"instId": symbol}
    )

    rows = data.get("data", [])

    if not rows:
        raise RuntimeError(
            "Ticker unavailable: " + symbol
        )

    return dec(rows[0]["last"])


def get_mark_price(symbol):

    data = public_get(
        "/api/v5/public/mark-price",
        {
            "instType": "SWAP",
            "instId": symbol
        }
    )

    rows = data.get("data", [])

    if not rows:
        return get_ticker(symbol)

    return dec(rows[0]["markPx"])


def get_candles(symbol, bar, limit=160):

    data = public_get(
        "/api/v5/market/candles",
        {
            "instId": symbol,
            "bar": bar,
            "limit": str(limit)
        }
    )

    candles = []

    for row in reversed(data.get("data", [])):

        candles.append({
            "ts": int(row[0]),
            "open": dec(row[1]),
            "high": dec(row[2]),
            "low": dec(row[3]),
            "close": dec(row[4]),
            "volume": dec(row[5]),
            "confirm": (
                row[8]
                if len(row) > 8
                else "1"
            )
        })

    return candles


# =========================================================
# EMA
# =========================================================

def calculate_ema(values, period):

    if len(values) < period:
        return [None] * len(values)

    result = [None] * len(values)

    value = (
        sum(values[:period], Decimal("0"))
        / Decimal(period)
    )

    result[period - 1] = value

    multiplier = (
        Decimal("2")
        / Decimal(period + 1)
    )

    for i in range(period, len(values)):

        value = (
            values[i] * multiplier
            + value * (
                Decimal("1") - multiplier
            )
        )

        result[i] = value

    return result


# =========================================================
# RSI
# =========================================================

def calculate_rsi(values, period):

    result = [None] * len(values)

    if len(values) <= period:
        return result

    gains = []
    losses = []

    for i in range(1, len(values)):

        change = (
            values[i] - values[i - 1]
        )

        gains.append(
            max(change, Decimal("0"))
        )

        losses.append(
            max(-change, Decimal("0"))
        )

    avg_gain = (
        sum(gains[:period], Decimal("0"))
        / Decimal(period)
    )

    avg_loss = (
        sum(losses[:period], Decimal("0"))
        / Decimal(period)
    )

    def rsi_value(gain, loss):

        if loss == 0:
            return Decimal("100")

        rs = gain / loss

        return (
            Decimal("100")
            - Decimal("100")
            / (Decimal("1") + rs)
        )

    result[period] = rsi_value(
        avg_gain,
        avg_loss
    )

    for i in range(period, len(gains)):

        avg_gain = (
            avg_gain * (period - 1)
            + gains[i]
        ) / Decimal(period)

        avg_loss = (
            avg_loss * (period - 1)
            + losses[i]
        ) / Decimal(period)

        result[i + 1] = rsi_value(
            avg_gain,
            avg_loss
        )

    return result


# =========================================================
# ATR
# =========================================================

def calculate_atr(candles, period=14):

    if len(candles) <= period:
        return None

    trs = []

    for i in range(1, len(candles)):

        high = candles[i]["high"]
        low = candles[i]["low"]
        previous_close = candles[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        trs.append(tr)

    return (
        sum(trs[-period:], Decimal("0"))
        / Decimal(period)
    )


# =========================================================
# ADX
# =========================================================

def calculate_adx(candles, period=14):

    if len(candles) < period + 2:
        return Decimal("0")

    plus = Decimal("0")
    minus = Decimal("0")
    total_range = Decimal("0")

    start = max(
        1,
        len(candles) - period
    )

    for i in range(start, len(candles)):

        up_move = (
            candles[i]["high"]
            - candles[i - 1]["high"]
        )

        down_move = (
            candles[i - 1]["low"]
            - candles[i]["low"]
        )

        if (
            up_move > down_move
            and up_move > 0
        ):
            plus += up_move

        if (
            down_move > up_move
            and down_move > 0
        ):
            minus += down_move

        total_range += (
            candles[i]["high"]
            - candles[i]["low"]
        )

    if total_range == 0:
        return Decimal("0")

    plus_di = (
        plus / total_range * Decimal("100")
    )

    minus_di = (
        minus / total_range * Decimal("100")
    )

    total = plus_di + minus_di

    if total == 0:
        return Decimal("0")

    return (
        abs(plus_di - minus_di)
        / total
        * Decimal("100")
    )


# =========================================================
# 15M TREND
# =========================================================

def get_trend(symbol):

    candles = get_candles(
        symbol,
        TREND_BAR,
        80
    )

    candles = [
        x for x in candles
        if x["confirm"] == "1"
    ]

    if len(candles) < 22:
        return "flat"

    closes = [
        x["close"] for x in candles
    ]

    ema20 = calculate_ema(
        closes,
        20
    )

    i = len(closes) - 1

    if (
        ema20[i] is None
        or ema20[i - 1] is None
    ):
        return "flat"

    if (
        closes[i] > ema20[i]
        and ema20[i] > ema20[i - 1]
    ):
        return "bull"

    if (
        closes[i] < ema20[i]
        and ema20[i] < ema20[i - 1]
    ):
        return "bear"

    return "flat"


# =========================================================
# ANALYSIS
# =========================================================

def analyze_symbol(symbol):

    candles = get_candles(
        symbol,
        BAR,
        160
    )

    candles = [
        x for x in candles
        if x["confirm"] == "1"
    ]

    if len(candles) < 105:

        return {
            "signal": "NONE",
            "score": 0,
            "reason": "Not enough candles"
        }

    closes = [
        x["close"] for x in candles
    ]

    i = len(candles) - 1

    rsi14 = calculate_rsi(
        closes,
        14
    )

    rsi100 = calculate_rsi(
        closes,
        100
    )

    ema20 = calculate_ema(
        closes,
        20
    )

    atr = calculate_atr(
        candles,
        14
    )

    adx = calculate_adx(
        candles,
        14
    )

    trend15 = get_trend(symbol)

    if (
        rsi14[i] is None
        or rsi100[i] is None
        or ema20[i] is None
        or atr is None
    ):

        return {
            "signal": "NONE",
            "score": 0,
            "reason": "Indicator unavailable"
        }

    average_volume = (
        sum(
            x["volume"]
            for x in candles[-21:-1]
        )
        / Decimal("20")
    )

    volume_ratio = (
        candles[i]["volume"]
        / average_volume
        if average_volume
        else Decimal("0")
    )

    atr_percent = (
        atr
        / closes[i]
        * Decimal("100")
    )

    buy_score = 0
    sell_score = 0

    reasons = []

    # RSI
    if rsi14[i] > rsi100[i]:

        buy_score += 2
        reasons.append("RSI bullish")

    elif rsi14[i] < rsi100[i]:

        sell_score += 2
        reasons.append("RSI bearish")

    # RSI crossover
    if (
        rsi14[i - 1]
        <= rsi100[i - 1]
        and rsi14[i]
        > rsi100[i]
    ):

        buy_score += 1
        reasons.append(
            "RSI bullish crossover"
        )

    elif (
        rsi14[i - 1]
        >= rsi100[i - 1]
        and rsi14[i]
        < rsi100[i]
    ):

        sell_score += 1
        reasons.append(
            "RSI bearish crossover"
        )

    # EMA position
    if closes[i] > ema20[i]:

        buy_score += 1
        reasons.append(
            "Price above EMA20"
        )

    elif closes[i] < ema20[i]:

        sell_score += 1
        reasons.append(
            "Price below EMA20"
        )

    # EMA slope
    if ema20[i] > ema20[i - 1]:

        buy_score += 1
        reasons.append(
            "EMA slope bullish"
        )

    elif ema20[i] < ema20[i - 1]:

        sell_score += 1
        reasons.append(
            "EMA slope bearish"
        )

    # EMA retest
    near_ema = (
        abs(
            closes[i] - ema20[i]
        )
        / closes[i]
        * Decimal("100")
        <= Decimal("0.20")
    )

    bullish_retest = (
        near_ema
        and closes[i] > ema20[i]
        and candles[i]["low"] <= ema20[i]
    )

    bearish_retest = (
        near_ema
        and closes[i] < ema20[i]
        and candles[i]["high"] >= ema20[i]
    )

    if bullish_retest:

        buy_score += 1
        reasons.append(
            "EMA20 bullish retest"
        )

    elif bearish_retest:

        sell_score += 1
        reasons.append(
            "EMA20 bearish retest"
        )

    # ADX
    if adx >= ADX_MIN:

        if buy_score > sell_score:
            buy_score += 1

        elif sell_score > buy_score:
            sell_score += 1

        reasons.append(
            "ADX strength OK"
        )

    else:

        reasons.append(
            "ADX below minimum"
        )

    # Volume
    if volume_ratio >= VOLUME_MULT:

        if buy_score > sell_score:
            buy_score += 1

        elif sell_score > buy_score:
            sell_score += 1

        reasons.append(
            "Volume confirmed"
        )

    else:

        reasons.append(
            "Volume filter not confirmed"
        )

    # ATR
    if atr_percent >= ATR_MIN_PCT:

        if buy_score > sell_score:
            buy_score += 1

        elif sell_score > buy_score:
            sell_score += 1

        reasons.append(
            "ATR volatility OK"
        )

    else:

        reasons.append(
            "ATR too low"
        )

    # 15m trend
    if trend15 == "bull":

        buy_score += 1
        reasons.append(
            "15m bullish trend"
        )

    elif trend15 == "bear":

        sell_score += 1
        reasons.append(
            "15m bearish trend"
        )

    else:

        reasons.append(
            "15m trend flat"
        )

    score = max(
        buy_score,
        sell_score
    )

    signal = "NONE"

    if (
        buy_score > sell_score
        and buy_score >= MIN_SCORE
    ):

        signal = "BUY"

    elif (
        sell_score > buy_score
        and sell_score >= MIN_SCORE
    ):

        signal = "SELL"

    # Never trade against 15m trend
    if (
        trend15 == "bull"
        and signal == "SELL"
    ):

        signal = "NONE"

        reasons.append(
            "Blocked by 15m bullish trend"
        )

    if (
        trend15 == "bear"
        and signal == "BUY"
    ):

        signal = "NONE"

        reasons.append(
            "Blocked by 15m bearish trend"
        )

    return {
        "signal": signal,
        "score": score,
        "buy": buy_score,
        "sell": sell_score,
        "entry": closes[i],
        "rsi14": rsi14[i],
        "rsi100": rsi100[i],
        "ema20": ema20[i],
        "adx": adx,
        "atr_pct": atr_percent,
        "volume_ratio": volume_ratio,
        "trend15": trend15,
        "reason": " | ".join(reasons)
    }


# =========================================================
# INSTRUMENT
# =========================================================

def get_instrument(symbol):

    data = public_get(
        "/api/v5/public/instruments",
        {
            "instType": "SWAP",
            "instId": symbol
        }
    )

    rows = data.get("data", [])

    if not rows:
        raise RuntimeError(
            "Instrument not found: " + symbol
        )

    item = rows[0]

    return {
        "ctVal": dec(item["ctVal"]),
        "lotSz": dec(item["lotSz"]),
        "minSz": dec(item["minSz"]),
        "tickSz": dec(item["tickSz"]),
        "state": item["state"]
    }


# =========================================================
# ORDER SIZE
# =========================================================

def calculate_order_size(symbol, price):

    info = get_instrument(symbol)

    if info["state"] != "live":

        raise RuntimeError(
            "Instrument not live: "
            + info["state"]
        )

    notional = (
        MARGIN_USDT * LEVERAGE
    )

    raw_size = (
        notional
        / (info["ctVal"] * price)
    )

    size = floor_step(
        raw_size,
        info["lotSz"]
    )

    if size < info["minSz"]:

        raise RuntimeError(
            "Order size below minimum | "
            f"calculated={size} "
            f"minSz={info['minSz']} "
            f"lotSz={info['lotSz']} "
            f"ctVal={info['ctVal']}"
        )

    return size, info


# =========================================================
# ACCOUNT
# =========================================================

def get_account_config():

    return private_request(
        "GET",
        "/api/v5/account/config"
    )


def get_positions(symbol=None):

    params = None

    if symbol:
        params = {
            "instId": symbol
        }

    return private_request(
        "GET",
        "/api/v5/account/positions",
        params=params
    )


def get_position(symbol):

    data = get_positions(symbol)

    for position in data.get("data", []):

        try:
            position_size = dec(
                position.get("pos", "0")
            )

        except Exception:

            position_size = Decimal("0")

        if position_size != 0:
            return position

    return None


def has_position(symbol):

    position = get_position(symbol)

    return (
        position is not None,
        position
    )


# =========================================================
# LEVERAGE
# =========================================================

def set_leverage(symbol):

    payload = {
        "instId": symbol,
        "lever": fmt(LEVERAGE),
        "mgnMode": TD_MODE
    }

    return private_request(
        "POST",
        "/api/v5/account/set-leverage",
        payload=payload
    )


# =========================================================
# ALGO ORDERS
# =========================================================

def get_pending_algo_orders(symbol):

    return private_request(
        "GET",
        "/api/v5/trade/orders-algo-pending",
        params={
            "instType": "SWAP",
            "instId": symbol,
            "ordType": "oco"
        }
    )


def cancel_algo_order(symbol, algo_id):

    payload = [
        {
            "instId": symbol,
            "algoId": str(algo_id)
        }
    ]

    return private_request(
        "POST",
        "/api/v5/trade/cancel-algos",
        payload=payload
    )


# =========================================================
# FIND FULL POSITION OCO
# =========================================================

def get_position_protection(symbol):

    position = get_position(symbol)

    if not position:
        return None

    close_algos = position.get(
        "closeOrderAlgo",
        []
    )

    if not isinstance(close_algos, list):
        return None

    for algo in close_algos:

        if (
            str(algo.get("closeFraction", ""))
            == "1"
        ):
            return algo

    return None


# =========================================================
# CANCEL EXISTING PROTECTION
# =========================================================

def cancel_existing_protection(symbol):

    algo_ids = set()

    # First source: pending OCO orders
    try:

        data = get_pending_algo_orders(symbol)

        for row in data.get("data", []):

            algo_id = row.get("algoId")

            if algo_id:
                algo_ids.add(str(algo_id))

    except Exception as error:

        log(
            "PENDING ALGO CHECK WARNING | "
            f"{symbol} | {error}"
        )

    # Second source: position closeOrderAlgo
    try:

        algo = get_position_protection(symbol)

        if algo:

            algo_id = algo.get("algoId")

            if algo_id:
                algo_ids.add(str(algo_id))

    except Exception as error:

        log(
            "POSITION PROTECTION CHECK WARNING | "
            f"{symbol} | {error}"
        )

    for algo_id in algo_ids:

        try:

            cancel_algo_order(
                symbol,
                algo_id
            )

            log(
                "OLD OCO CANCELLED | "
                f"{symbol} | "
                f"algoId={algo_id}"
            )

        except Exception as error:

            log(
                "OCO CANCEL WARNING | "
                f"{symbol} | "
                f"algoId={algo_id} | "
                f"{error}"
            )


# =========================================================
# PRICE CALCULATION
# =========================================================

def calculate_initial_sl_tp(
    side,
    entry,
    tick
):

    if side == "buy":

        sl = (
            entry
            * (
                Decimal("1")
                - SL_PERCENT / Decimal("100")
            )
        )

        tp = (
            entry
            * (
                Decimal("1")
                + TP_PERCENT / Decimal("100")
            )
        )

        sl = floor_step(sl, tick)
        tp = floor_step(tp, tick)

    else:

        sl = (
            entry
            * (
                Decimal("1")
                + SL_PERCENT / Decimal("100")
            )
        )

        tp = (
            entry
            * (
                Decimal("1")
                - TP_PERCENT / Decimal("100")
            )
        )

        sl = ceil_step(sl, tick)
        tp = ceil_step(tp, tick)

    return sl, tp


# =========================================================
# FULL POSITION OCO PROTECTION
# =========================================================

def place_full_position_protection(
    symbol,
    side,
    sl_price,
    tp_price,
    tick
):

    """
    OKX FULL-POSITION OCO

    closeFraction=1
    NO sz
    posSide=net
    reduceOnly=true

    This is the critical protection path.
    """

    if side not in ("buy", "sell"):
        raise RuntimeError(
            "Invalid position side"
        )

    if sl_price <= 0 or tp_price <= 0:
        raise RuntimeError(
            "Invalid SL/TP price"
        )

    if side == "buy":

        sl_price = floor_step(
            sl_price,
            tick
        )

        tp_price = floor_step(
            tp_price,
            tick
        )

        close_side = "sell"

    else:

        sl_price = ceil_step(
            sl_price,
            tick
        )

        tp_price = ceil_step(
            tp_price,
            tick
        )

        close_side = "buy"

    payload = {
        "instId": symbol,
        "tdMode": TD_MODE,
        "side": close_side,
        "posSide": "net",
        "ordType": "oco",
        "reduceOnly": True,
        "closeFraction": "1",

        "tpTriggerPx": fmt(tp_price),
        "tpOrdPx": "-1",
        "tpTriggerPxType": "mark",

        "slTriggerPx": fmt(sl_price),
        "slOrdPx": "-1",
        "slTriggerPxType": "mark",

        "algoClOrdId": (
            "protect"
            + uuid.uuid4().hex[:20]
        )
    }

    # CRITICAL ASSERTIONS
    if "sz" in payload:
        raise RuntimeError(
            "CRITICAL INTERNAL ERROR: "
            "sz must NOT be sent with "
            "closeFraction=1"
        )

    if payload["closeFraction"] != "1":
        raise RuntimeError(
            "CRITICAL INTERNAL ERROR: "
            "closeFraction must be 1"
        )

    if payload["posSide"] != "net":
        raise RuntimeError(
            "CRITICAL INTERNAL ERROR: "
            "posSide must be net"
        )

    if payload["reduceOnly"] is not True:
        raise RuntimeError(
            "CRITICAL INTERNAL ERROR: "
            "reduceOnly must be true"
        )

    log(
        "OCO PROTECTION SUBMIT | "
        f"{symbol} | "
        f"{side.upper()} | "
        f"SL={fmt(sl_price)} | "
        f"TP={fmt(tp_price)}"
    )

    result = private_request(
        "POST",
        "/api/v5/trade/order-algo",
        payload=payload
    )

    rows = result.get("data", [])

    if not rows:
        raise RuntimeError(
            "OKX returned empty protection response"
        )

    row = rows[0]

    s_code = row.get("sCode")

    if s_code not in (
        None,
        "",
        "0"
    ):

        raise RuntimeError(
            "PROTECTION REJECTED | "
            f"sCode={s_code} | "
            f"sMsg={row.get('sMsg')}"
        )

    algo_id = row.get("algoId")

    if not algo_id:
        raise RuntimeError(
            "PROTECTION RESPONSE HAS NO algoId"
        )

    return result


# =========================================================
# PROTECTION VERIFICATION
# =========================================================

def protection_exists(symbol):

    # PRIMARY CHECK:
    # OKX position.closeOrderAlgo
    try:

        algo = get_position_protection(symbol)

        if algo:

            if (
                str(
                    algo.get(
                        "closeFraction",
                        ""
                    )
                ) == "1"
            ):
                return True

    except Exception as error:

        log(
            "POSITION OCO VERIFY WARNING | "
            f"{symbol} | {error}"
        )

    # SECONDARY CHECK:
    # pending OCO list
    try:

        data = get_pending_algo_orders(symbol)

        for row in data.get("data", []):

            if (
                str(
                    row.get(
                        "closeFraction",
                        ""
                    )
                ) == "1"
            ):
                return True

            # Some responses may omit
            # closeFraction. A pending OCO
            # for this instrument is still
            # treated as protection.
            if row.get("algoId"):
                return True

    except Exception as error:

        log(
            "PENDING OCO VERIFY WARNING | "
            f"{symbol} | {error}"
        )

    return False


# =========================================================
# EMERGENCY CLOSE
# =========================================================

def emergency_close(symbol):

    log(
        "EMERGENCY CLOSE | "
        f"{symbol}"
    )

    payload = {
        "instId": symbol,
        "mgnMode": TD_MODE,
        "autoCxl": True
    }

    return private_request(
        "POST",
        "/api/v5/trade/close-position",
        payload=payload
    )


# =========================================================
# PLACE NEW TRADE
# =========================================================

def place_order(symbol, analysis):

    if not AUTO_TRADE:

        return {
            "status": "BLOCKED",
            "reason": "AUTO_TRADE=false"
        }

    if not DEMO:

        return {
            "status": "BLOCKED",
            "reason": "Live trading disabled"
        }

    signal = analysis.get("signal")

    if signal not in ("BUY", "SELL"):

        return {
            "status": "NO_TRADE",
            "reason": "Signal below threshold"
        }

    with order_lock:

        exists, position = has_position(symbol)

        if exists:

            return {
                "status": "BLOCKED",
                "reason": "Existing position"
            }

        side = (
            "buy"
            if signal == "BUY"
            else "sell"
        )

        price = get_ticker(symbol)

        size, info = calculate_order_size(
            symbol,
            price
        )

        set_leverage(symbol)

        payload = {
            "instId": symbol,
            "tdMode": TD_MODE,
            "side": side,
            "ordType": "market",
            "sz": fmt(size),
            "clOrdId": (
                "bot"
                + uuid.uuid4().hex[:24]
            )
        }

        log(
            "ORDER SUBMIT | "
            f"{symbol} | "
            f"{side.upper()} | "
            f"size={fmt(size)}"
        )

        result = private_request(
            "POST",
            "/api/v5/trade/order",
            payload=payload
        )

        rows = result.get("data", [])

        row = (
            rows[0]
            if rows
            else {}
        )

        if row.get("sCode") not in (
            None,
            "",
            "0"
        ):

            raise RuntimeError(
                "ORDER REJECTED | "
                f"sCode={row.get('sCode')} | "
                f"sMsg={row.get('sMsg')}"
            )

        # Wait for confirmed position
        filled_position = None

        for _ in range(10):

            time.sleep(1)

            filled_position = get_position(
                symbol
            )

            if filled_position:
                break

        if not filled_position:

            raise RuntimeError(
                "ENTRY SENT BUT POSITION "
                "WAS NOT CONFIRMED"
            )

        entry = dec(
            filled_position.get("avgPx")
            or filled_position.get(
                "nonSettleAvgPx"
            )
            or price
        )

        sl, tp = calculate_initial_sl_tp(
            side,
            entry,
            info["tickSz"]
        )

        # Remove any stale protection
        cancel_existing_protection(symbol)

        protection = None
        last_error = None

        for attempt in range(1, 4):

            try:

                protection = (
                    place_full_position_protection(
                        symbol,
                        side,
                        sl,
                        tp,
                        info["tickSz"]
                    )
                )

                # Immediately verify
                time.sleep(1)

                if not protection_exists(symbol):

                    raise RuntimeError(
                        "OCO submitted but "
                        "full-position protection "
                        "was not verified"
                    )

                break

            except Exception as error:

                last_error = error

                log(
                    "PROTECTION RETRY | "
                    f"{symbol} | "
                    f"attempt={attempt}/3 | "
                    f"{error}"
                )

                if attempt < 3:
                    time.sleep(
                        PROTECTION_RETRY_SECONDS
                    )

        if protection is None:

            log(
                "CRITICAL PROTECTION FAILURE | "
                f"{symbol} | "
                f"{last_error}"
            )

            try:

                emergency_close(symbol)

                return {
                    "status": "EMERGENCY_CLOSED",
                    "reason": "Protection failed",
                    "error": str(last_error)
                }

            except Exception as close_error:

                raise RuntimeError(
                    "CRITICAL: POSITION OPEN "
                    "BUT PROTECTION FAILED "
                    "AND EMERGENCY CLOSE FAILED | "
                    f"protection={last_error} | "
                    f"close={close_error}"
                )

        with state_lock:

            state.setdefault(
                symbol,
                {}
            )

            state[symbol].update({
                "entry_price": entry,
                "current_sl": sl,
                "current_tp": tp,
                "position_size": dec(
                    filled_position.get(
                        "pos",
                        "0"
                    )
                ),
                "protection": "ACTIVE",
                "protection_algo": protection
            })

        log(
            "TRADE PROTECTED | "
            f"{symbol} | "
            f"{side.upper()} | "
            f"ENTRY={fmt(entry)} | "
            f"SL={fmt(sl)} | "
            f"TP={fmt(tp)}"
        )

        return {
            "status": "ORDER_AND_PROTECTION_ACTIVE",
            "symbol": symbol,
            "side": side,
            "size": fmt(size),
            "entry": fmt(entry),
            "sl": fmt(sl),
            "tp": fmt(tp),
            "ordId": row.get(
                "ordId",
                ""
            )
        }


# =========================================================
# DYNAMIC PROTECTION / TRAILING
# =========================================================

def manage_position_protection(symbol):

    position = get_position(symbol)

    if not position:
        return

    pos_size = dec(
        position.get("pos", "0")
    )

    if pos_size == 0:
        return

    avg_px = dec(
        position.get("avgPx", "0")
    )

    if avg_px <= 0:
        return

    pos_side = position.get(
        "posSide",
        "net"
    )

    if pos_side == "long":

        side = "buy"

    elif pos_side == "short":

        side = "sell"

    else:

        side = (
            "buy"
            if pos_size > 0
            else "sell"
        )

    current_price = get_mark_price(
        symbol
    )

    info = get_instrument(symbol)
    tick = info["tickSz"]

    if side == "buy":

        profit_pct = (
            (current_price - avg_px)
            / avg_px
            * Decimal("100")
        )

    else:

        profit_pct = (
            (avg_px - current_price)
            / avg_px
            * Decimal("100")
        )

    # -----------------------------------------------------
    # READ LOCAL STATE
    # -----------------------------------------------------

    with state_lock:

        saved = state.get(
            symbol,
            {}
        )

        current_sl = saved.get(
            "current_sl"
        )

        current_tp = saved.get(
            "current_tp"
        )

    # -----------------------------------------------------
    # RECOVER STATE AFTER RESTART
    # -----------------------------------------------------

    if current_sl is None:

        current_sl, default_tp = (
            calculate_initial_sl_tp(
                side,
                avg_px,
                tick
            )
        )

    else:

        default_tp = None

    if current_tp is None:

        if default_tp is not None:

            current_tp = default_tp

        else:

            _, current_tp = (
                calculate_initial_sl_tp(
                    side,
                    avg_px,
                    tick
                )
            )

    # -----------------------------------------------------
    # VERIFY CURRENT OKX PROTECTION
    # -----------------------------------------------------

    protection_active = False

    try:

        protection_active = (
            protection_exists(symbol)
        )

    except Exception:
        protection_active = False

    # -----------------------------------------------------
    # CALCULATE NEW SL
    # -----------------------------------------------------

    new_sl = current_sl

    # BREAK EVEN
    if (
        profit_pct
        >= BREAK_EVEN_TRIGGER_PCT
    ):

        if side == "buy":

            break_even_sl = (
                avg_px
                * (
                    Decimal("1")
                    + BREAK_EVEN_OFFSET_PCT
                    / Decimal("100")
                )
            )

            break_even_sl = floor_step(
                break_even_sl,
                tick
            )

            if break_even_sl > new_sl:
                new_sl = break_even_sl

        else:

            break_even_sl = (
                avg_px
                * (
                    Decimal("1")
                    - BREAK_EVEN_OFFSET_PCT
                    / Decimal("100")
                )
            )

            break_even_sl = ceil_step(
                break_even_sl,
                tick
            )

            if break_even_sl < new_sl:
                new_sl = break_even_sl

    # TRAILING
    if (
        profit_pct
        >= TRAIL_START_PCT
    ):

        if side == "buy":

            trail_sl = (
                current_price
                * (
                    Decimal("1")
                    - TRAIL_DISTANCE_PCT
                    / Decimal("100")
                )
            )

            trail_sl = floor_step(
                trail_sl,
                tick
            )

            if trail_sl > new_sl:
                new_sl = trail_sl

        else:

            trail_sl = (
                current_price
                * (
                    Decimal("1")
                    + TRAIL_DISTANCE_PCT
                    / Decimal("100")
                )
            )

            trail_sl = ceil_step(
                trail_sl,
                tick
            )

            if trail_sl < new_sl:
                new_sl = trail_sl

    # -----------------------------------------------------
    # NEVER LOOSEN SL
    # -----------------------------------------------------

    if side == "buy":

        if new_sl < current_sl:
            new_sl = current_sl

    else:

        if new_sl > current_sl:
            new_sl = current_sl

    # -----------------------------------------------------
    # PROTECTION MISSING
    # -----------------------------------------------------

    if not protection_active:

        log(
            "CRITICAL PROTECTION MISSING | "
            f"{symbol} | "
            f"attempting restoration"
        )

        cancel_existing_protection(symbol)

        for attempt in range(1, 4):

            try:

                place_full_position_protection(
                    symbol,
                    side,
                    new_sl,
                    current_tp,
                    tick
                )

                time.sleep(1)

                if protection_exists(symbol):

                    protection_active = True

                    log(
                        "PROTECTION RESTORED | "
                        f"{symbol}"
                    )

                    break

                raise RuntimeError(
                    "Protection submitted "
                    "but verification failed"
                )

            except Exception as error:

                log(
                    "PROTECTION RESTORE RETRY | "
                    f"{symbol} | "
                    f"{attempt}/3 | "
                    f"{error}"
                )

                if attempt < 3:

                    time.sleep(
                        PROTECTION_RETRY_SECONDS
                    )

        if not protection_active:

            log(
                "CRITICAL: RESTORE FAILED | "
                f"{symbol}"
            )

            try:

                emergency_close(symbol)

                log(
                    "EMERGENCY CLOSE EXECUTED | "
                    f"{symbol}"
                )

            except Exception as error:

                log(
                    "EMERGENCY CLOSE FAILED | "
                    f"{symbol} | "
                    f"{error}"
                )

            return

    # -----------------------------------------------------
    # NO SL CHANGE
    # -----------------------------------------------------

    if new_sl == current_sl:

        with state_lock:

            state.setdefault(
                symbol,
                {}
            )

            state[symbol].update({
                "position_size": pos_size,
                "entry_price": avg_px,
                "mark_price": current_price,
                "profit_pct": profit_pct,
                "current_sl": current_sl,
                "current_tp": current_tp,
                "protection": (
                    "ACTIVE"
                    if protection_active
                    else "MISSING"
                )
            })

        return

    # -----------------------------------------------------
    # TRAILING / BE OCO REPLACEMENT
    # -----------------------------------------------------

    log(
        "DYNAMIC OCO UPDATE | "
        f"{symbol} | "
        f"profit={fmt(profit_pct, 4)}% | "
        f"oldSL={fmt(current_sl)} | "
        f"newSL={fmt(new_sl)} | "
        f"TP={fmt(current_tp)}"
    )

    old_sl = current_sl

    # IMPORTANT:
    # Cancel old OCO first.
    cancel_existing_protection(symbol)

    time.sleep(0.5)

    try:

        place_full_position_protection(
            symbol,
            side,
            new_sl,
            current_tp,
            tick
        )

        time.sleep(1)

        if not protection_exists(symbol):

            raise RuntimeError(
                "New trailing OCO submitted "
                "but verification failed"
            )

        with state_lock:

            state.setdefault(
                symbol,
                {}
            )

            state[symbol].update({
                "position_size": pos_size,
                "entry_price": avg_px,
                "mark_price": current_price,
                "profit_pct": profit_pct,
                "current_sl": new_sl,
                "current_tp": current_tp,
                "protection": "ACTIVE"
            })

        log(
            "TRAILING OCO ACTIVE | "
            f"{symbol} | "
            f"SL={fmt(new_sl)} | "
            f"TP={fmt(current_tp)}"
        )

    except Exception as error:

        log(
            "CRITICAL DYNAMIC OCO ERROR | "
            f"{symbol} | "
            f"{error}"
        )

        # Restore previous protection
        try:

            place_full_position_protection(
                symbol,
                side,
                old_sl,
                current_tp,
                tick
            )

            time.sleep(1)

            if protection_exists(symbol):

                log(
                    "OLD OCO RESTORED | "
                    f"{symbol}"
                )

                with state_lock:

                    state.setdefault(
                        symbol,
                        {}
                    )

                    state[symbol].update({
                        "current_sl": old_sl,
                        "current_tp": current_tp,
                        "protection": "ACTIVE"
                    })

                return

            raise RuntimeError(
                "Old protection submitted "
                "but verification failed"
            )

        except Exception as restore_error:

            log(
                "CRITICAL PROTECTION RESTORE FAILED | "
                f"{symbol} | "
                f"{restore_error}"
            )

            try:

                emergency_close(symbol)

                log(
                    "EMERGENCY CLOSE EXECUTED | "
                    f"{symbol}"
                )

            except Exception as close_error:

                log(
                    "EMERGENCY CLOSE FAILED | "
                    f"{symbol} | "
                    f"{close_error}"
                )


# =========================================================
# STARTUP CHECKS
# =========================================================

def startup_checks():

    log("====================================================")
    log("OKX SCALPING BOT - FINAL")
    log("DEMO + AUTO TRADE + OCO + BREAK-EVEN + TRAILING")
    log(f"DEMO={DEMO}")
    log(f"AUTO_TRADE={AUTO_TRADE}")
    log(f"MARGIN=${MARGIN_USDT}")
    log(f"LEVERAGE={LEVERAGE}x")
    log(f"SL={SL_PERCENT}%")
    log(f"TP={TP_PERCENT}%")
    log(
        f"BE_TRIGGER="
        f"{BREAK_EVEN_TRIGGER_PCT}%"
    )
    log(
        f"TRAIL_START="
        f"{TRAIL_START_PCT}%"
    )
    log(
        f"TRAIL_DISTANCE="
        f"{TRAIL_DISTANCE_PCT}%"
    )
    log(f"SYMBOLS={SYMBOLS}")
    log("====================================================")

    try:

        sync_okx_time()

    except Exception as error:

        log(
            "TIME SYNC WARNING | "
            f"{type(error).__name__}: "
            f"{error}"
        )

    # Public API
    try:

        ticker = public_get(
            "/api/v5/market/ticker",
            {
                "instId":
                "BTC-USDT-SWAP"
            }
        )

        price = ticker[
            "data"
        ][0].get(
            "last",
            "-"
        )

        log(
            "OKX MARKET CONNECTED | "
            f"BTC={price}"
        )

        with state_lock:
            state["public_api"] = "CONNECTED"

    except Exception as error:

        log(
            "PUBLIC API ERROR | "
            f"{type(error).__name__}: "
            f"{error}"
        )

        with state_lock:
            state["public_api"] = "ERROR"

    # Private API
    try:

        private_request(
            "GET",
            "/api/v5/account/balance"
        )

        log("OKX PRIVATE API CONNECTED")

        with state_lock:
            state["private_api"] = "CONNECTED"

    except Exception as error:

        log(
            "PRIVATE API ERROR | "
            f"{type(error).__name__}: "
            f"{error}"
        )

        with state_lock:

            state["private_api"] = (
                "ERROR: " + str(error)
            )


# =========================================================
# WORKER
# =========================================================

def worker():

    global worker_started

    worker_started = True

    startup_checks()

    while True:

        for symbol in SYMBOLS:

            try:

                # EXISTING POSITION FIRST
                try:

                    if has_position(symbol)[0]:

                        manage_position_protection(
                            symbol
                        )

                except Exception as protection_error:

                    log(
                        "PROTECTION MANAGEMENT ERROR | "
                        f"{symbol} | "
                        f"{type(protection_error).__name__}: "
                        f"{protection_error}"
                    )

                # ANALYSIS
                with state_lock:

                    state.setdefault(
                        symbol,
                        {}
                    )

                    state[symbol][
                        "last_activity"
                    ] = (
                        "CHECKING " + symbol
                    )

                analysis = analyze_symbol(
                    symbol
                )

                with state_lock:

                    state[symbol].update(
                        analysis
                    )

                    state[symbol][
                        "last_checked"
                    ] = datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )

                log(
                    f"{symbol}: "
                    f"{analysis['signal']} "
                    f"{analysis.get('score', 0)}/10 | "
                    f"BUY={analysis.get('buy', 0)} "
                    f"SELL={analysis.get('sell', 0)} | "
                    f"{analysis.get('reason', '')}"
                )

                # NEW TRADE
                if (
                    analysis["signal"]
                    in ("BUY", "SELL")
                    and
                    analysis["score"]
                    >= MIN_SCORE
                ):

                    try:

                        result = place_order(
                            symbol,
                            analysis
                        )

                        log(
                            "TRADE RESULT | "
                            f"{symbol} | "
                            + json.dumps(
                                result,
                                default=str
                            )
                        )

                        with state_lock:

                            state[symbol][
                                "trade_status"
                            ] = result.get(
                                "status",
                                "UNKNOWN"
                            )

                            state[symbol][
                                "trade_result"
                            ] = result

                    except Exception as error:

                        log(
                            "TRADE ERROR | "
                            f"{symbol} | "
                            f"{type(error).__name__}: "
                            f"{error}"
                        )

                        with state_lock:

                            state[symbol][
                                "trade_status"
                            ] = "ERROR"

                            state[symbol][
                                "trade_error"
                            ] = str(error)

                else:

                    with state_lock:

                        state[symbol][
                            "trade_status"
                        ] = "NO TRADE"

            except Exception as error:

                log(
                    f"{symbol} ERROR | "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

                with state_lock:

                    state.setdefault(
                        symbol,
                        {}
                    )

                    state[symbol][
                        "trade_status"
                    ] = "ERROR"

                    state[symbol][
                        "trade_error"
                    ] = str(error)

        time.sleep(POLL_SECONDS)


# =========================================================
# DASHBOARD
# =========================================================

HTML = """
<!doctype html>
<html>
<head>
<meta name="viewport"
      content="width=device-width,initial-scale=1">
<title>OKX Scalping Bot</title>
<style>
body{
font-family:Arial,sans-serif;
background:#101216;
color:#eee;
margin:0;
padding:14px;
}
h2{margin:0 0 12px}
.card{
background:#1a1e24;
border:1px solid #303640;
border-radius:10px;
padding:10px;
margin-bottom:8px;
}
.wrap{overflow:auto}
table{
width:100%;
border-collapse:collapse;
font-size:12px;
}
th,td{
padding:7px;
border-bottom:1px solid #303640;
text-align:left;
white-space:nowrap;
}
th{
background:#191d23;
position:sticky;
top:0;
}
.buy{color:#43d17a;font-weight:bold}
.sell{color:#ff6262;font-weight:bold}
.active{color:#43d17a;font-weight:bold}
.danger{color:#ff6262;font-weight:bold}
.reason{
white-space:normal;
min-width:300px;
}
</style>
</head>

<body>

<h2>OKX Scalping Bot</h2>

<div id="top"></div>

<div id="activity">
Loading...
</div>

<div class="wrap">

<table>

<thead>
<tr>
<th>Pair</th>
<th>Signal</th>
<th>Score</th>
<th>Entry</th>
<th>Mark</th>
<th>P/L%</th>
<th>SL</th>
<th>TP</th>
<th>Protection</th>
<th>RSI14</th>
<th>RSI100</th>
<th>EMA20</th>
<th>ADX</th>
<th>ATR%</th>
<th>Volume</th>
<th>15m</th>
<th>Status</th>
</tr>
</thead>

<tbody id="rows"></tbody>

</table>

</div>

<script>

function esc(x){
return String(x ?? "-")
.replace(/[&<>"']/g,function(m){
return {
"&":"&amp;",
"<":"&lt;",
">":"&gt;",
'"':"&quot;",
"'":"&#39;"
}[m];
});
}

async function refresh(){

try{

const s =
await fetch("/api/status")
.then(r=>r.json());

document.getElementById("top").innerHTML =
'<div class="card">' +
'Mode: <b>' +
esc(s.demo ? "DEMO" : "LIVE") +
'</b> | Auto: <b>' +
esc(s.auto_trade) +
'</b> | Margin: <b>$' +
esc(s.margin) +
'</b> | Leverage: <b>' +
esc(s.leverage) +
'x</b> | BE: <b>' +
esc(s.be_trigger) +
'%</b> | Trail: <b>' +
esc(s.trail_start) +
'%</b>' +
'</div>' +

'<div class="card">' +
'Public API: <b>' +
esc(s.public_api) +
'</b> | Private API: <b>' +
esc(s.private_api) +
'</b>' +
'</div>';

document.getElementById("activity").textContent =
"Last activity: " +
s.last_activity +
" | Updated: " +
s.updated;

let html="";

for(
const [sym,x]
of Object.entries(s.symbols)
){

const sig=x.signal || "NONE";

const cls =
sig==="BUY"
? "buy"
: sig==="SELL"
? "sell"
: "";

const protection =
x.protection || "NONE";

const pclass =
protection==="ACTIVE"
? "active"
: "danger";

html +=
"<tr>" +

"<td>"+esc(sym)+"</td>" +

'<td class="'+cls+'">'+
esc(sig)+
"</td>" +

"<td>"+
esc(x.score)+
"/10</td>" +

"<td>"+
esc(x.entry_price || x.entry)+
"</td>" +

"<td>"+
esc(x.mark_price)+
"</td>" +

"<td>"+
esc(x.profit_pct)+
"</td>" +

"<td>"+
esc(x.current_sl)+
"</td>" +

"<td>"+
esc(x.current_tp)+
"</td>" +

'<td class="'+pclass+'">'+
esc(protection)+
"</td>" +

"<td>"+
esc(x.rsi14)+
"</td>" +

"<td>"+
esc(x.rsi100)+
"</td>" +

"<td>"+
esc(x.ema20)+
"</td>" +

"<td>"+
esc(x.adx)+
"</td>" +

"<td>"+
esc(x.atr_pct)+
"</td>" +

"<td>"+
esc(x.volume_ratio)+
"</td>" +

"<td>"+
esc(x.trend15)+
"</td>" +

"<td>"+
esc(x.trade_status || "WAITING")+
"</td>" +

"</tr>";
}

document.getElementById("rows").innerHTML =
html;

}catch(e){

document.getElementById("activity")
.textContent =
"Dashboard error: "+e;

}

}

refresh();
setInterval(refresh,5000);

</script>

</body>
</html>
"""


# =========================================================
# WEB ROUTES
# =========================================================

@app.get("/")
def home():

    return Response(
        HTML,
        mimetype="text/html"
    )


@app.get("/api/status")
def api_status():

    with state_lock:

        symbols = {
            key: value.copy()
            for key,value
            in state.items()
            if key in SYMBOLS
        }

        public_api = state.get(
            "public_api",
            "STARTING"
        )

        private_api = state.get(
            "private_api",
            "STARTING"
        )

    activity = "STARTING"

    for symbol in SYMBOLS:

        if symbol in symbols:

            value = symbols[symbol].get(
                "last_activity"
            )

            if value:
                activity = value

    return jsonify({

        "bot":
            "OKX Scalping Bot",

        "status":
            "running"
            if worker_started
            else "starting",

        "demo":
            DEMO,

        "auto_trade":
            AUTO_TRADE,

        "margin":
            str(MARGIN_USDT),

        "leverage":
            str(LEVERAGE),

        "notional":
            str(
                MARGIN_USDT
                * LEVERAGE
            ),

        "sl_percent":
            str(SL_PERCENT),

        "tp_percent":
            str(TP_PERCENT),

        "be_trigger":
            str(
                BREAK_EVEN_TRIGGER_PCT
            ),

        "trail_start":
            str(
                TRAIL_START_PCT
            ),

        "trail_distance":
            str(
                TRAIL_DISTANCE_PCT
            ),

        "public_api":
            public_api,

        "private_api":
            private_api,

        "last_activity":
            activity,

        "updated":
            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

        "symbols":
            symbols
    })


@app.get("/api/health")
def health():

    return jsonify({

        "status": "ok",

        "bot":
            "running"
            if worker_started
            else "starting",

        "demo":
            DEMO,

        "auto_trade":
            AUTO_TRADE
    })


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    worker_thread = threading.Thread(
        target=worker,
        daemon=True
    )

    worker_thread.start()

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
