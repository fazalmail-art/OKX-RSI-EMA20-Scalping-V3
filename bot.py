 import os
import time
import json
import hmac
import base64
import hashlib
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify
from dotenv import load_dotenv


# =========================================================
# LOAD ENVIRONMENT
# =========================================================

load_dotenv()


# =========================================================
# OKX SETTINGS
# =========================================================

BASE_URL = os.getenv(
    "OKX_BASE_URL",
    "https://openapi.okx.com"
)

API_KEY = os.getenv(
    "OKX_API_KEY",
    ""
)

SECRET_KEY = os.getenv(
    "OKX_SECRET_KEY",
    ""
)

PASSPHRASE = os.getenv(
    "OKX_PASSPHRASE",
    ""
)

DEMO = (
    os.getenv(
        "OKX_DEMO",
        "true"
    ).lower()
    == "true"
)


# =========================================================
# TRADING SETTINGS
# =========================================================

MARGIN_USDT = Decimal(
    os.getenv(
        "MARGIN_USDT",
        "20"
    )
)

LEVERAGE = Decimal(
    os.getenv(
        "LEVERAGE",
        "5"
    )
)

SL_PERCENT = Decimal(
    os.getenv(
        "SL_PERCENT",
        "0.4"
    )
)

TP_PERCENT = Decimal(
    os.getenv(
        "TP_PERCENT",
        "0.8"
    )
)

BAR = os.getenv(
    "BAR",
    "5m"
)

TREND_BAR = os.getenv(
    "TREND_BAR",
    "15m"
)

POLL_SECONDS = int(
    os.getenv(
        "POLL_SECONDS",
        "15"
    )
)

TD_MODE = os.getenv(
    "TD_MODE",
    "isolated"
)

POS_SIDE = os.getenv(
    "POS_SIDE",
    "net"
)


# =========================================================
# STRATEGY SETTINGS
# =========================================================

ADX_MIN = Decimal(
    os.getenv(
        "ADX_MIN",
        "18"
    )
)

VOLUME_MULT = Decimal(
    os.getenv(
        "VOLUME_MULT",
        "0.8"
    )
)

ATR_MIN_PCT = Decimal(
    os.getenv(
        "ATR_MIN_PCT",
        "0.05"
    )
)


# =========================================================
# SYMBOLS
# =========================================================

SYMBOLS = [
    item.strip()
    for item in os.getenv(
        "SYMBOLS",
        "BTC-USDT-SWAP,ETH-USDT-SWAP,XRP-USDT-SWAP,DOGE-USDT-SWAP"
    ).split(",")
    if item.strip()
]


# =========================================================
# APPLICATION
# =========================================================

app = Flask(__name__)

session = requests.Session()

last_processed_candle = {}

server_time_offset_ms = 0


# =========================================================
# LOG
# =========================================================

def log(message):

    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"{message}",
        flush=True
    )


# =========================================================
# SERVER TIME
# =========================================================

def sync_server_time():

    global server_time_offset_ms

    response = session.get(
        BASE_URL + "/api/v5/public/time",
        timeout=15
    )

    response.raise_for_status()

    data = response.json()

    if data.get("code") != "0":
        raise RuntimeError(
            f"OKX TIME ERROR: {data}"
        )

    server_ms = int(
        data["data"][0]["ts"]
    )

    local_ms = int(
        time.time() * 1000
    )

    server_time_offset_ms = (
        server_ms - local_ms
    )

    log(
        "OKX server time synchronized"
    )


