import os
import time
import json
import hmac
import base64
import hashlib
import threading
import uuid

from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, Response
from dotenv import load_dotenv

load_dotenv()

# =========================================================
# SETTINGS
# =========================================================

BASE_URL = os.getenv(
    "OKX_BASE_URL",
    "https://www.okx.com"
).rstrip("/")

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

TD_MODE = os.getenv("TD_MODE", "cross")

# =========================================================
# DYNAMIC PROTECTION SETTINGS
# =========================================================

# Break-even activates after this price move.
BREAK_EVEN_TRIGGER_PERCENT = Decimal(
    os.getenv("BREAK_EVEN_TRIGGER_PERCENT", "0.45")
)

# Move SL slightly beyond entry to cover some costs.
BREAK_EVEN_OFFSET_PERCENT = Decimal(
    os.getenv("BREAK_EVEN_OFFSET_PERCENT", "0.05")
)

# Trailing starts after this profit.
TRAIL_START_PERCENT = Decimal(
    os.getenv("TRAIL_START_PERCENT", "0.70")
)

# Distance between current price and trailing SL.
TRAIL_DISTANCE_PERCENT = Decimal(
    os.getenv("TRAIL_DISTANCE_PERCENT", "0.35")
)

# Do not move SL for every tiny price change.
TRAIL_STEP_PERCENT = Decimal(
    os.getenv("TRAIL_STEP_PERCENT", "0.10")
)

# Once this profit is reached, lock at least this profit.
PROFIT_LOCK_TRIGGER_PERCENT = Decimal(
    os.getenv("PROFIT_LOCK_TRIGGER_PERCENT", "1.00")
)

PROFIT_LOCK_PERCENT = Decimal(
    os.getenv("PROFIT_LOCK_PERCENT", "0.35")
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
# APP
# =========================================================

app = Flask(__name__)

session = requests.Session()

state = {}
position_cache = {}

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
        f"{Decimal(str(value)):.{places}f}"
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


# =========================================================
# PUBLIC API
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
            "OKX PUBLIC ERROR "
            f"{data.get('code')}: "
            f"{data.get('msg')}"
        )

    return data


# =========================================================
# TIME SYNC
# =========================================================

