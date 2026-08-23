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

DEMO = os.getenv(
    "OKX_DEMO",
    "true"
).lower() == "true"

AUTO_TRADE = os.getenv(
    "AUTO_TRADE",
    "true"
).lower() == "true"

BAR = os.getenv("BAR", "5m")
TREND_BAR = os.getenv("TREND_BAR", "15m")

MARGIN_USDT = Decimal(
    os.getenv("MARGIN_USDT", "20")
)

LEVERAGE = Decimal(
    os.getenv("LEVERAGE", "5")
)

SL_PERCENT = Decimal(
    os.getenv("SL_PERCENT", "0.4")
)

TP_PERCENT = Decimal(
    os.getenv("TP_PERCENT", "0.8")
)

POLL_SECONDS = int(
    os.getenv("POLL_SECONDS", "20")
)

MIN_SCORE = int(
    os.getenv("MIN_SCORE", "7")
)

ADX_MIN = Decimal(
    os.getenv("ADX_MIN", "18")
)

VOLUME_MULT = Decimal(
    os.getenv("VOLUME_MULT", "0.8")
)

ATR_MIN_PCT = Decimal(
    os.getenv("ATR_MIN_PCT", "0.05")
)

TD_MODE = os.getenv(
    "TD_MODE",
    "cross"
)


# ---------------------------------------------------------
# DYNAMIC PROTECTION
# ---------------------------------------------------------

BREAKEVEN_TRIGGER = Decimal(
    os.getenv(
        "BREAKEVEN_TRIGGER",
        "0.30"
    )
)

BREAKEVEN_OFFSET = Decimal(
    os.getenv(
        "BREAKEVEN_OFFSET",
        "0.03"
    )
)

TRAIL_TRIGGER = Decimal(
    os.getenv(
        "TRAIL_TRIGGER",
        "0.45"
    )
)

TRAIL_DISTANCE = Decimal(
    os.getenv(
        "TRAIL_DISTANCE",
        "0.25"
    )
)

TRAIL_MIN_MOVE = Decimal(
    os.getenv(
        "TRAIL_MIN_MOVE",
        "0.05"
    )
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
# DECIMAL
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

    before = int(
        time.time() * 1000
    )

    data = public_get(
        "/api/v5/public/time",
        raw=True
    )

    after = int(
        time.time() * 1000
    )

    server_ms = int(
        data["data"][0]["ts"]
    )

    local_mid = (
        before + after
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
        .replace(
            "+00:00",
            "Z"
        )
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
        SECRET_KEY.encode(),
        prehash.encode(),
        hashlib.sha256
    ).digest()

    return base64.b64encode(
        digest
    ).decode()


# =========================================================
# PRIVATE API
# =========================================================

def private_request(
    method,
    path,
    payload=None,
    params=None
):

    if not API_KEY:
        raise RuntimeError(
            "OKX_API_KEY missing"
        )

    if not SECRET_KEY:
        raise RuntimeError(
            "OKX_SECRET_KEY missing"
        )

    if not PASSPHRASE:
        raise RuntimeError(
            "OKX_PASSPHRASE missing"
        )

    method = method.upper()

    request_path = path

    if params:

        query = "&".join(
            f"{key}={value}"
            for key, value in params.items()
        )

        request_path += (
            "?"
            + query
        )

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
        "Content-Type":
            "application/json",

        "OK-ACCESS-KEY":
            API_KEY,

        "OK-ACCESS-SIGN":
            signature,

        "OK-ACCESS-PASSPHRASE":
            PASSPHRASE,

        "OK-ACCESS-TIMESTAMP":
            timestamp
    }

    if DEMO:

        headers[
            "x-simulated-trading"
        ] = "1"

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
        data = {
            "raw": response.text
        }

    if response.status_code >= 400:

        raise RuntimeError(
            f"OKX HTTP "
            f"{response.status_code}: "
            f"{data}"
        )

    if data.get("code") != "0":

        raise RuntimeError(
            f"OKX PRIVATE ERROR "
            f"{data.get('code')}: "
            f"{data.get('msg')}"
        )

    return data


# =========================================================
# MARKET
# =========================================================