def utc_timestamp():

    timestamp_ms = (
        int(time.time() * 1000)
        + server_time_offset_ms
    )

    timestamp = datetime.fromtimestamp(
        timestamp_ms / 1000,
        timezone.utc
    )

    return timestamp.isoformat(
        timespec="milliseconds"
    ).replace(
        "+00:00",
        "Z"
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

    message = (
        timestamp
        + method.upper()
        + request_path
        + body
    )

    digest = hmac.new(
        SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256
    ).digest()

    return base64.b64encode(
        digest
    ).decode()


# =========================================================
# PUBLIC REQUEST
# =========================================================

def public_get(
    path,
    params=None
):

    response = session.get(
        BASE_URL + path,
        params=params,
        timeout=15
    )

    response.raise_for_status()

    data = response.json()

    if data.get("code") != "0":

        raise RuntimeError(
            f"OKX PUBLIC ERROR "
            f"{data.get('code')}: "
            f"{data.get('msg')}"
        )

    return data


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
        raise RuntimeError(
            "OKX_API_KEY is missing"
        )

    if not SECRET_KEY:
        raise RuntimeError(
            "OKX_SECRET_KEY is missing"
        )

    if not PASSPHRASE:
        raise RuntimeError(
            "OKX_PASSPHRASE is missing"
        )

    body = ""

    if payload is not None:

        body = json.dumps(
            payload,
            separators=(",", ":")
        )

    request_path = path

    if params:

        query_parts = []

        for key, value in params.items():

            query_parts.append(
                f"{key}={value}"
            )

        request_path += (
            "?"
            + "&".join(query_parts)
        )

    timestamp = utc_timestamp()

    headers = {
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": create_signature(
            timestamp,
            method,
            request_path,
            body
        ),
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "OK-ACCESS-TIMESTAMP": timestamp
    }

    if DEMO:

        headers[
            "x-simulated-trading"
        ] = "1"

    response = session.request(
        method,
        BASE_URL + path,
        headers=headers,
        json=payload,
        params=params,
        timeout=15
    )

    response.raise_for_status()

    data = response.json()

    if data.get("code") != "0":

        raise RuntimeError(
            f"OKX PRIVATE ERROR "
            f"{data.get('code')}: "
            f"{data.get('msg')}"
        )

    return data


# =========================================================
# MARKET CANDLES
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

    candles = []

    for row in reversed(
        data.get("data", [])
    ):

        candles.append(
            {
                "ts": int(row[0]),
                "open": Decimal(row[1]),
                "high": Decimal(row[2]),
                "low": Decimal(row[3]),
                "close": Decimal(row[4]),
                "volume": Decimal(row[5]),
                "confirm": (
                    row[8]
                    if len(row) > 8
                    else "1"
                )
            }
        )

    return candles


# =========================================================
# EMA
# =========================================================

def calculate_ema(
    values,
    period
):

    if len(values) < period:

        return [
            None
        ] * len(values)

    result = [
        None
    ] * len(values)

    value = (
        sum(
            values[:period],
            Decimal("0")
        )
        / Decimal(period)
    )

    result[
        period - 1
    ] = value

    multiplier = (
        Decimal("2")
        / Decimal(period + 1)
    )

    for i in range(
        period,
        len(values)
    ):

        value = (
            values[i]
            * multiplier
            + value
            * (
                Decimal("1")
                - multiplier
            )
        )

        result[i] = value

    return result


# =========================================================
# RSI
# =========================================================

def calculate_rsi(
    values,
    period
):

    result = [
        None
    ] * len(values)

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

    if avg_loss == 0:

        result[
            period
        ] = Decimal("100")

    else:

        rs = (
            avg_gain
            / avg_loss
        )

        result[
            period
        ] = (
            Decimal("100")
            - (
                Decimal("100")
                / (
                    Decimal("1")
                    + rs
                )
            )
        )

    for i in range(
        period,
        len(gains)
    ):

        avg_gain = (
            avg_gain
            * (period - 1)
            + gains[i]
        ) / Decimal(period)

        avg_loss = (
            avg_loss
            * (period - 1)
            + losses[i]
        ) / Decimal(period)

        if avg_loss == 0:

            result[
                i + 1
            ] = Decimal("100")

        else:

            rs = (
                avg_gain
                / avg_loss
            )

            result[
                i + 1
            ] = (
                Decimal("100")
                - (
                    Decimal("100")
                    / (
                        Decimal("1")
                        + rs
                    )
                )
            )

    return result


# =========================================================
# ATR
# =========================================================

