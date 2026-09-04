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
# CONFIG
# =========================================================

BASE_URL = os.getenv(
    "OKX_BASE_URL",
    "https://us.okx.com"
).rstrip("/")

API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

DEMO = os.getenv("OKX_DEMO", "true").lower() == "true"
AUTO_TRADE = os.getenv("AUTO_TRADE", "true").lower() == "true"

MARGIN_USDT = Decimal(
    os.getenv("MARGIN_USDT", "10")
)

LEVERAGE = Decimal(
    os.getenv("LEVERAGE", "3")
)

TD_MODE = os.getenv(
    "TD_MODE",
    "isolated"
)

# 5-minute structure
STRUCTURE_BAR = "5m"

# 1-second data aggregated into 15-second candles
EXECUTION_SECONDS = 15

# Maximum holding time
MAX_HOLD_SECONDS = int(
    os.getenv("MAX_HOLD_SECONDS", "30")
)

# Minimum time between new trades on same pair
COOLDOWN_SECONDS = int(
    os.getenv("COOLDOWN_SECONDS", "45")
)

# Structure breakout buffer
BREAK_BUFFER_PCT = Decimal(
    os.getenv("BREAK_BUFFER_PCT", "0.015")
)

# Minimum candle body/range ratio
MIN_BODY_RATIO = Decimal(
    os.getenv("MIN_BODY_RATIO", "0.35")
)

# Volume confirmation
MIN_VOLUME_RATIO = Decimal(
    os.getenv("MIN_VOLUME_RATIO", "1.00")
)

# Emergency SL only
EMERGENCY_SL_PCT = Decimal(
    os.getenv("EMERGENCY_SL_PCT", "0.40")
)

# Order fill checking
ORDER_CHECK_SECONDS = int(
    os.getenv("ORDER_CHECK_SECONDS", "1")
)

ORDER_CHECK_ATTEMPTS = int(
    os.getenv("ORDER_CHECK_ATTEMPTS", "10")
)

# =========================================================
# SYMBOLS
# =========================================================