def sync_okx_time():

    global server_offset_ms

    local_before = int(time.time() * 1000)

    data = public_get(
        "/api/v5/public/time",
        raw=True
    )

    local_after = int(time.time() * 1000)

    server_ms = int(
        data["data"][0]["ts"]
    )

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
        dt.isoformat(
            timespec="milliseconds"
        )
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

        query = urlencode(
            [
                (
                    str(k),
                    str(v)
                )
                for k, v in params.items()
            ]
        )

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
            "OKX PRIVATE ERROR "
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

    if not data.get("data"):
        raise RuntimeError(
            f"No ticker data: {symbol}"
        )

    return dec(
        data["data"][0]["last"]
    )


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

    for row in reversed(
        data.get("data", [])
    ):

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
        sum(
            values[:period],
            Decimal("0")
        )
        / Decimal(period)
    )

    result[period - 1] = value

    multiplier = (
        Decimal("2")
        / Decimal(period + 1)
    )

    for i in range(
        period,
        len(values)
    ):

        value = (
            values[i] * multiplier
            +
            value * (
                Decimal("1")
                - multiplier
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

    for i in range(
        1,
        len(values)
    ):

        change = (
            values[i]
            - values[i - 1]
        )

        gains.append(
            max(change, Decimal("0"))
        )

        losses.append(
            max(-change, Decimal("0"))
        )

    avg_gain = (
        sum(
            gains[:period],
            Decimal("0")
        )
        / Decimal(period)
    )

    avg_loss = (
        sum(
            losses[:period],
            Decimal("0")
        )
        / Decimal(period)
    )

    def rsi_value(gain, loss):

        if loss == 0:
            return Decimal("100")

        rs = gain / loss

        return (
            Decimal("100")
            -
            Decimal("100")
            /
            (Decimal("1") + rs)
        )

    result[period] = rsi_value(
        avg_gain,
        avg_loss
    )

    for i in range(
        period,
        len(gains)
    ):

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

    for i in range(
        1,
        len(candles)
    ):

        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        trs.append(tr)

    return (
        sum(
            trs[-period:],
            Decimal("0")
        )
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

    for i in range(
        start,
        len(candles)
    ):

        up_move = (
            candles[i]["high"]
            -
            candles[i - 1]["high"]
        )

        down_move = (
            candles[i - 1]["low"]
            -
            candles[i]["low"]
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
            -
            candles[i]["low"]
        )

    if total_range == 0:
        return Decimal("0")

    plus_di = (
        plus
        / total_range
        * Decimal("100")
    )

    minus_di = (
        minus
        / total_range
        * Decimal("100")
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
        x["close"]
        for x in candles
    ]

    ema20 = calculate_ema(
        closes,
        20
    )

    i = len(closes) - 1

    if (
        ema20[i] is None
        or
        ema20[i - 1] is None
    ):
        return "flat"

    if (
        closes[i] > ema20[i]
        and
        ema20[i] > ema20[i - 1]
    ):
        return "bull"

    if (
        closes[i] < ema20[i]
        and
        ema20[i] < ema20[i - 1]
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
        x["close"]
        for x in candles
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
        or
        rsi100[i] is None
        or
        ema20[i] is None
        or
        atr is None
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

    if rsi14[i] > rsi100[i]:
        buy_score += 2
        reasons.append("RSI bullish")

    elif rsi14[i] < rsi100[i]:
        sell_score += 2
        reasons.append("RSI bearish")

    if (
        rsi14[i - 1] <= rsi100[i - 1]
        and
        rsi14[i] > rsi100[i]
    ):
        buy_score += 1
        reasons.append("RSI bullish crossover")

    elif (
        rsi14[i - 1] >= rsi100[i - 1]
        and
        rsi14[i] < rsi100[i]
    ):
        sell_score += 1
        reasons.append("RSI bearish crossover")

    if closes[i] > ema20[i]:
        buy_score += 1
        reasons.append("Price above EMA20")

    elif closes[i] < ema20[i]:
        sell_score += 1
        reasons.append("Price below EMA20")

    if ema20[i] > ema20[i - 1]:
        buy_score += 1
        reasons.append("EMA slope bullish")

    elif ema20[i] < ema20[i - 1]:
        sell_score += 1
        reasons.append("EMA slope bearish")

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
        reasons.append("EMA20 bullish retest")

    elif bearish_retest:
        sell_score += 1
        reasons.append("EMA20 bearish retest")

    if adx >= ADX_MIN:

        if buy_score > sell_score:
            buy_score += 1

        elif sell_score > buy_score:
            sell_score += 1

        reasons.append("ADX strength OK")

    else:
        reasons.append("ADX below minimum")

    if volume_ratio >= VOLUME_MULT:

        if buy_score > sell_score:
            buy_score += 1

        elif sell_score > buy_score:
            sell_score += 1

        reasons.append("Volume confirmed")

    else:
        reasons.append("Volume not confirmed")

    if atr_percent >= ATR_MIN_PCT:

        if buy_score > sell_score:
            buy_score += 1

        elif sell_score > buy_score:
            sell_score += 1

        reasons.append("ATR volatility OK")

    else:
        reasons.append("ATR too low")

    if trend15 == "bull":
        buy_score += 1
        reasons.append("15m bullish trend")

    elif trend15 == "bear":
        sell_score += 1
        reasons.append("15m bearish trend")

    else:
        reasons.append("15m trend flat")

    score = max(
        buy_score,
        sell_score
    )

    signal = "NONE"

    if (
        buy_score > sell_score
        and
        buy_score >= MIN_SCORE
    ):
        signal = "BUY"

    elif (
        sell_score > buy_score
        and
        sell_score >= MIN_SCORE
    ):
        signal = "SELL"

    if (
        trend15 == "bull"
        and
        signal == "SELL"
    ):
        signal = "NONE"
        reasons.append(
            "Blocked by 15m bullish trend"
        )

    if (
        trend15 == "bear"
        and
        signal == "BUY"
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

    if not data.get("data"):
        raise RuntimeError(
            "Instrument not found: " + symbol
        )

    item = data["data"][0]

    return {
        "ctVal": dec(item["ctVal"]),
        "lotSz": dec(item["lotSz"]),
        "minSz": dec(item["minSz"]),
        "tickSz": dec(item["tickSz"]),
        "state": item["state"]
    }


# =========================================================
# POSITION
# =========================================================

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


def get_open_position(symbol):

    data = get_positions(symbol)

    for position in data.get(
        "data",
        []
    ):

        pos = dec(
            position.get(
                "pos",
                "0"
            )
        )

        if pos != 0:

            return position

    return None


def has_position(symbol):

    position = get_open_position(symbol)

    return (
        position is not None,
        position
    )


# =========================================================
# ORDER SIZE
# =========================================================

def calculate_order_size(
    symbol,
    price
):

    info = get_instrument(symbol)

    if info["state"] != "live":

        raise RuntimeError(
            "Instrument not live: "
            + info["state"]
        )

    notional = (
        MARGIN_USDT
        * LEVERAGE
    )

    raw_size = (
        notional
        /
        (
            info["ctVal"]
            * price
        )
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
# ORDER DETAILS
# =========================================================

def get_order_details(
    symbol,
    ord_id
):

    return private_request(
        "GET",
        "/api/v5/trade/order",
        params={
            "instId": symbol,
            "ordId": ord_id
        }
    )


# =========================================================
# EXTRACT ATTACHED ALGO ID
# =========================================================

def find_attach_algo_id(
    symbol,
    ord_id
):

    try:

        data = get_order_details(
            symbol,
            ord_id
        )

        rows = data.get(
            "data",
            []
        )

        if not rows:
            return ""

        order = rows[0]

        attached = order.get(
            "attachAlgoOrds",
            []
        )

        for algo in attached:

            algo_id = (
                algo.get(
                    "attachAlgoId"
                )
                or
                algo.get(
                    "algoId"
                )
                or
                ""
            )

            if algo_id:
                return algo_id

    except Exception as error:

        log(
            "ATTACHED ALGO LOOKUP ERROR | "
            f"{symbol} | "
            f"{error}"
        )

    return ""


# =========================================================
# AMEND ATTACHED SL/TP
# =========================================================

def amend_attached_protection(
    symbol,
    ord_id,
    new_sl,
    new_tp=None
):

    algo_id = find_attach_algo_id(
        symbol,
        ord_id
    )

    if not algo_id:

        raise RuntimeError(
            "Attached protection algoId not found"
        )

    payload = {
        "instId": symbol,
        "ordId": ord_id,
        "attachAlgoOrds": [
            {
                "attachAlgoId": algo_id,
                "newSlTriggerPx": fmt(new_sl),
                "newSlOrdPx": "-1",
                "newSlOrdKind": "condition"
            }
        ]
    }

    if new_tp is not None:

        payload[
            "attachAlgoOrds"
        ][0].update({
            "newTpTriggerPx": fmt(new_tp),
            "newTpOrdPx": "-1",
            "newTpOrdKind": "condition"
        })

    # IMPORTANT:
    # No "sz"
    # No "closeFraction"
    #
    # This avoids the previous OKX 50024 error.

    return private_request(
        "POST",
        "/api/v5/trade/amend-order",
        payload=payload
    )


# =========================================================
# PLACE ORDER
# =========================================================

def place_order(
    symbol,
    analysis
):

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

    if analysis["signal"] not in (
        "BUY",
        "SELL"
    ):

        return {
            "status": "NO_TRADE",
            "reason": "Signal below threshold"
        }

    with order_lock:

        exists, position = has_position(
            symbol
        )

        if exists:

            return {
                "status": "BLOCKED",
                "reason": "Existing position"
            }

        price = analysis["entry"]

        size, info = calculate_order_size(
            symbol,
            price
        )

        set_leverage(symbol)

        side = (
            "buy"
            if analysis["signal"] == "BUY"
            else "sell"
        )

        if side == "buy":

            tp = (
                price
                *
                (
                    Decimal("1")
                    +
                    TP_PERCENT / Decimal("100")
                )
            )

            sl = (
                price
                *
                (
                    Decimal("1")
                    -
                    SL_PERCENT / Decimal("100")
                )
            )

        else:

            tp = (
                price
                *
                (
                    Decimal("1")
                    -
                    TP_PERCENT / Decimal("100")
                )
            )

            sl = (
                price
                *
                (
                    Decimal("1")
                    +
                    SL_PERCENT / Decimal("100")
                )
            )

        tick = info["tickSz"]

        tp = floor_step(
            tp,
            tick
        )

        sl = floor_step(
            sl,
            tick
        )

        cl_id = (
            "bot"
            +
            uuid.uuid4().hex[:24]
        )

        payload = {
            "instId": symbol,
            "tdMode": TD_MODE,
            "side": side,
            "ordType": "market",
            "sz": fmt(size),
            "clOrdId": cl_id,
            "attachAlgoOrds": [
                {
                    "attachAlgoClOrdId":
                        "sl" + uuid.uuid4().hex[:24],

                    "tpTriggerPx":
                        fmt(tp),

                    "tpOrdPx":
                        "-1",

                    "tpTriggerPxType":
                        "mark",

                    "slTriggerPx":
                        fmt(sl),

                    "slOrdPx":
                        "-1",

                    "slTriggerPxType":
                        "mark"
                }
            ]
        }

        log(
            "ORDER SUBMIT | "
            f"{symbol} | "
            f"{side.upper()} | "
            f"size={fmt(size)} | "
            f"SL={fmt(sl)} | "
            f"TP={fmt(tp)}"
        )

        result = private_request(
            "POST",
            "/api/v5/trade/order",
            payload=payload
        )

        rows = result.get(
            "data",
            []
        )

        row = (
            rows[0]
            if rows
            else {}
        )

        if row.get("sCode") not in (
            None,
            "0"
        ):

            raise RuntimeError(
                "ORDER REJECTED | "
                f"sCode={row.get('sCode')} | "
                f"sMsg={row.get('sMsg')}"
            )

        ord_id = row.get(
            "ordId",
            ""
        )

        with state_lock:

            state.setdefault(
                symbol,
                {}
            )

            state[
                symbol
            ].update({

                "position_side":
                    side,

                "entry_price":
                    price,

                "initial_sl":
                    sl,

                "initial_tp":
                    tp,

                "current_sl":
                    sl,

                "current_tp":
                    tp,

                "ord_id":
                    ord_id,

                "protection_stage":
                    "INITIAL",

                "best_price":
                    price,

                "last_trailing_update":
                    price
            })

        return {

            "status":
                "ORDER_SENT",

            "symbol":
                symbol,

            "side":
                side,

            "size":
                fmt(size),

            "sl":
                fmt(sl),

            "tp":
                fmt(tp),

            "ordId":
                ord_id,

            "result":
                result
        }


# =========================================================
# DYNAMIC PROTECTION CALCULATION
# =========================================================

def calculate_dynamic_sl(
    side,
    entry,
    current_price,
    current_sl,
    best_price
):

    if side == "buy":

        profit_percent = (
            (
                current_price
                - entry
            )
            /
            entry
            *
            Decimal("100")
        )

        new_best = max(
            best_price,
            current_price
        )

        candidate = current_sl
        stage = "INITIAL"

        # -----------------------------
        # BREAK EVEN
        # -----------------------------

        if (
            profit_percent
            >=
            BREAK_EVEN_TRIGGER_PERCENT
        ):

            be_sl = (
                entry
                *
                (
                    Decimal("1")
                    +
                    BREAK_EVEN_OFFSET_PERCENT
                    /
                    Decimal("100")
                )
            )

            if be_sl > candidate:
                candidate = be_sl

            stage = "BREAK_EVEN"

        # -----------------------------
        # PROFIT LOCK
        # -----------------------------

        if (
            profit_percent
            >=
            PROFIT_LOCK_TRIGGER_PERCENT
        ):

            lock_sl = (
                entry
                *
                (
                    Decimal("1")
                    +
                    PROFIT_LOCK_PERCENT
                    /
                    Decimal("100")
                )
            )

            if lock_sl > candidate:
                candidate = lock_sl

            stage = "PROFIT_LOCK"

        # -----------------------------
        # TRAILING
        # -----------------------------

        if (
            profit_percent
            >=
            TRAIL_START_PERCENT
        ):

            trail_sl = (
                new_best
                *
                (
                    Decimal("1")
                    -
                    TRAIL_DISTANCE_PERCENT
                    /
                    Decimal("100")
                )
            )

            if trail_sl > candidate:
                candidate = trail_sl

            stage = "TRAILING"

        return (
            candidate,
            new_best,
            stage,
            profit_percent
        )

    # =====================================================
    # SHORT
    # =====================================================

    profit_percent = (
        (
            entry
            - current_price
        )
        /
        entry
        *
        Decimal("100")
    )

    new_best = min(
        best_price,
        current_price
    )

    candidate = current_sl
    stage = "INITIAL"

    if (
        profit_percent
        >=
        BREAK_EVEN_TRIGGER_PERCENT
    ):

        be_sl = (
            entry
            *
            (
                Decimal("1")
                -
                BREAK_EVEN_OFFSET_PERCENT
                /
                Decimal("100")
            )
        )

        if be_sl < candidate:
            candidate = be_sl

        stage = "BREAK_EVEN"

    if (
        profit_percent
        >=
        PROFIT_LOCK_TRIGGER_PERCENT
    ):

        lock_sl = (
            entry
            *
            (
                Decimal("1")
                -
                PROFIT_LOCK_PERCENT
                /
                Decimal("100")
            )
        )

        if lock_sl < candidate:
            candidate = lock_sl

        stage = "PROFIT_LOCK"

    if (
        profit_percent
        >=
        TRAIL_START_PERCENT
    ):

        trail_sl = (
            new_best
            *
            (
                Decimal("1")
                +
                TRAIL_DISTANCE_PERCENT
                /
                Decimal("100")
            )
        )

        if trail_sl < candidate:
            candidate = trail_sl

        stage = "TRAILING"

    return (
        candidate,
        new_best,
        stage,
        profit_percent
    )


# =========================================================
# DYNAMIC PROTECTION MANAGER
# =========================================================

def manage_position_protection(
    symbol
):

    try:

        position = get_open_position(
            symbol
        )

        if position is None:

            with state_lock:

                if symbol in position_cache:
                    position_cache.pop(
                        symbol,
                        None
                    )

                if symbol in state:

                    state[
                        symbol
                    ][
                        "position_status"
                    ] = "NO POSITION"

            return

        pos = dec(
            position.get(
                "pos",
                "0"
            )
        )

        if pos == 0:
            return

        side = (
            "buy"
            if pos > 0
            else "sell"
        )

        entry = dec(
            position.get(
                "avgPx",
                "0"
            )
        )

        if entry <= 0:
            return

        current_price = get_ticker(
            symbol
        )

        info = get_instrument(
            symbol
        )

        tick = info["tickSz"]

        with state_lock:

            local = state.setdefault(
                symbol,
                {}
            )

            old_side = local.get(
                "position_side"
            )

            if old_side != side:

                local.update({

                    "position_side":
                        side,

                    "entry_price":
                        entry,

                    "best_price":
                        entry,

                    "current_sl":
                        local.get(
                            "initial_sl"
                        ),

                    "protection_stage":
                        "INITIAL"
                })

            current_sl = local.get(
                "current_sl"
            )

            if current_sl is None:

                if side == "buy":

                    current_sl = (
                        entry
                        *
                        (
                            Decimal("1")
                            -
                            SL_PERCENT
                            /
                            Decimal("100")
                        )
                    )

                else:

                    current_sl = (
                        entry
                        *
                        (
                            Decimal("1")
                            +
                            SL_PERCENT
                            /
                            Decimal("100")
                        )
                    )

            current_sl = dec(
                current_sl
            )

            best_price = dec(
                local.get(
                    "best_price",
                    entry
                )
            )

        (
            candidate_sl,
            new_best,
            stage,
            profit_percent
        ) = calculate_dynamic_sl(
            side,
            entry,
            current_price,
            current_sl,
            best_price
        )

        candidate_sl = floor_step(
            candidate_sl,
            tick
        )

        # =================================================
        # NEVER MOVE SL BACKWARDS
        # =================================================

        if side == "buy":

            if candidate_sl <= current_sl:

                with state_lock:

                    state[
                        symbol
                    ].update({

                        "best_price":
                            new_best,

                        "profit_percent":
                            profit_percent,

                        "protection_stage":
                            stage,

                        "position_status":
                            "PROTECTED"
                    })

                return

            improvement = (
                (
                    candidate_sl
                    - current_sl
                )
                /
                current_sl
                *
                Decimal("100")
            )

        else:

            if candidate_sl >= current_sl:

                with state_lock:

                    state[
                        symbol
                    ].update({

                        "best_price":
                            new_best,

                        "profit_percent":
                            profit_percent,

                        "protection_stage":
                            stage,

                        "position_status":
                            "PROTECTED"
                    })

                return

            improvement = (
                (
                    current_sl
                    - candidate_sl
                )
                /
                current_sl
                *
                Decimal("100")
            )

        # =================================================
        # TRAIL STEP FILTER
        # =================================================

        if (
            improvement
            <
            TRAIL_STEP_PERCENT
        ):

            with state_lock:

                state[
                    symbol
                ].update({

                    "best_price":
                        new_best,

                    "profit_percent":
                        profit_percent,

                    "protection_stage":
                        stage,

                    "position_status":
                        "WAITING TRAIL STEP"
                })

            return

        # =================================================
        # GET ORDER ID
        # =================================================

        with state_lock:

            ord_id = state[
                symbol
            ].get(
                "ord_id",
                ""
            )

        if not ord_id:

            # We cannot safely amend an unknown attached
            # order. Do NOT blindly create another SL.
            log(
                "PROTECTION WAIT | "
                f"{symbol} | "
                "Missing parent ordId"
            )
            return

        # =================================================
        # AMEND EXISTING ATTACHED SL
        # =================================================

        try:

            amend_result = (
                amend_attached_protection(
                    symbol,
                    ord_id,
                    candidate_sl
                )
            )

            log(
                "DYNAMIC SL UPDATED | "
                f"{symbol} | "
                f"side={side.upper()} | "
                f"price={fmt(current_price)} | "
                f"entry={fmt(entry)} | "
                f"SL={fmt(candidate_sl)} | "
                f"profit={fmt(profit_percent, 3)}% | "
                f"stage={stage}"
            )

            with state_lock:

                state[
                    symbol
                ].update({

                    "current_sl":
                        candidate_sl,

                    "best_price":
                        new_best,

                    "profit_percent":
                        profit_percent,

                    "protection_stage":
                        stage,

                    "last_protection_update":
                        datetime.now().strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),

                    "protection_status":
                        "UPDATED",

                    "protection_result":
                        amend_result
                })

        except Exception as error:

            log(
                "CRITICAL PROTECTION ERROR | "
                f"{symbol} | "
                f"{type(error).__name__}: "
                f"{error}"
            )

            with state_lock:

                state[
                    symbol
                ].update({

                    "protection_status":
                        "ERROR",

                    "protection_error":
                        str(error),

                    "position_status":
                        "PROTECTION ERROR"
                })

    except Exception as error:

        log(
            "PROTECTION MANAGER ERROR | "
            f"{symbol} | "
            f"{type(error).__name__}: "
            f"{error}"
        )

        with state_lock:

            state.setdefault(
                symbol,
                {}
            )

            state[
                symbol
            ][
                "protection_error"
            ] = str(error)


# =========================================================
# PROTECTION WORKER
# =========================================================

def protection_worker():

    while True:

        for symbol in SYMBOLS:

            try:
                manage_position_protection(
                    symbol
                )

            except Exception as error:

                log(
                    "PROTECTION LOOP ERROR | "
                    f"{symbol} | "
                    f"{error}"
                )

        time.sleep(
            max(
                5,
                POLL_SECONDS
            )
        )


# =========================================================
# STARTUP
# =========================================================

def startup_checks():

    log(
        "===================================================="
    )

    log(
        "OKX SCALPING BOT V5 FINAL"
    )

    log(
        "DEMO + AUTO TRADE + DYNAMIC SL/TP"
    )

    log(
        "BREAK-EVEN + PROFIT LOCK + TRAILING SL"
    )

    log(
        f"DEMO={DEMO}"
    )

    log(
        f"AUTO_TRADE={AUTO_TRADE}"
    )

    log(
        f"MARGIN=${MARGIN_USDT}"
    )

    log(
        f"LEVERAGE={LEVERAGE}x"
    )

    log(
        f"INITIAL SL={SL_PERCENT}%"
    )

    log(
        f"INITIAL TP={TP_PERCENT}%"
    )

    log(
        f"BREAK-EVEN={BREAK_EVEN_TRIGGER_PERCENT}%"
    )

    log(
        f"TRAIL START={TRAIL_START_PERCENT}%"
    )

    log(
        f"TRAIL DISTANCE={TRAIL_DISTANCE_PERCENT}%"
    )

    log(
        f"PROFIT LOCK={PROFIT_LOCK_TRIGGER_PERCENT}%"
    )

    log(
        f"SYMBOLS={SYMBOLS}"
    )

    log(
        "===================================================="
    )

    try:

        sync_okx_time()

    except Exception as error:

        log(
            "TIME SYNC WARNING | "
            f"{error}"
        )

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

            state[
                "public_api"
            ] = "CONNECTED"

    except Exception as error:

        log(
            "PUBLIC API ERROR | "
            f"{error}"
        )

        with state_lock:

            state[
                "public_api"
            ] = "ERROR"

    try:

        private_request(
            "GET",
            "/api/v5/account/balance"
        )

        log(
            "OKX PRIVATE API CONNECTED"
        )

        with state_lock:

            state[
                "private_api"
            ] = "CONNECTED"

    except Exception as error:

        log(
            "PRIVATE API ERROR | "
            f"{error}"
        )

        with state_lock:

            state[
                "private_api"
            ] = "ERROR: " + str(error)


# =========================================================
# MAIN TRADING WORKER
# =========================================================

def worker():

    global worker_started

    worker_started = True

    startup_checks()

    while True:

        for symbol in SYMBOLS:

            try:

                with state_lock:

                    state.setdefault(
                        symbol,
                        {}
                    )

                    state[
                        symbol
                    ][
                        "last_activity"
                    ] = (
                        "CHECKING "
                        + symbol
                    )

                analysis = analyze_symbol(
                    symbol
                )

                with state_lock:

                    state[
                        symbol
                    ].update(
                        analysis
                    )

                    state[
                        symbol
                    ][
                        "last_checked"
                    ] = (
                        datetime.now().strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                    )

                log(
                    f"{symbol}: "
                    f"{analysis['signal']} "
                    f"{analysis.get('score', 0)}/10 | "
                    f"BUY={analysis.get('buy', 0)} "
                    f"SELL={analysis.get('sell', 0)} | "
                    f"{analysis.get('reason', '')}"
                )

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
                            +
                            json.dumps(
                                result,
                                default=str
                            )
                        )

                        with state_lock:

                            state[
                                symbol
                            ][
                                "trade_status"
                            ] = result.get(
                                "status",
                                "UNKNOWN"
                            )

                            state[
                                symbol
                            ][
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

                            state[
                                symbol
                            ][
                                "trade_status"
                            ] = "ERROR"

                            state[
                                symbol
                            ][
                                "trade_error"
                            ] = str(error)

                else:

                    with state_lock:

                        state[
                            symbol
                        ][
                            "trade_status"
                        ] = "NO TRADE"

            except Exception as error:

                log(
                    f"{symbol} ANALYSIS ERROR | "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

                with state_lock:

                    state.setdefault(
                        symbol,
                        {}
                    )

                    state[
                        symbol
                    ][
                        "trade_status"
                    ] = "ANALYSIS ERROR"

                    state[
                        symbol
                    ][
                        "trade_error"
                    ] = str(error)

        time.sleep(
            POLL_SECONDS
        )


# =========================================================
# DASHBOARD
# =========================================================

HTML = r"""
<!doctype html>

<html>

<head>

<meta
name="viewport"
content="width=device-width,initial-scale=1"
>

<title>OKX Scalping Bot V5</title>

<style>

body {
    font-family: Arial, sans-serif;
    background: #101216;
    color: #eeeeee;
    margin: 0;
    padding: 14px;
}

h2 {
    margin: 0 0 12px;
}

.top {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
}

.card {
    background: #1a1e24;
    border: 1px solid #303640;
    border-radius: 10px;
    padding: 10px;
}

.ok {
    color: #43d17a;
}

.bad {
    color: #ff6262;
}

.warn {
    color: #ffc857;
}

table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 12px;
    font-size: 12px;
}

th, td {
    padding: 7px;
    border-bottom: 1px solid #303640;
    text-align: left;
    white-space: nowrap;
}

th {
    background: #191d23;
    position: sticky;
    top: 0;
}

.buy {
    color: #43d17a;
    font-weight: bold;
}

.sell {
    color: #ff6262;
    font-weight: bold;
}

.reason {
    white-space: normal;
    min-width: 280px;
}

.wrap {
    overflow: auto;
}

.small {
    font-size: 12px;
    color: #aaaaaa;
    margin-top: 10px;
}

</style>

</head>

<body>

<h2>
OKX Scalping Bot V5 — Dynamic Protection
</h2>

<div id="top" class="top"></div>

<div id="activity" class="small">
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
<th>Price</th>
<th>SL</th>
<th>TP</th>
<th>Profit %</th>
<th>Stage</th>
<th>15m</th>
<th>Status</th>
<th>Protection</th>

</tr>

</thead>

<tbody id="rows">
</tbody>

</table>

</div>

<script>

function esc(x) {

    return String(
        x ?? "-"
    ).replace(
        /[&<>"']/g,
        function(m) {

            return {
                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#39;"
            }[m];

        }
    );

}


async function refresh() {

    try {

        const s =
            await fetch(
                "/api/status"
            ).then(
                r => r.json()
            );

        document.getElementById(
            "top"
        ).innerHTML =

            `<div class="card">
                Mode:
                <b>
                    ${esc(
                        s.demo
                        ? "DEMO"
                        : "LIVE"
                    )}
                </b>
            </div>`

            +

            `<div class="card">
                Auto:
                <b>
                    ${esc(
                        s.auto_trade
                    )}
                </b>
            </div>`

            +

            `<div class="card">
                Margin:
                <b>
                    $${esc(
                        s.margin
                    )}
                </b>
            </div>`

            +

            `<div class="card">
                Leverage:
                <b>
                    ${esc(
                        s.leverage
                    )}x
                </b>
            </div>`

            +

            `<div class="card">
                Initial SL:
                <b>
                    ${esc(
                        s.initial_sl
                    )}%
                </b>
            </div>`

            +

            `<div class="card">
                Trail:
                <b>
                    ${esc(
                        s.trail_distance
                    )}%
                </b>
            </div>`

            +

            `<div class="card">
                Public:
                <b class="${
                    s.public_api ===
                    "CONNECTED"
                    ? "ok"
                    : "bad"
                }">
                    ${esc(
                        s.public_api
                    )}
                </b>
            </div>`

            +

            `<div class="card">
                Private:
                <b class="${
                    s.private_api ===
                    "CONNECTED"
                    ? "ok"
                    : "bad"
                }">
                    ${esc(
                        s.private_api
                    )}
                </b>
            </div>`;


        document.getElementById(
            "activity"
        ).textContent =

            "Last activity: "
            +
            s.last_activity
            +
            " | Updated: "
            +
            s.updated;


        let html = "";


        for (
            const [sym, x]
            of Object.entries(
                s.symbols
            )
        ) {

            const sig =
                x.signal ||
                "NONE";

            const cls =
                sig === "BUY"
                ? "buy"
                :
                sig === "SELL"
                ? "sell"
                :
                "none";


            html +=

                `<tr>

                    <td>
                        ${esc(sym)}
                    </td>

                    <td class="${cls}">
                        ${esc(sig)}
                    </td>

                    <td>
                        ${esc(x.score)}/10
                    </td>

                    <td>
                        ${esc(
                            x.entry_price ||
                            x.entry
                        )}
                    </td>

                    <td>
                        ${esc(
                            x.best_price
                        )}
                    </td>

                    <td>
                        ${esc(
                            x.current_sl
                        )}
                    </td>

                    <td>
                        ${esc(
                            x.current_tp ||
                            x.initial_tp
                        )}
                    </td>

                    <td>
                        ${esc(
                            x.profit_percent
                        )}
                    </td>

                    <td>
                        ${esc(
                            x.protection_stage
                        )}
                    </td>

                    <td>
                        ${esc(
                            x.trend15
                        )}
                    </td>

                    <td>
                        ${esc(
                            x.trade_status ||
                            x.position_status ||
                            "WAITING"
                        )}
                    </td>

                    <td>
                        ${esc(
                            x.protection_status ||
                            "-"
                        )}
                    </td>

                </tr>`;

        }


        document.getElementById(
            "rows"
        ).innerHTML = html;


    } catch (e) {

        document.getElementById(
            "activity"
        ).textContent =
            "Dashboard error: " + e;

    }

}


refresh();

setInterval(
    refresh,
    5000
);

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

            for key, value
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

            if symbols[
                symbol
            ].get(
                "last_activity"
            ):

                activity = symbols[
                    symbol
                ][
                    "last_activity"
                ]

    return jsonify({

        "bot":
            "OKX Scalping Bot V5 FINAL",

        "status":
            (
                "running"
                if worker_started
                else "starting"
            ),

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

        "initial_sl":
            str(SL_PERCENT),

        "initial_tp":
            str(TP_PERCENT),

        "break_even":
            str(
                BREAK_EVEN_TRIGGER_PERCENT
            ),

        "trail_start":
            str(
                TRAIL_START_PERCENT
            ),

        "trail_distance":
            str(
                TRAIL_DISTANCE_PERCENT
            ),

        "profit_lock":
            str(
                PROFIT_LOCK_PERCENT
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

        "status":
            "ok",

        "bot":
            (
                "running"
                if worker_started
                else "starting"
            ),

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

    protection_thread = threading.Thread(
        target=protection_worker,
        daemon=True
    )

    protection_thread.start()

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