def calculate_atr(
    candles,
    period=14
):

    if len(candles) <= period:

        return None

    true_ranges = []

    for i in range(
        1,
        len(candles)
    ):

        high = candles[i]["high"]
        low = candles[i]["low"]

        previous_close = (
            candles[i - 1]["close"]
        )

        true_range = max(
            high - low,
            abs(
                high
                - previous_close
            ),
            abs(
                low
                - previous_close
            )
        )

        true_ranges.append(
            true_range
        )

    return (
        sum(
            true_ranges[-period:],
            Decimal("0")
        )
        / Decimal(period)
    )


# =========================================================
# SIMPLE ADX
# =========================================================

def calculate_adx(
    candles,
    period=14
):

    if len(candles) < (
        period + 2
    ):

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
        plus
        / total_range
        * Decimal("100")
    )

    minus_di = (
        minus
        / total_range
        * Decimal("100")
    )

    total = (
        plus_di
        + minus_di
    )

    if total == 0:

        return Decimal("0")

    return (
        abs(
            plus_di
            - minus_di
        )
        / total
        * Decimal("100")
    )


# =========================================================
# 15M TREND
# =========================================================

def get_15m_trend(
    symbol
):

    candles = get_candles(
        symbol,
        TREND_BAR,
        80
    )

    candles = [
        candle
        for candle in candles
        if candle["confirm"] == "1"
    ]

    if len(candles) < 22:

        return "flat"

    closes = [
        candle["close"]
        for candle in candles
    ]

    ema20 = calculate_ema(
        closes,
        20
    )

    i = len(candles) - 1

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
# SIGNAL
# =========================================================

def calculate_signal(
    symbol
):

    candles = get_candles(
        symbol,
        BAR,
        160
    )

    candles = [
        candle
        for candle in candles
        if candle["confirm"] == "1"
    ]

    if len(candles) < 105:

        return None

    closes = [
        candle["close"]
        for candle in candles
    ]

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

    i = len(candles) - 1

    if (
        rsi14[i] is None
        or rsi14[i - 1] is None
        or rsi100[i] is None
        or rsi100[i - 1] is None
        or ema20[i] is None
        or ema20[i - 1] is None
        or atr is None
    ):

        return None

    # RSI crossover
    buy_rsi = (
        rsi14[i - 1]
        <= rsi100[i - 1]
        and
        rsi14[i]
        > rsi100[i]
    )

    sell_rsi = (
        rsi14[i - 1]
        >= rsi100[i - 1]
        and
        rsi14[i]
        < rsi100[i]
    )

    # EMA20 signal for sideways market
    buy_ema = (
        closes[i - 1]
        <= ema20[i - 1]
        and
        closes[i]
        > ema20[i]
        and
        rsi14[i] <= 55
    )

    sell_ema = (
        closes[i - 1]
        >= ema20[i - 1]
        and
        closes[i]
        < ema20[i]
        and
        rsi14[i] >= 45
    )

    signal = None
    reason = None

    if buy_rsi:

        signal = "buy"
        reason = "RSI_CROSS"

    elif sell_rsi:

        signal = "sell"
        reason = "RSI_CROSS"

    elif buy_ema:

        signal = "buy"
        reason = "EMA20"

    elif sell_ema:

        signal = "sell"
        reason = "EMA20"

    if signal is None:

        return None

    average_volume = (
        sum(
            candle["volume"]
            for candle in candles[-21:-1]
        )
        / Decimal("20")
    )

    volume_ok = (
        candles[i]["volume"]
        >=
        average_volume
        * VOLUME_MULT
    )

    atr_percent = (
        atr
        / closes[i]
        * Decimal("100")
    )

    if adx < ADX_MIN:

        return None

    if not volume_ok:

        return None

    if atr_percent < ATR_MIN_PCT:

        return None

    trend15 = get_15m_trend(
        symbol
    )

    # 15m confirmation
    if (
        trend15 == "bull"
        and signal != "buy"
    ):

        return None

    if (
        trend15 == "bear"
        and signal != "sell"
    ):

        return None

    return {
        "signal": signal,
        "reason": reason,
        "entry": closes[i],
        "rsi14": rsi14[i],
        "rsi100": rsi100[i],
        "ema20": ema20[i],
        "adx": adx,
        "atr": atr,
        "trend15": trend15
    }


# =========================================================
# INSTRUMENT INFORMATION
# =========================================================