def get_candles(
    symbol,
    bar,
    limit=160
):

    data = public_get(
        "/api/v5/market/candles",
        {
            "instId": symbol,
            "bar": bar,
            "limit": str(limit)
        }
    )

    result = []

    for row in reversed(
        data.get("data", [])
    ):

        result.append({

            "ts": int(row[0]),

            "open": dec(row[1]),

            "high": dec(row[2]),

            "low": dec(row[3]),

            "close": dec(row[4]),

            "volume": dec(row[5]),

            "confirm":
                row[8]
                if len(row) > 8
                else "1"
        })

    return result


# =========================================================
# INDICATORS
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
            value *
            (
                Decimal("1")
                - multiplier
            )
        )

        result[i] = value

    return result


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
            max(
                change,
                Decimal("0")
            )
        )

        losses.append(
            max(
                -change,
                Decimal("0")
            )
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

    def value(gain, loss):

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

    result[period] = value(
        avg_gain,
        avg_loss
    )

    for i in range(
        period,
        len(gains)
    ):

        avg_gain = (
            avg_gain *
            (period - 1)
            + gains[i]
        ) / Decimal(period)

        avg_loss = (
            avg_loss *
            (period - 1)
            + losses[i]
        ) / Decimal(period)

        result[i + 1] = value(
            avg_gain,
            avg_loss
        )

    return result


def calculate_atr(
    candles,
    period=14
):

    if len(candles) <= period:
        return None

    trs = []

    for i in range(
        1,
        len(candles)
    ):

        high = candles[i]["high"]
        low = candles[i]["low"]
        previous = candles[i - 1]["close"]

        trs.append(
            max(
                high - low,
                abs(high - previous),
                abs(low - previous)
            )
        )

    return (
        sum(
            trs[-period:],
            Decimal("0")
        )
        / Decimal(period)
    )


def calculate_adx(
    candles,
    period=14
):

    if len(candles) < period + 2:
        return Decimal("0")

    plus = Decimal("0")
    minus = Decimal("0")
    total = Decimal("0")

    start = max(
        1,
        len(candles) - period
    )

    for i in range(
        start,
        len(candles)
    ):

        up = (
            candles[i]["high"]
            - candles[i - 1]["high"]
        )

        down = (
            candles[i - 1]["low"]
            - candles[i]["low"]
        )

        if up > down and up > 0:
            plus += up

        if down > up and down > 0:
            minus += down

        total += (
            candles[i]["high"]
            - candles[i]["low"]
        )

    if total == 0:
        return Decimal("0")

    plus_di = (
        plus / total
        * Decimal("100")
    )

    minus_di = (
        minus / total
        * Decimal("100")
    )

    denominator = (
        plus_di + minus_di
    )

    if denominator == 0:
        return Decimal("0")

    return (
        abs(
            plus_di - minus_di
        )
        / denominator
        * Decimal("100")
    )


# =========================================================
# TREND
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

    ema = calculate_ema(
        closes,
        20
    )

    i = len(closes) - 1

    if (
        ema[i] is None
        or
        ema[i - 1] is None
    ):
        return "flat"

    if (
        closes[i] > ema[i]
        and
        ema[i] > ema[i - 1]
    ):
        return "bull"

    if (
        closes[i] < ema[i]
        and
        ema[i] < ema[i - 1]
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

    buy = 0
    sell = 0
    reasons = []

    if rsi14[i] > rsi100[i]:

        buy += 2
        reasons.append("RSI bullish")

    elif rsi14[i] < rsi100[i]:

        sell += 2
        reasons.append("RSI bearish")

    if (
        rsi14[i - 1]
        <= rsi100[i - 1]
        and
        rsi14[i]
        > rsi100[i]
    ):

        buy += 1

    elif (
        rsi14[i - 1]
        >= rsi100[i - 1]
        and
        rsi14[i]
        < rsi100[i]
    ):

        sell += 1

    if closes[i] > ema20[i]:

        buy += 1
        reasons.append("Above EMA20")

    elif closes[i] < ema20[i]:

        sell += 1
        reasons.append("Below EMA20")

    if ema20[i] > ema20[i - 1]:

        buy += 1

    elif ema20[i] < ema20[i - 1]:

        sell += 1

    if adx >= ADX_MIN:

        if buy > sell:
            buy += 1

        elif sell > buy:
            sell += 1

        reasons.append("ADX OK")

    else:

        reasons.append("ADX weak")

    if volume_ratio >= VOLUME_MULT:

        if buy > sell:
            buy += 1

        elif sell > buy:
            sell += 1

        reasons.append("Volume OK")

    if atr_percent >= ATR_MIN_PCT:

        if buy > sell:
            buy += 1

        elif sell > buy:
            sell += 1

        reasons.append("ATR OK")

    if trend15 == "bull":

        buy += 1
        reasons.append("15m bull")

    elif trend15 == "bear":

        sell += 1
        reasons.append("15m bear")

    score = max(
        buy,
        sell
    )

    signal = "NONE"

    if (
        buy > sell
        and
        buy >= MIN_SCORE
        and
        trend15 != "bear"
    ):

        signal = "BUY"

    elif (
        sell > buy
        and
        sell >= MIN_SCORE
        and
        trend15 != "bull"
    ):

        signal = "SELL"

    return {

        "signal": signal,

        "score": score,

        "buy": buy,

        "sell": sell,

        "entry": closes[i],

        "rsi14": rsi14[i],

        "rsi100": rsi100[i],

        "ema20": ema20[i],

        "adx": adx,

        "atr_pct": atr_percent,

        "volume_ratio":
            volume_ratio,

        "trend15":
            trend15,

        "reason":
            " | ".join(reasons)
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
            "Instrument not found: "
            + symbol
        )

    item = data["data"][0]

    return {

        "ctVal":
            dec(item["ctVal"]),

        "lotSz":
            dec(item["lotSz"]),

        "minSz":
            dec(item["minSz"]),

        "tickSz":
            dec(item["tickSz"]),

        "state":
            item["state"]
    }


# =========================================================
# POSITION
# =========================================================

def get_positions(symbol=None):

    params = {}

    if symbol:
        params["instId"] = symbol

    return private_request(
        "GET",
        "/api/v5/account/positions",
        params=params
    )


def get_position(symbol):

    data = get_positions(symbol)

    for p in data.get("data", []):

        pos = dec(
            p.get("pos", "0")
        )

        if pos != 0:

            return p

    return None


def has_position(symbol):

    p = get_position(symbol)

    return (
        p is not None,
        p
    )


# =========================================================
# LEVERAGE
# =========================================================

def set_leverage(symbol):

    return private_request(
        "POST",
        "/api/v5/account/set-leverage",
        payload={

            "instId": symbol,

            "lever": fmt(
                LEVERAGE
            ),

            "mgnMode": TD_MODE
        }
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
            f"size={size} "
            f"min={info['minSz']}"
        )

    return size, info


# =========================================================
# PLACE MARKET ORDER
# =========================================================

def place_market_order(
    symbol,
    analysis
):

    side = (
        "buy"
        if analysis["signal"] == "BUY"
        else "sell"
    )

    price = analysis["entry"]

    size, info = (
        calculate_order_size(
            symbol,
            price
        )
    )

    set_leverage(symbol)

    payload = {

        "instId":
            symbol,

        "tdMode":
            TD_MODE,

        "side":
            side,

        "ordType":
            "market",

        "sz":
            fmt(size),

        "clOrdId":
            "bot"
            + uuid.uuid4()
            .hex[:24]
    }

    log(
        "ENTRY ORDER | "
        f"{symbol} | "
        f"{side.upper()} | "
        f"sz={fmt(size)}"
    )

    result = private_request(
        "POST",
        "/api/v5/trade/order",
        payload=payload
    )

    row = (
        result.get("data", [{}])[0]
    )

    if row.get("sCode") not in (
        None,
        "0"
    ):

        raise RuntimeError(
            "ENTRY REJECTED | "
            f"{row.get('sCode')} | "
            f"{row.get('sMsg')}"
        )

    return {
        "ordId":
            row.get("ordId", ""),

        "side":
            side,

        "size":
            size,

        "info":
            info
    }


# =========================================================
# CREATE TP/SL PROTECTION
#
# IMPORTANT:
# Only sz is sent.
# closeFraction is NEVER sent.
# =========================================================

def create_protection(
    symbol,
    position
):

    pos_size = abs(
        dec(
            position.get(
                "pos",
                "0"
            )
        )
    )

    if pos_size <= 0:

        raise RuntimeError(
            "Cannot protect empty position"
        )

    entry = dec(
        position.get(
            "avgPx",
            position.get(
                "nonSettleAvgPx",
                "0"
            )
        )
    )

    if entry <= 0:

        raise RuntimeError(
            "Invalid position entry price"
        )

    pos_side = (
        position.get(
            "posSide",
            "net"
        )
        or
        "net"
    )

    # In NET mode, positive/negative pos determines direction.
    if pos_side == "net":

        raw_pos = dec(
            position.get(
                "pos",
                "0"
            )
        )

        side = (
            "buy"
            if raw_pos > 0
            else "sell"
        )

    else:

        side = (
            "buy"
            if pos_side == "long"
            else "sell"
        )

    info = get_instrument(symbol)

    tick = info["tickSz"]

    if side == "buy":

        sl = (
            entry
            *
            (
                Decimal("1")
                -
                SL_PERCENT
                / Decimal("100")
            )
        )

        tp = (
            entry
            *
            (
                Decimal("1")
                +
                TP_PERCENT
                / Decimal("100")
            )
        )

    else:

        sl = (
            entry
            *
            (
                Decimal("1")
                +
                SL_PERCENT
                / Decimal("100")
            )
        )

        tp = (
            entry
            *
            (
                Decimal("1")
                -
                TP_PERCENT
                / Decimal("100")
            )
        )

    sl = floor_step(sl, tick)
    tp = floor_step(tp, tick)

    payload = {

        "instId":
            symbol,

        "tdMode":
            TD_MODE,

        "side":
            side,

        "posSide":
            "net",

        "ordType":
            "conditional",

        "sz":
            fmt(pos_size),

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
            "mark",

        "algoClOrdId":
            "prot"
            + uuid.uuid4()
            .hex[:24]
    }

    log(
        "PROTECTION CREATE | "
        f"{symbol} | "
        f"side={side} | "
        f"SL={fmt(sl)} | "
        f"TP={fmt(tp)} | "
        f"sz={fmt(pos_size)}"
    )

    result = private_request(
        "POST",
        "/api/v5/trade/order-algo",
        payload=payload
    )

    row = (
        result.get("data", [{}])[0]
    )

    if row.get("sCode") not in (
        None,
        "0"
    ):

        raise RuntimeError(
            "PROTECTION REJECTED | "
            f"{row.get('sCode')} | "
            f"{row.get('sMsg')}"
        )

    algo_id = row.get(
        "algoId",
        ""
    )

    with state_lock:

        state.setdefault(
            symbol,
            {}
        )

        state[symbol].update({

            "protection":
                "ACTIVE",

            "protection_algo":
                algo_id,

            "protection_sl":
                sl,

            "protection_tp":
                tp,

            "protection_entry":
                entry,

            "protection_stage":
                "INITIAL"
        })

    return result


# =========================================================
# FIND PROTECTION ALGO
# =========================================================

def find_protection_algo(symbol):

    data = private_request(
        "GET",
        "/api/v5/trade/orders-algo-pending",
        params={

            "ordType":
                "conditional",

            "instId":
                symbol,

            "limit":
                "100"
        }
    )

    rows = data.get(
        "data",
        []
    )

    position = get_position(symbol)

    if not position:
        return None

    pos = dec(
        position.get(
            "pos",
            "0"
        )
    )

    expected_side = (
        "buy"
        if pos > 0
        else "sell"
    )

    candidates = []

    for row in rows:

        if row.get(
            "instId"
        ) != symbol:

            continue

        if row.get(
            "state"
        ) not in (
            "live",
            "effective"
        ):

            continue

        row_side = row.get(
            "side",
            ""
        )

        if row_side != expected_side:
            continue

        if (
            row.get("slTriggerPx")
            or
            row.get("tpTriggerPx")
        ):

            candidates.append(row)

    if not candidates:
        return None

    # Newest/highest algo ID is normally the newest.
    candidates.sort(
        key=lambda x:
        str(x.get("algoId", ""))
    )

    return candidates[-1]


# =========================================================
# ENSURE PROTECTION
# =========================================================

def ensure_protection(symbol):

    position = get_position(symbol)

    if not position:

        with state_lock:

            if symbol in state:

                state[symbol].update({

                    "protection":
                        "NO POSITION",

                    "protection_algo":
                        "",

                    "protection_stage":
                        "-"
                })

        return True

    algo = find_protection_algo(symbol)

    if algo:

        algo_id = algo.get(
            "algoId",
            ""
        )

        with state_lock:

            state.setdefault(
                symbol,
                {}
            )

            state[symbol].update({

                "protection":
                    "ACTIVE",

                "protection_algo":
                    algo_id,

                "protection_sl":
                    dec(
                        algo.get(
                            "slTriggerPx",
                            "0"
                        )
                    )
                    if algo.get(
                        "slTriggerPx"
                    )
                    else None,

                "protection_tp":
                    dec(
                        algo.get(
                            "tpTriggerPx",
                            "0"
                        )
                    )
                    if algo.get(
                        "tpTriggerPx"
                    )
                    else None
            })

        return True

    # No protection found.
    log(
        "PROTECTION MISSING | "
        f"{symbol} | creating emergency protection"
    )

    create_protection(
        symbol,
        position
    )

    return True


# =========================================================
# DYNAMIC TRAILING SL
#
# This does NOT use move_order_stop amendment.
# It amends the supported conditional SL.
# =========================================================

def update_dynamic_protection(
    symbol,
    position
):

    if not position:
        return

    algo = find_protection_algo(
        symbol
    )

    if not algo:

        log(
            "TRAILING BLOCKED | "
            f"{symbol} | protection algo not found"
        )

        ensure_protection(symbol)

        return

    algo_id = algo.get(
        "algoId",
        ""
    )

    if not algo_id:
        return

    pos = dec(
        position.get(
            "pos",
            "0"
        )
    )

    entry = dec(
        position.get(
            "avgPx",
            position.get(
                "nonSettleAvgPx",
                "0"
            )
        )
    )

    mark = dec(
        position.get(
            "markPx",
            "0"
        )
    )

    if entry <= 0 or mark <= 0:
        return

    info = get_instrument(symbol)
    tick = info["tickSz"]

    if pos > 0:

        profit_pct = (
            (
                mark - entry
            )
            / entry
            * Decimal("100")
        )

        # ---------------------------------------------
        # BREAK EVEN
        # ---------------------------------------------

        if profit_pct >= BREAKEVEN_TRIGGER:

            be_sl = (
                entry
                *
                (
                    Decimal("1")
                    +
                    BREAKEVEN_OFFSET
                    / Decimal("100")
                )
            )

            current_sl = (
                dec(
                    algo.get(
                        "slTriggerPx",
                        "0"
                    )
                )
                if algo.get(
                    "slTriggerPx"
                )
                else Decimal("0")
            )

            if be_sl > current_sl:

                new_sl = floor_step(
                    be_sl,
                    tick
                )

                amend_sl(
                    symbol,
                    algo_id,
                    new_sl
                )

                log(
                    "BREAK-EVEN SL | "
                    f"{symbol} | "
                    f"SL={fmt(new_sl)}"
                )

                current_sl = new_sl

        # ---------------------------------------------
        # TRAILING
        # ---------------------------------------------

        if profit_pct >= TRAIL_TRIGGER:

            trail_sl = (
                mark
                *
                (
                    Decimal("1")
                    -
                    TRAIL_DISTANCE
                    / Decimal("100")
                )
            )

            current_sl = (
                dec(
                    algo.get(
                        "slTriggerPx",
                        "0"
                    )
                )
                if algo.get(
                    "slTriggerPx"
                )
                else Decimal("0")
            )

            # Never move SL backwards.
            if trail_sl > current_sl:

                movement = (
                    (
                        trail_sl
                        - current_sl
                    )
                    / entry
                    * Decimal("100")
                )

                if movement >= TRAIL_MIN_MOVE:

                    new_sl = floor_step(
                        trail_sl,
                        tick
                    )

                    amend_sl(
                        symbol,
                        algo_id,
                        new_sl
                    )

                    log(
                        "TRAILING SL UP | "
                        f"{symbol} | "
                        f"mark={fmt(mark)} | "
                        f"SL={fmt(new_sl)}"
                    )

    else:

        profit_pct = (
            (
                entry - mark
            )
            / entry
            * Decimal("100")
        )

        # ---------------------------------------------
        # BREAK EVEN SHORT
        # ---------------------------------------------

        if profit_pct >= BREAKEVEN_TRIGGER:

            be_sl = (
                entry
                *
                (
                    Decimal("1")
                    -
                    BREAKEVEN_OFFSET
                    / Decimal("100")
                )
            )

            current_sl = (
                dec(
                    algo.get(
                        "slTriggerPx",
                        "0"
                    )
                )
                if algo.get(
                    "slTriggerPx"
                )
                else Decimal("999999999")
            )

            if be_sl < current_sl:

                new_sl = floor_step(
                    be_sl,
                    tick
                )

                amend_sl(
                    symbol,
                    algo_id,
                    new_sl
                )

                log(
                    "BREAK-EVEN SL SHORT | "
                    f"{symbol} | "
                    f"SL={fmt(new_sl)}"
                )

        # ---------------------------------------------
        # TRAILING SHORT
        # ---------------------------------------------

        if profit_pct >= TRAIL_TRIGGER:

            trail_sl = (
                mark
                *
                (
                    Decimal("1")
                    +
                    TRAIL_DISTANCE
                    / Decimal("100")
                )
            )

            current_sl = (
                dec(
                    algo.get(
                        "slTriggerPx",
                        "0"
                    )
                )
                if algo.get(
                    "slTriggerPx"
                )
                else Decimal("999999999")
            )

            # Never move SL backwards.
            if trail_sl < current_sl:

                movement = (
                    (
                        current_sl
                        - trail_sl
                    )
                    / entry
                    * Decimal("100")
                )

                if movement >= TRAIL_MIN_MOVE:

                    new_sl = floor_step(
                        trail_sl,
                        tick
                    )

                    amend_sl(
                        symbol,
                        algo_id,
                        new_sl
                    )

                    log(
                        "TRAILING SL DOWN | "
                        f"{symbol} | "
                        f"mark={fmt(mark)} | "
                        f"SL={fmt(new_sl)}"
                    )


# =========================================================
# AMEND EXISTING CONDITIONAL SL
# =========================================================

def amend_sl(
    symbol,
    algo_id,
    new_sl
):

    if not algo_id:
        return False

    payload = {

        "algoId":
            algo_id,

        "instId":
            symbol,

        "newSlTriggerPx":
            fmt(new_sl),

        "newSlOrdPx":
            "-1",

        "newSlTriggerPxType":
            "mark",

        # IMPORTANT:
        # Never cancel the existing algo
        # automatically if amendment fails.
        "cxlOnFail":
            False
    }

    try:

        result = private_request(
            "POST",
            "/api/v5/trade/amend-algos",
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

        if row.get(
            "sCode"
        ) not in (
            None,
            "0"
        ):

            raise RuntimeError(
                "SL AMEND REJECTED | "
                f"{row.get('sCode')} | "
                f"{row.get('sMsg')}"
            )

        with state_lock:

            state.setdefault(
                symbol,
                {}
            )

            state[symbol][
                "protection_sl"
            ] = new_sl

            state[symbol][
                "protection_stage"
            ] = "TRAILING"

        return True

    except Exception as error:

        log(
            "SL AMEND ERROR | "
            f"{symbol} | "
            f"{type(error).__name__}: "
            f"{error}"
        )

        # IMPORTANT:
        # Do NOT cancel protection on amend failure.
        return False


# =========================================================
# PROTECTION WORKER
# =========================================================

def protection_worker():

    while True:

        for symbol in SYMBOLS:

            try:

                position = get_position(
                    symbol
                )

                if position:

                    ensure_protection(
                        symbol
                    )

                    # Re-read after ensuring.
                    position = get_position(
                        symbol
                    )

                    if position:

                        update_dynamic_protection(
                            symbol,
                            position
                        )

                else:

                    with state_lock:

                        if symbol in state:

                            state[symbol].update({

                                "protection":
                                    "NO POSITION",

                                "protection_algo":
                                    "",

                                "protection_stage":
                                    "-"
                            })

            except Exception as error:

                log(
                    "CRITICAL PROTECTION ERROR | "
                    f"{symbol} | "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

                with state_lock:

                    state.setdefault(
                        symbol,
                        {}
                    )

                    state[symbol][
                        "protection"
                    ] = "ERROR"

                    state[symbol][
                        "protection_error"
                    ] = str(error)

        time.sleep(
            max(
                5,
                min(
                    POLL_SECONDS,
                    10
                )
            )
        )


# =========================================================
# TRADE ENGINE
# =========================================================

def trade_symbol(
    symbol,
    analysis
):

    if not AUTO_TRADE:

        return {
            "status":
                "BLOCKED",
            "reason":
                "AUTO_TRADE=false"
        }

    if not DEMO:

        return {
            "status":
                "BLOCKED",
            "reason":
                "Live trading disabled"
        }

    if analysis.get(
        "signal"
    ) not in (
        "BUY",
        "SELL"
    ):

        return {
            "status":
                "NO_TRADE",
            "reason":
                "No valid signal"
        }

    with order_lock:

        exists, position = has_position(
            symbol
        )

        if exists:

            return {
                "status":
                    "BLOCKED",
                "reason":
                    "Existing position"
            }

        entry = place_market_order(
            symbol,
            analysis
        )

        # Give OKX a short moment to register
        # the position before protection is created.
        time.sleep(0.7)

        position = get_position(
            symbol
        )

        if not position:

            raise RuntimeError(
                "ENTRY FILLED/ACCEPTED BUT "
                "POSITION NOT YET VISIBLE"
            )

        protection = create_protection(
            symbol,
            position
        )

        return {

            "status":
                "ORDER_AND_PROTECTION_OK",

            "symbol":
                symbol,

            "entry_order":
                entry,

            "protection":
                protection
        }


# =========================================================
# STARTUP
# =========================================================

def startup_checks():

    log(
        "================================================"
    )

    log(
        "OKX SCALPING BOT V6"
    )

    log(
        "DYNAMIC SL + BREAK-EVEN + TRAILING"
    )

    log(
        f"DEMO={DEMO}"
    )

    log(
        f"AUTO_TRADE={AUTO_TRADE}"
    )

    log(
        f"MARGIN={MARGIN_USDT}"
    )

    log(
        f"LEVERAGE={LEVERAGE}x"
    )

    log(
        f"SL={SL_PERCENT}%"
    )

    log(
        f"TP={TP_PERCENT}%"
    )

    log(
        f"BE_TRIGGER={BREAKEVEN_TRIGGER}%"
    )

    log(
        f"TRAIL_TRIGGER={TRAIL_TRIGGER}%"
    )

    log(
        f"TRAIL_DISTANCE={TRAIL_DISTANCE}%"
    )

    log(
        f"SYMBOLS={SYMBOLS}"
    )

    log(
        "================================================"
    )

    try:

        sync_okx_time()

    except Exception as error:

        log(
            "TIME SYNC ERROR | "
            f"{error}"
        )

    try:

        public_get(
            "/api/v5/market/ticker",
            {
                "instId":
                    "BTC-USDT-SWAP"
            }
        )

        with state_lock:
            state[
                "public_api"
            ] = "CONNECTED"

        log(
            "PUBLIC API CONNECTED"
        )

    except Exception as error:

        with state_lock:
            state[
                "public_api"
            ] = "ERROR"

        log(
            "PUBLIC API ERROR | "
            f"{error}"
        )

    try:

        private_request(
            "GET",
            "/api/v5/account/balance"
        )

        with state_lock:
            state[
                "private_api"
            ] = "CONNECTED"

        log(
            "PRIVATE API CONNECTED"
        )

    except Exception as error:

        with state_lock:
            state[
                "private_api"
            ] = "ERROR"

        log(
            "PRIVATE API ERROR | "
            f"{error}"
        )


# =========================================================
# ANALYSIS WORKER
# =========================================================

def worker():

    global worker_started

    worker_started = True

    startup_checks()

    while True:

        for symbol in SYMBOLS:

            try:

                analysis = analyze_symbol(
                    symbol
                )

                with state_lock:

                    state.setdefault(
                        symbol,
                        {}
                    )

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
                    f"{analysis.get('signal')} "
                    f"{analysis.get('score', 0)}/10 | "
                    f"BUY={analysis.get('buy', 0)} "
                    f"SELL={analysis.get('sell', 0)}"
                )

                if (
                    analysis.get(
                        "signal"
                    ) in (
                        "BUY",
                        "SELL"
                    )
                    and
                    analysis.get(
                        "score",
                        0
                    ) >= MIN_SCORE
                ):

                    try:

                        result = trade_symbol(
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

                            state[symbol][
                                "trade_status"
                            ] = result.get(
                                "status",
                                "UNKNOWN"
                            )

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
                    "ANALYSIS ERROR | "
                    f"{symbol} | "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

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

<title>OKX Scalping Bot V6</title>

<style>

body {
    font-family: Arial, sans-serif;
    background:#101216;
    color:#eee;
    margin:0;
    padding:14px;
}

h2 {
    margin-top:0;
}

.cards {
    display:flex;
    flex-wrap:wrap;
    gap:8px;
}

.card {
    background:#1a1e24;
    border:1px solid #303640;
    border-radius:10px;
    padding:10px;
}

.ok {
    color:#43d17a;
}

.bad {
    color:#ff6262;
}

.warn {
    color:#ffc857;
}

.wrap {
    overflow:auto;
}

table {
    width:100%;
    border-collapse:collapse;
    margin-top:14px;
    font-size:12px;
}

th, td {
    padding:7px;
    border-bottom:1px solid #303640;
    white-space:nowrap;
    text-align:left;
}

th {
    background:#191d23;
    position:sticky;
    top:0;
}

.buy {
    color:#43d17a;
    font-weight:bold;
}

.sell {
    color:#ff6262;
    font-weight:bold;
}

.protected {
    color:#43d17a;
    font-weight:bold;
}

.error {
    color:#ff6262;
    font-weight:bold;
}

.small {
    color:#aaa;
    margin-top:10px;
    font-size:12px;
}

</style>

</head>

<body>

<h2>
OKX Scalping Bot V6 — Dynamic Protection
</h2>

<div
id="top"
class="cards"
>
Loading...
</div>

<div
id="activity"
class="small"
>
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
<th>15m</th>
<th>SL</th>
<th>TP</th>
<th>Protection</th>
<th>Stage</th>
<th>Algo ID</th>
<th>Status</th>
</tr>

</thead>

<tbody id="rows"></tbody>

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
                "&":"&amp;",
                "<":"&lt;",
                ">":"&gt;",
                '"':"&quot;",
                "'":"&#39;"
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
                SL:
                <b>
                ${esc(
                    s.sl
                )}%
                </b>
            </div>`

            +

            `<div class="card">
                TP:
                <b>
                ${esc(
                    s.tp
                )}%
                </b>
            </div>`

            +

            `<div class="card">
                BE:
                <b>
                ${esc(
                    s.be
                )}%
                </b>
            </div>`

            +

            `<div class="card">
                Trail:
                <b>
                ${esc(
                    s.trail
                )}%
                </b>
            </div>`;


        document.getElementById(
            "activity"
        ).textContent =

            "Last update: "
            +
            s.updated;


        let html = "";


        for (
            const [sym, x]
            of Object.entries(
                s.symbols
            )
        ) {

            const signal =
                x.signal || "NONE";

            const signalClass =
                signal === "BUY"
                ? "buy"
                :
                signal === "SELL"
                ? "sell"
                :
                "";


            const protection =
                x.protection ||
                "WAITING";

            const protectionClass =
                protection === "ACTIVE"
                ? "protected"
                :
                protection === "ERROR"
                ? "error"
                :
                "warn";


            html +=

                `<tr>

                <td>
                ${esc(sym)}
                </td>

                <td
                class="${signalClass}"
                >
                ${esc(signal)}
                </td>

                <td>
                ${esc(
                    x.score
                )}/10
                </td>

                <td>
                ${esc(
                    x.entry
                )}
                </td>

                <td>
                ${esc(
                    x.trend15
                )}
                </td>

                <td>
                ${esc(
                    x.protection_sl
                )}
                </td>

                <td>
                ${esc(
                    x.protection_tp
                )}
                </td>

                <td
                class="${protectionClass}"
                >
                ${esc(
                    protection
                )}
                </td>

                <td>
                ${esc(
                    x.protection_stage
                )}
                </td>

                <td>
                ${esc(
                    x.protection_algo
                )}
                </td>

                <td>
                ${esc(
                    x.trade_status
                    || "WAITING"
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
            "Dashboard error: "
            + e;

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

            key:
                value.copy()

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

    return jsonify({

        "bot":
            "OKX Scalping Bot V6",

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

        "sl":
            str(SL_PERCENT),

        "tp":
            str(TP_PERCENT),

        "be":
            str(BREAKEVEN_TRIGGER),

        "trail":
            str(TRAIL_DISTANCE),

        "public_api":
            public_api,

        "private_api":
            private_api,

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

    threading.Thread(
        target=worker,
        daemon=True
    ).start()

    threading.Thread(
        target=protection_worker,
        daemon=True
    ).start()

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