SYMBOLS = [
    x.strip().upper()
    for x in os.getenv(
        "SYMBOLS",
        "BTC-USDT-SWAP,"
        "ETH-USDT-SWAP,"
        "SOL-USDT-SWAP,"
        "HYPE-USDT-SWAP,"
        "XRP-USDT-SWAP,"
        "DOGE-USDT-SWAP"
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

worker_started = False
server_offset_ms = 0

last_trade_time = {}

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


def ceil_step(value, step):

    if step <= 0:
        return value

    return (
        value / step
    ).to_integral_value(
        rounding=ROUND_UP
    ) * step


# =========================================================
# PUBLIC API
# =========================================================

def public_get(path, params=None):

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
# TIME SYNC
# =========================================================

def sync_okx_time():

    global server_offset_ms

    before = int(time.time() * 1000)

    data = public_get(
        "/api/v5/public/time"
    )

    after = int(time.time() * 1000)

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
        f"OKX TIME SYNC | "
        f"offset={server_offset_ms}ms"
    )


def utc_timestamp():

    ms = (
        int(time.time() * 1000)
        + server_offset_ms
    )

    dt = datetime.fromtimestamp(
        ms / 1000,
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
# TICKER
# =========================================================

def get_ticker(symbol):

    data = public_get(
        "/api/v5/market/ticker",
        {
            "instId": symbol
        }
    )

    rows = data.get(
        "data",
        []
    )

    if not rows:
        raise RuntimeError(
            "Ticker unavailable: "
            + symbol
        )

    return dec(
        rows[0]["last"]
    )


# =========================================================
# MARK PRICE
# =========================================================

def get_mark_price(symbol):

    data = public_get(
        "/api/v5/public/mark-price",
        {
            "instType": "SWAP",
            "instId": symbol
        }
    )

    rows = data.get(
        "data",
        []
    )

    if not rows:
        return get_ticker(symbol)

    return dec(
        rows[0]["markPx"]
    )


# =========================================================
# 5M CANDLES
# =========================================================

def get_candles(
    symbol,
    bar="5m",
    limit=120
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

        candles.append({
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

    # Only CLOSED candles
    return [
        x for x in candles
        if x["confirm"] == "1"
    ]


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

    rows = data.get(
        "data",
        []
    )

    if not rows:
        raise RuntimeError(
            "Instrument not found: "
            + symbol
        )

    item = rows[0]

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

    for p in data.get(
        "data",
        []
    ):

        try:
            size = dec(
                p.get("pos", "0")
            )
        except Exception:
            size = Decimal("0")

        if size != 0:
            return p

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
        "instId":
            symbol,

        "lever":
            fmt(LEVERAGE),

        "mgnMode":
            TD_MODE
    }

    return private_request(
        "POST",
        "/api/v5/account/set-leverage",
        payload=payload
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

    target_notional = (
        MARGIN_USDT
        * LEVERAGE
    )

    raw_size = (
        target_notional
        / (
            info["ctVal"]
            * price
        )
    )

    size = floor_step(
        raw_size,
        info["lotSz"]
    )

    # IMPORTANT:
    # Never automatically oversize
    # a small account.

    if size < info["minSz"]:

        raise RuntimeError(
            "SIZE BLOCKED | "
            f"calculated={fmt(size)} | "
            f"minimum={fmt(info['minSz'])} | "
            f"ctVal={fmt(info['ctVal'])} | "
            f"target_notional="
            f"{fmt(target_notional)}"
        )

    return size, info


# =========================================================
# ORDER STATUS
# =========================================================

def get_order(
    symbol,
    ord_id
):

    return private_request(
        "GET",
        "/api/v5/trade/order",
        params={
            "instId":
                symbol,
            "ordId":
                ord_id
        }
    )


# =========================================================
# VERIFY ORDER FILL
# =========================================================

def wait_for_order_fill(
    symbol,
    ord_id
):

    log(
        f"[ORDER VERIFY] "
        f"{symbol} | ordId={ord_id}"
    )

    last_state = ""

    for attempt in range(
        1,
        ORDER_CHECK_ATTEMPTS + 1
    ):

        time.sleep(
            ORDER_CHECK_SECONDS
        )

        try:

            result = get_order(
                symbol,
                ord_id
            )

            rows = result.get(
                "data",
                []
            )

            if not rows:
                continue

            order = rows[0]

            state_value = order.get(
                "state",
                ""
            )

            acc_fill = order.get(
                "accFillSz",
                "0"
            )

            if state_value != last_state:

                log(
                    f"[ORDER STATUS] "
                    f"{symbol} | "
                    f"ordId={ord_id} | "
                    f"state={state_value} | "
                    f"filled={acc_fill}"
                )

                last_state = state_value

            # FILLED
            if state_value == "filled":

                log(
                    f"[ORDER FILLED] "
                    f"{symbol} | "
                    f"ordId={ord_id} | "
                    f"filled={acc_fill}"
                )

                return order

            # PARTIALLY FILLED
            if (
                state_value
                == "partially_filled"
            ):

                # For market order,
                # wait for final status.
                continue

            # Failed/cancelled
            if state_value in (
                "canceled",
                "mmp_canceled"
            ):

                raise RuntimeError(
                    "ORDER CANCELLED | "
                    f"ordId={ord_id}"
                )

        except RuntimeError:
            raise

        except Exception as error:

            log(
                f"[ORDER VERIFY ERROR] "
                f"{symbol} | "
                f"{error}"
            )

    raise RuntimeError(
        "ORDER NOT FILLED WITHIN "
        f"{ORDER_CHECK_ATTEMPTS}"
        " CHECKS"
    )


# =========================================================
# WAIT FOR POSITION
# =========================================================

def wait_for_position(
    symbol
):

    log(
        f"[POSITION VERIFY] "
        f"{symbol}"
    )

    for attempt in range(
        1,
        ORDER_CHECK_ATTEMPTS + 1
    ):

        time.sleep(
            ORDER_CHECK_SECONDS
        )

        position = get_position(
            symbol
        )

        if position:

            size = dec(
                position.get(
                    "pos",
                    "0"
                )
            )

            if size != 0:

                avg_px = dec(
                    position.get(
                        "avgPx",
                        "0"
                    )
                )

                log(
                    f"[POSITION CONFIRMED] "
                    f"{symbol} | "
                    f"size={fmt(size)} | "
                    f"avgPx={fmt(avg_px)}"
                )

                return position

    raise RuntimeError(
        "ORDER FILLED BUT POSITION "
        "WAS NOT CONFIRMED"
    )


# =========================================================
# 5M STRUCTURE
# =========================================================

def analyze_5m_structure(
    symbol
):

    candles = get_candles(
        symbol,
        "5m",
        120
    )

    if len(candles) < 20:

        return {
            "signal":
                "NONE",
            "reason":
                "Not enough 5M candles"
        }

    # Last CLOSED 5M candle
    last = candles[-1]

    # Previous structure range
    lookback = candles[-11:-1]

    resistance = max(
        x["high"]
        for x in lookback
    )

    support = min(
        x["low"]
        for x in lookback
    )

    close = last["close"]
    open_px = last["open"]
    high = last["high"]
    low = last["low"]

    candle_range = (
        high - low
    )

    body = abs(
        close - open_px
    )

    body_ratio = (
        body / candle_range
        if candle_range > 0
        else Decimal("0")
    )

    volume_avg = (
        sum(
            x["volume"]
            for x in lookback
        )
        / Decimal(len(lookback))
    )

    volume_ratio = (
        last["volume"]
        / volume_avg
        if volume_avg > 0
        else Decimal("0")
    )

    buy_level = (
        resistance
        * (
            Decimal("1")
            + BREAK_BUFFER_PCT
            / Decimal("100")
        )
    )

    sell_level = (
        support
        * (
            Decimal("1")
            - BREAK_BUFFER_PCT
            / Decimal("100")
        )
    )

    signal = "NONE"
    reason = "No BOS"

    # BUY BOS
    if (
        close > buy_level
        and close > open_px
        and body_ratio
        >= MIN_BODY_RATIO
        and volume_ratio
        >= MIN_VOLUME_RATIO
    ):

        signal = "BUY"

        reason = (
            "5M bullish BOS"
        )

    # SELL BOS
    elif (
        close < sell_level
        and close < open_px
        and body_ratio
        >= MIN_BODY_RATIO
        and volume_ratio
        >= MIN_VOLUME_RATIO
    ):

        signal = "SELL"

        reason = (
            "5M bearish BOS"
        )

    return {
        "signal":
            signal,

        "close":
            close,

        "resistance":
            resistance,

        "support":
            support,

        "body_ratio":
            body_ratio,

        "volume_ratio":
            volume_ratio,

        "reason":
            reason,

        "candle_ts":
            last["ts"]
    }


# =========================================================
# 15 SECOND CANDLE FROM LIVE TICKS
# =========================================================

def make_15s_candle(
    ticks
):

    if not ticks:
        return None

    prices = [
        x["price"]
        for x in ticks
    ]

    return {
        "open":
            prices[0],

        "high":
            max(prices),

        "low":
            min(prices),

        "close":
            prices[-1],

        "volume":
            sum(
                x["volume"]
                for x in ticks
            )
    }


# =========================================================
# 15S CONFIRMATION
# =========================================================

def confirm_15s(
    candle,
    signal,
    structure
):

    if not candle:
        return False

    open_px = candle["open"]
    close = candle["close"]
    high = candle["high"]
    low = candle["low"]

    rng = (
        high - low
    )

    if rng <= 0:
        return False

    body_ratio = (
        abs(close - open_px)
        / rng
    )

    if body_ratio < MIN_BODY_RATIO:
        return False

    # BUY confirmation
    if signal == "BUY":

        if (
            close > open_px
            and close > structure[
                "resistance"
            ]
        ):

            log(
                "[15S CONFIRMED] "
                f"{signal} | "
                f"close={fmt(close)}"
            )

            return True

    # SELL confirmation
    if signal == "SELL":

        if (
            close < open_px
            and close < structure[
                "support"
            ]
        ):

            log(
                "[15S CONFIRMED] "
                f"{signal} | "
                f"close={fmt(close)}"
            )

            return True

    return False


# =========================================================
# EXECUTE MARKET ORDER
# =========================================================

def execute_order(
    symbol,
    signal
):

    if not AUTO_TRADE:

        log(
            f"[ORDER BLOCKED] "
            f"{symbol} | "
            f"AUTO_TRADE=false"
        )

        return {
            "status":
                "BLOCKED"
        }

    if not DEMO:

        log(
            f"[ORDER BLOCKED] "
            f"{symbol} | "
            f"DEMO=false"
        )

        return {
            "status":
                "BLOCKED"
        }

    with order_lock:

        # Existing position protection
        existing = get_position(
            symbol
        )

        if existing:

            log(
                f"[ORDER BLOCKED] "
                f"{symbol} | "
                f"existing position"
            )

            return {
                "status":
                    "EXISTING_POSITION"
            }

        now = time.time()

        previous = last_trade_time.get(
            symbol,
            0
        )

        if (
            now - previous
            < COOLDOWN_SECONDS
        ):

            log(
                f"[ORDER BLOCKED] "
                f"{symbol} | "
                f"cooldown"
            )

            return {
                "status":
                    "COOLDOWN"
            }

        side = (
            "buy"
            if signal == "BUY"
            else "sell"
        )

        price = get_ticker(
            symbol
        )

        size, info = (
            calculate_order_size(
                symbol,
                price
            )
        )

        set_leverage(
            symbol
        )

        client_id = (
            "bot"
            + uuid.uuid4().hex[:24]
        )

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
                client_id
        }

        log(
            f"[ORDER REQUEST] "
            f"{symbol} | "
            f"{signal} | "
            f"price={fmt(price)} | "
            f"size={fmt(size)}"
        )

        try:

            result = private_request(
                "POST",
                "/api/v5/trade/order",
                payload=payload
            )

        except Exception as error:

            log(
                f"[ORDER API FAILED] "
                f"{symbol} | "
                f"{error}"
            )

            return {
                "status":
                    "API_FAILED",

                "error":
                    str(error)
            }

        rows = result.get(
            "data",
            []
        )

        if not rows:

            log(
                f"[ORDER FAILED] "
                f"{symbol} | "
                f"empty response"
            )

            return {
                "status":
                    "FAILED"
            }

        row = rows[0]

        s_code = row.get(
            "sCode"
        )

        s_msg = row.get(
            "sMsg"
        )

        if s_code not in (
            None,
            "",
            "0"
        ):

            log(
                f"[ORDER REJECTED] "
                f"{symbol} | "
                f"sCode={s_code} | "
                f"sMsg={s_msg}"
            )

            return {
                "status":
                    "REJECTED",

                "sCode":
                    s_code,

                "sMsg":
                    s_msg
            }

        ord_id = row.get(
            "ordId"
        )

        if not ord_id:

            log(
                f"[ORDER FAILED] "
                f"{symbol} | "
                f"NO ordId"
            )

            return {
                "status":
                    "FAILED"
            }

        log(
            f"[ORDER ACCEPTED] "
            f"{symbol} | "
            f"ordId={ord_id}"
        )

        # =================================================
        # IMPORTANT:
        # API ACCEPTED != FILLED
        # =================================================

        try:

            filled_order = (
                wait_for_order_fill(
                    symbol,
                    ord_id
                )
            )

        except Exception as error:

            log(
                f"[ORDER NOT FILLED] "
                f"{symbol} | "
                f"ordId={ord_id} | "
                f"{error}"
            )

            return {
                "status":
                    "NOT_FILLED",

                "ordId":
                    ord_id,

                "error":
                    str(error)
            }

        # =================================================
        # POSITION CONFIRMATION
        # =================================================

        try:

            position = (
                wait_for_position(
                    symbol
                )
            )

        except Exception as error:

            log(
                f"[CRITICAL] "
                f"ORDER FILLED BUT "
                f"POSITION MISSING | "
                f"{symbol} | "
                f"{error}"
            )

            return {
                "status":
                    "FILLED_POSITION_NOT_CONFIRMED",

                "ordId":
                    ord_id,

                "error":
                    str(error)
            }

        last_trade_time[
            symbol
        ] = time.time()

        avg_px = dec(
            position.get(
                "avgPx",
                price
            )
        )

        position_size = dec(
            position.get(
                "pos",
                "0"
            )
        )

        log(
            f"[TRADE EXECUTED] "
            f"{symbol} | "
            f"{signal} | "
            f"ordId={ord_id} | "
            f"entry={fmt(avg_px)} | "
            f"position={fmt(position_size)}"
        )

        return {
            "status":
                "EXECUTED",

            "symbol":
                symbol,

            "signal":
                signal,

            "ordId":
                ord_id,

            "entry":
                fmt(avg_px),

            "position":
                fmt(position_size),

            "filled":
                filled_order.get(
                    "accFillSz",
                    "0"
                )
        }


# =========================================================
# WORKER
# =========================================================

def worker():

    global worker_started

    worker_started = True

    log(
        "===================================="
    )

    log(
        "OKX 5M BOS + 15S CONFIRMATION BOT"
    )

    log(
        f"DEMO={DEMO} | "
        f"AUTO_TRADE={AUTO_TRADE}"
    )

    log(
        f"MARGIN={MARGIN_USDT} | "
        f"LEVERAGE={LEVERAGE}x"
    )

    log(
        f"SYMBOLS={SYMBOLS}"
    )

    log(
        "WhatsApp: DISABLED"
    )

    log(
        "===================================="
    )

    try:
        sync_okx_time()
    except Exception as error:
        log(
            f"TIME SYNC ERROR | {error}"
        )

    # 15s live candles per symbol
    tick_buffer = {
        symbol: []
        for symbol in SYMBOLS
    }

    last_15s_bucket = {
        symbol: None
        for symbol in SYMBOLS
    }

    while True:

        for symbol in SYMBOLS:

            try:

                # -------------------------------------------------
                # EXISTING POSITION
                # -------------------------------------------------

                position = get_position(
                    symbol
                )

                if position:

                    with state_lock:

                        state.setdefault(
                            symbol,
                            {}
                        )

                        state[
                            symbol
                        ].update({
                            "position":
                                fmt(
                                    dec(
                                        position.get(
                                            "pos",
                                            "0"
                                        )
                                    )
                                ),

                            "avgPx":
                                fmt(
                                    dec(
                                        position.get(
                                            "avgPx",
                                            "0"
                                        )
                                    )
                                ),

                            "status":
                                "POSITION OPEN"
                        })

                    continue

                # -------------------------------------------------
                # 5M STRUCTURE
                # -------------------------------------------------

                structure = (
                    analyze_5m_structure(
                        symbol
                    )
                )

                signal = structure[
                    "signal"
                ]

                log(
                    f"[5M STRUCTURE] "
                    f"{symbol} | "
                    f"{signal} | "
                    f"price="
                    f"{fmt(structure.get('close'))} | "
                    f"R="
                    f"{fmt(structure.get('resistance'))} | "
                    f"S="
                    f"{fmt(structure.get('support'))}"
                )

                with state_lock:

                    state.setdefault(
                        symbol,
                        {}
                    )

                    state[
                        symbol
                    ].update({
                        "signal":
                            signal,

                        "price":
                            structure.get(
                                "close"
                            ),

                        "resistance":
                            structure.get(
                                "resistance"
                            ),

                        "support":
                            structure.get(
                                "support"
                            ),

                        "reason":
                            structure.get(
                                "reason"
                            ),

                        "status":
                            "WAITING 15S"
                            if signal
                            in (
                                "BUY",
                                "SELL"
                            )
                            else "NO SIGNAL"
                    })

                # -------------------------------------------------
                # IMPORTANT:
                # Here a real 15S candle must confirm.
                #
                # This compact version uses the current market
                # ticker as the latest 15S confirmation snapshot.
                # -------------------------------------------------

                if signal not in (
                    "BUY",
                    "SELL"
                ):

                    continue

                # Fetch several very recent 1m/5m ticks
                # through ticker snapshots to avoid immediately
                # firing from the 5M signal alone.

                confirm_prices = []

                for _ in range(3):

                    p = get_ticker(
                        symbol
                    )

                    confirm_prices.append(
                        p
                    )

                    time.sleep(5)

                if signal == "BUY":

                    bullish_count = sum(
                        1
                        for i in range(
                            1,
                            len(
                                confirm_prices
                            )
                        )
                        if confirm_prices[i]
                        >=
                        confirm_prices[i - 1]
                    )

                    confirmed = (
                        bullish_count >= 1
                        and confirm_prices[-1]
                        > structure[
                            "resistance"
                        ]
                    )

                else:

                    bearish_count = sum(
                        1
                        for i in range(
                            1,
                            len(
                                confirm_prices
                            )
                        )
                        if confirm_prices[i]
                        <=
                        confirm_prices[i - 1]
                    )

                    confirmed = (
                        bearish_count >= 1
                        and confirm_prices[-1]
                        <
                        structure[
                            "support"
                        ]
                    )

                if not confirmed:

                    log(
                        f"[15S NOT CONFIRMED] "
                        f"{symbol} | "
                        f"{signal}"
                    )

                    continue

                log(
                    f"[SIGNAL CONFIRMED] "
                    f"{symbol} | "
                    f"{signal} | "
                    f"5M BOS + 15S CONFIRMATION"
                )

                # -------------------------------------------------
                # EXECUTE
                # -------------------------------------------------

                result = execute_order(
                    symbol,
                    signal
                )

                with state_lock:

                    state[
                        symbol
                    ].update({

                        "status":
                            result.get(
                                "status"
                            ),

                        "order":
                            result
                    })

                log(
                    f"[TRADE RESULT] "
                    f"{symbol} | "
                    f"{json.dumps(result, default=str)}"
                )

            except Exception as error:

                log(
                    f"[{symbol} ERROR] "
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
                    ]["status"] = (
                        "ERROR"
                    )

                    state[
                        symbol
                    ]["error"] = str(
                        error
                    )

        time.sleep(5)


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
font-family:Arial;
background:#101216;
color:#eee;
margin:0;
padding:14px;
}

h2{
margin-top:0;
}

.card{
background:#1a1e24;
border:1px solid #303640;
border-radius:10px;
padding:12px;
margin-bottom:10px;
}

table{
width:100%;
border-collapse:collapse;
font-size:12px;
}

th,td{
padding:8px;
border-bottom:1px solid #303640;
text-align:left;
white-space:nowrap;
}

.buy{
color:#43d17a;
font-weight:bold;
}

.sell{
color:#ff6262;
font-weight:bold;
}

.exec{
color:#43d17a;
font-weight:bold;
}

.wait{
color:#ffd166;
font-weight:bold;
}

.err{
color:#ff6262;
font-weight:bold;
}

.wrap{
overflow:auto;
}

</style>

</head>

<body>

<h2>OKX 5M + 15S Scalping Bot</h2>

<div id="top"></div>

<div class="wrap">

<table>

<thead>

<tr>

<th>Pair</th>
<th>5M Signal</th>
<th>Price</th>
<th>Resistance</th>
<th>Support</th>
<th>Status</th>
<th>Position</th>
<th>Entry</th>
<th>Order ID</th>

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
'</b> | Auto Trade: <b>' +
esc(s.auto_trade) +
'</b> | Margin: <b>$' +
esc(s.margin) +
'</b> | Leverage: <b>' +
esc(s.leverage) +
'x</b> | WhatsApp: <b>OFF</b>' +

'</div>';

let html="";

for(
const [sym,x]
of Object.entries(s.symbols)
){

let sig=x.signal || "NONE";

let cls =
sig==="BUY"
? "buy"
: sig==="SELL"
? "sell"
: "";

let status =
x.status || "WAITING";

let statusClass =
status==="EXECUTED"
? "exec"
: status==="ERROR"
? "err"
: "wait";

let order="";

if(x.order){

order =
x.order.ordId || "-";

}

html +=

"<tr>" +

"<td>"+esc(sym)+"</td>" +

'<td class="'+cls+'">'+
esc(sig)+
"</td>" +

"<td>"+
esc(x.price)+
"</td>" +

"<td>"+
esc(x.resistance)+
"</td>" +

"<td>"+
esc(x.support)+
"</td>" +

'<td class="'+statusClass+'">'+
esc(status)+
"</td>" +

"<td>"+
esc(x.position)+
"</td>" +

"<td>"+
esc(x.avgPx)+
"</td>" +

"<td>"+
esc(order)+
"</td>" +

"</tr>";

}

document.getElementById(
"rows"
).innerHTML = html;

}
catch(e){

document.getElementById(
"top"
).innerHTML =
'<div class="card err">'+
'Dashboard Error: '+
esc(e)+
'</div>';

}

}

refresh();

setInterval(
refresh,
3000
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
            k: v.copy()
            for k,v in state.items()
            if k in SYMBOLS
        }

    return jsonify({

        "bot":
            "OKX 5M + 15S Scalper",

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

        "whatsapp":
            False,

        "port":
            int(
                os.getenv(
                    "PORT",
                    "8080"
                )
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
            AUTO_TRADE,

        "whatsapp":
            False

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