def get_instrument(
    symbol
):

    data = public_get(
        "/api/v5/public/instruments",
        {
            "instType": "SWAP",
            "instId": symbol
        }
    )

    if not data.get("data"):

        raise RuntimeError(
            f"Instrument not found: {symbol}"
        )

    item = data["data"][0]

    return {
        "ctVal": Decimal(
            item["ctVal"]
        ),
        "ctValCcy": item[
            "ctValCcy"
        ],
        "lotSz": Decimal(
            item["lotSz"]
        ),
        "minSz": Decimal(
            item["minSz"]
        ),
        "tickSz": Decimal(
            item["tickSz"]
        )
    }


# =========================================================
# ROUNDING
# =========================================================

def round_down(
    value,
    step
):

    if step <= 0:

        return value

    units = (
        value / step
    ).to_integral_value(
        rounding=ROUND_DOWN
    )

    return units * step


# =========================================================
# MARKET PRICE
# =========================================================

def get_last_price(
    symbol
):

    data = public_get(
        "/api/v5/market/ticker",
        {
            "instId": symbol
        }
    )

    if not data.get("data"):

        raise RuntimeError(
            f"No ticker data: {symbol}"
        )

    return Decimal(
        data["data"][0]["last"]
    )


# =========================================================
# POSITION SIZE
# =========================================================

def calculate_contract_size(
    symbol,
    price
):

    info = get_instrument(
        symbol
    )

    if info["ctValCcy"].upper() != "BTC" and \
       symbol.endswith("-USDT-SWAP"):

        pass

    target_notional = (
        MARGIN_USDT
        * LEVERAGE
    )

    contract_value = (
        price
        * info["ctVal"]
    )

    if contract_value <= 0:

        raise RuntimeError(
            "Invalid contract value"
        )

    raw_size = (
        target_notional
        / contract_value
    )

    size = round_down(
        raw_size,
        info["lotSz"]
    )

    if size < info["minSz"]:

        size = info["minSz"]

    return size, info


# =========================================================
# SET LEVERAGE
# =========================================================

def set_leverage(
    symbol
):

    payload = {
        "instId": symbol,
        "lever": str(
            LEVERAGE
        ),
        "mgnMode": TD_MODE
    }

    return private_request(
        "POST",
        "/api/v5/account/set-leverage",
        payload
    )


# =========================================================
# GET POSITIONS
# =========================================================

def get_positions(
    symbol
):

    data = private_request(
        "GET",
        "/api/v5/account/positions",
        params={
            "instId": symbol
        }
    )

    positions = []

    for position in data.get(
        "data",
        []
    ):

        try:

            position_size = Decimal(
                position.get(
                    "pos",
                    "0"
                )
            )

        except Exception:

            position_size = Decimal("0")

        if position_size != 0:

            positions.append(
                position
            )

    return positions


# =========================================================
# CLOSE EXISTING NET POSITION
# =========================================================

def close_position(
    symbol,
    position
):

    pos_size = position.get(
        "pos",
        "0"
    )

    if Decimal(
        pos_size
    ) == 0:

        return None

    side = (
        "sell"
        if Decimal(pos_size) > 0
        else "buy"
    )

    size = str(
        abs(
            Decimal(pos_size)
        )
    )

    payload = {
        "instId": symbol,
        "tdMode": TD_MODE,
        "side": side,
        "posSide": "net",
        "ordType": "market",
        "sz": size,
        "reduceOnly": True
    }

    log(
        f"CLOSING EXISTING POSITION "
        f"{symbol} size={size}"
    )

    return private_request(
        "POST",
        "/api/v5/trade/order",
        payload
    )


# =========================================================
# OPEN DEMO POSITION
# =========================================================

def open_position(
    symbol,
    signal
):

    price = signal["entry"]

    size, info = calculate_contract_size(
        symbol,
        price
    )

    set_leverage(
        symbol
    )

    if signal["signal"] == "buy":

        side = "buy"

        sl_price = (
            price
            * (
                Decimal("1")
                - (
                    SL_PERCENT
                    / Decimal("100")
                )
            )
        )

        tp_price = (
            price
            * (
                Decimal("1")
                + (
                    TP_PERCENT
                    / Decimal("100")
                )
            )
        )

    else:

        side = "sell"

        sl_price = (
            price
            * (
                Decimal("1")
                + (
                    SL_PERCENT
                    / Decimal("100")
                )
            )
        )

        tp_price = (
            price
            * (
                Decimal("1")
                - (
                    TP_PERCENT
                    / Decimal("100")
                )
            )
        )

    sl_price = round_down(
        sl_price,
        info["tickSz"]
    )

    tp_price = round_down(
        tp_price,
        info["tickSz"]
    )

    order_id = (
        "rsi"
        + str(
            int(
                time.time() * 1000
            )
        )
    )[-32:]

    payload = {
        "instId": symbol,
        "tdMode": TD_MODE,
        "side": side,
        "posSide": POS_SIDE,
        "ordType": "market",
        "sz": str(size),
        "clOrdId": order_id,
        "attachAlgoOrds": [
            {
                "attachAlgoClOrdId": (
                    order_id
                    + "a"
                )[-32:],
                "tpTriggerPx": str(
                    tp_price
                ),
                "tpOrdPx": "-1",
                "tpTriggerPxType": "mark",
                "slTriggerPx": str(
                    sl_price
                ),
                "slOrdPx": "-1",
                "slTriggerPxType": "mark"
            }
        ]
    }

    log(
        "===================================="
    )

    log(
        f"PLACING DEMO ORDER: "
        f"{symbol} "
        f"{side.upper()}"
    )

    log(
        f"Margin=${MARGIN_USDT} "
        f"Leverage={LEVERAGE}x"
    )

    log(
        f"Contracts={size}"
    )

    log(
        f"Entry={price}"
    )

    log(
        f"SL={sl_price}"
    )

    log(
        f"TP={tp_price}"
    )

    result = private_request(
        "POST",
        "/api/v5/trade/order",
        payload
    )

    log(
        "ORDER REQUEST ACCEPTED"
    )

    log(
        json.dumps(
            result,
            default=str
        )
    )

    return {
        "status": "order_sent",
        "symbol": symbol,
        "side": side,
        "contracts": str(size),
        "entry": str(price),
        "sl": str(sl_price),
        "tp": str(tp_price),
        "okx": result
    }


# =========================================================
# EXECUTE SIGNAL
# =========================================================

def execute(
    symbol,
    signal
):

    if not DEMO:

        raise RuntimeError(
            "DEMO mode is required. "
            "Set OKX_DEMO=true."
        )

    positions = get_positions(
        symbol
    )

    desired_side = (
        "long"
        if signal["signal"] == "buy"
        else "short"
    )

    # Do not duplicate same-direction trade
    for position in positions:

        pos_side = position.get(
            "posSide",
            "net"
        )

        position_size = Decimal(
            position.get(
                "pos",
                "0"
            )
        )

        if (
            position_size != 0
            and POS_SIDE == "net"
        ):

            if (
                signal["signal"] == "buy"
                and position_size > 0
            ):

                return {
                    "status": "ignored",
                    "reason":
                    "BUY position already open"
                }

            if (
                signal["signal"] == "sell"
                and position_size < 0
            ):

                return {
                    "status": "ignored",
                    "reason":
                    "SELL position already open"
                }

            close_position(
                symbol,
                position
            )

            time.sleep(1)

    result = open_position(
        symbol,
        signal
    )

    return result


# =========================================================
# TEST OKX CONNECTION
# =========================================================

def test_okx_connection():

    log(
        "Testing OKX public market connection..."
    )

    ticker = public_get(
        "/api/v5/market/ticker",
        {
            "instId":
            "BTC-USDT-SWAP"
        }
    )

    if ticker.get("data"):

        btc_price = ticker[
            "data"
        ][0].get(
            "last"
        )

        log(
            "OKX MARKET CONNECTED | "
            f"BTC={btc_price}"
        )

    if (
        API_KEY
        and SECRET_KEY
        and PASSPHRASE
    ):

        log(
            "Testing OKX private API..."
        )

        balance = private_request(
            "GET",
            "/api/v5/account/balance"
        )

        if balance.get("code") == "0":

            log(
                "OKX PRIVATE API CONNECTED"
            )

        return balance

    log(
        "WARNING: OKX API credentials "
        "are missing"
    )

    return None


# =========================================================
# WORKER
# =========================================================

def worker():

    log(
        "========================================"
    )

    log(
        "OKX RSI + EMA20 + ADX + ATR "
        "+ VOLUME V4 STARTED"
    )

    log(
        f"DEMO={DEMO}"
    )

    log(
        f"MARGIN=${MARGIN_USDT}"
    )

    log(
        f"LEVERAGE={LEVERAGE}x"
    )

    log(
        f"BAR={BAR}"
    )

    log(
        f"TREND_BAR={TREND_BAR}"
    )

    log(
        f"TD_MODE={TD_MODE}"
    )

    log(
        f"POS_SIDE={POS_SIDE}"
    )

    log(
        f"SYMBOLS={SYMBOLS}"
    )

    log(
        "========================================"
    )

    try:

        sync_server_time()

    except Exception as error:

        log(
            "TIME SYNC ERROR: "
            f"{type(error).__name__}: "
            f"{error}"
        )

    try:

        test_okx_connection()

    except Exception as error:

        log(
            "OKX CONNECTION ERROR: "
            f"{type(error).__name__}: "
            f"{error}"
        )

    while True:

        log(
            "BOT LOOP: checking market..."
        )

        for symbol in SYMBOLS:

            try:

                log(
                    f"CHECKING {symbol}"
                )

                candles = get_candles(
                    symbol,
                    BAR,
                    160
                )

                confirmed = [
                    candle
                    for candle in candles
                    if candle["confirm"] == "1"
                ]

                log(
                    f"{symbol}: "
                    f"{len(confirmed)} "
                    f"confirmed candles"
                )

                if not confirmed:

                    continue

                candle_time = (
                    confirmed[-1]["ts"]
                )

                if (
                    last_processed_candle.get(
                        symbol
                    )
                    == candle_time
                ):

                    continue

                last_processed_candle[
                    symbol
                ] = candle_time

                signal = calculate_signal(
                    symbol
                )

                if signal is None:

                    log(
                        f"{symbol}: "
                        f"NO VALID SIGNAL"
                    )

                    continue

                log(
                    f"*** SIGNAL *** "
                    f"{symbol} "
                    f"{signal['signal'].upper()} "
                    f"reason={signal['reason']} "
                    f"entry={signal['entry']} "
                    f"ADX={signal['adx']} "
                    f"Trend15={signal['trend15']}"
                )

                result = execute(
                    symbol,
                    signal
                )

                log(
                    json.dumps(
                        result,
                        default=str
                    )
                )

            except Exception as error:

                log(
                    f"{symbol} ERROR: "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

        time.sleep(
            POLL_SECONDS
        )


# =========================================================
# WEB ENDPOINTS
# =========================================================

@app.get("/")
def home():

    return jsonify(
        {
            "bot":
            "OKX RSI EMA20 ADX ATR Volume Scalping V4",
            "status":
            "running",
            "demo":
            DEMO,
            "margin_usdt":
            str(MARGIN_USDT),
            "leverage":
            str(LEVERAGE),
            "timeframe":
            BAR,
            "trend_timeframe":
            TREND_BAR,
            "symbols":
            SYMBOLS
        }
    )


@app.get("/health")
def health():

    return jsonify(
        {
            "status":
            "healthy",
            "demo":
            DEMO,
            "api_key_present":
            bool(API_KEY),
            "secret_present":
            bool(SECRET_KEY),
            "passphrase_present":
            bool(PASSPHRASE),
            "margin_usdt":
            str(MARGIN_USDT),
            "leverage":
            str(LEVERAGE),
            "symbols":
            SYMBOLS
        }
    )


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    import threading

    log(
        "========== BOT STARTING =========="
    )

    worker_thread = threading.Thread(
        target=worker,
        daemon=True
    )

    worker_thread.start()

    log(
        "========== WORKER STARTED =========="
    )

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

    log(
        f"========== WEB SERVER PORT "
        f"{port} =========="
    )

    app.run(
        host="0.0.0.0",
        port=port
    )   
