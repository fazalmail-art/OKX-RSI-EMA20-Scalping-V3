import os
import json
import time
import hmac
import base64
import hashlib
import threading
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd
import websocket
from flask import Flask, jsonify, render_template_string


# ============================================================
# ICT SWIFTEDGE - OKX FUTURES SCALPER
# 5M STRUCTURE + 15S EXECUTION
# SOL / BTC / ETH / HYPE / XRP / DOGE
# ============================================================

OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://us.okx.com")

# US OKX DEMO BUSINESS WS
OKX_WS_BUSINESS = os.getenv(
    "OKX_WS_BUSINESS",
    "wss://wsuspap.okx.com:8443/ws/v5/business"
)

OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

OKX_DEMO = os.getenv("OKX_DEMO", "true").lower() == "true"

# IMPORTANT:
# false = signal only
# true  = actual OKX demo orders
AUTO_TRADE = os.getenv("AUTO_TRADE", "true").lower() == "true"

# Live trading remains blocked unless explicitly enabled.
ALLOW_LIVE = os.getenv("ALLOW_LIVE", "false").lower() == "true"

# ============================================================
# SYMBOLS
# ============================================================

REQUESTED_SYMBOLS = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "HYPE-USDT-SWAP",
    "XRP-USDT-SWAP",
    "DOGE-USDT-SWAP",
]

# ============================================================
# MONEY / LEVERAGE
# ============================================================

# Target margin per trade
MARGIN_USDT = float(os.getenv("MARGIN_USDT", "10"))

# Position target = margin x leverage
LEVERAGE = int(os.getenv("LEVERAGE", "3"))

TD_MODE = os.getenv("TD_MODE", "isolated")

# If an instrument's minimum contract size is larger than
# the requested $30 notional, DO NOT automatically oversize.
# This prevents unexpected large positions.
ALLOW_MIN_SIZE_OVERSIZE = (
    os.getenv("ALLOW_MIN_SIZE_OVERSIZE", "false").lower() == "true"
)

# ============================================================
# STRATEGY
# ============================================================

STRUCTURE_LOOKBACK = int(
    os.getenv("STRUCTURE_LOOKBACK", "100")
)

PIVOT_LEFT = int(os.getenv("PIVOT_LEFT", "2"))
PIVOT_RIGHT = int(os.getenv("PIVOT_RIGHT", "2"))

RSI_LENGTH = 14
RSI_MA_LENGTH = 7

# 0.015 = 0.015%
BREAK_BUFFER_PCT = float(
    os.getenv("BREAK_BUFFER_PCT", "0.015")
)

MIN_BODY_RATIO = float(
    os.getenv("MIN_BODY_RATIO", "0.35")
)

MIN_VOLUME_RATIO = float(
    os.getenv("MIN_VOLUME_RATIO", "1.00")
)

# ============================================================
# EXIT
# ============================================================

MIN_HOLD_SECONDS = 3
MAX_HOLD_SECONDS = 30

TRAIL_START_SECONDS = 12
TRAIL_ATR_MULT = 0.75
EMERGENCY_SL_ATR = 1.60

COOLDOWN_SECONDS = 45

MAX_DAILY_LOSS_USDT = float(
    os.getenv("MAX_DAILY_LOSS_USDT", "30")
)

MAX_CONSECUTIVE_LOSSES = int(
    os.getenv("MAX_CONSECUTIVE_LOSSES", "4")
)

# ============================================================
# SERVER
# ============================================================

PORT = int(os.getenv("PORT", "8080"))

# ============================================================
# WHATSAPP
# ============================================================

WHATSAPP_ACCESS_TOKEN = os.getenv(
    "WHATSAPP_ACCESS_TOKEN", ""
)

WHATSAPP_PHONE_NUMBER_ID = os.getenv(
    "WHATSAPP_PHONE_NUMBER_ID", ""
)

WHATSAPP_TO_NUMBER = os.getenv(
    "WHATSAPP_TO_NUMBER", ""
)

WHATSAPP_API_VERSION = os.getenv(
    "WHATSAPP_API_VERSION", ""
)


# ============================================================
# APP
# ============================================================

app = Flask(__name__)

state = {
    "status": "STARTING",
    "ws_connected": False,
    "last_data": None,
    "last_signal": None,
    "last_order": None,
    "last_error": None,

    "daily_pnl": 0.0,

    "trades": 0,
    "wins": 0,
    "losses": 0,

    "consecutive_losses": 0,

    "signals": 0,

    "started_at": datetime.now(
        timezone.utc
    ).isoformat()
}

valid_symbols = []

positions = {}

last_trade_time = {}

one_second_data = {}

candles_15s = {}

instrument_cache = {}

symbol_status = {}

log_history = []

data_lock = threading.Lock()


# ============================================================
# LOGGING
# ============================================================

def log(message):
    text = (
        f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] "
        f"{message}"
    )

    print(text, flush=True)

    log_history.append(text)

    if len(log_history) > 300:
        del log_history[:-300]


# ============================================================
# TIME / SIGN
# ============================================================

def timestamp():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def sign(ts, method, path, body=""):
    message = (
        ts +
        method.upper() +
        path +
        body
    )

    digest = hmac.new(
        OKX_SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256
    ).digest()

    return base64.b64encode(
        digest
    ).decode()


def headers(method, path, body=""):
    ts = timestamp()

    h = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign(
            ts,
            method,
            path,
            body
        ),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }

    if OKX_DEMO:
        h["x-simulated-trading"] = "1"

    return h


# ============================================================
# REST
# ============================================================

def public_get(path, params=None):

    try:

        r = requests.get(
            OKX_BASE_URL + path,
            params=params,
            timeout=10
        )

        result = r.json()

        if result.get("code") not in (None, "0", 0):

            log(
                f"[REST WARNING] {path} "
                f"{result}"
            )

        return result

    except Exception as e:

        state["last_error"] = str(e)

        log(
            f"[REST ERROR] {path} {e}"
        )

        return {}


def private_post(path, payload):

    body = json.dumps(
        payload,
        separators=(",", ":")
    )

    try:

        r = requests.post(
            OKX_BASE_URL + path,
            headers=headers(
                "POST",
                path,
                body
            ),
            data=body,
            timeout=10
        )

        result = r.json()

        state["last_order"] = result

        return result

    except Exception as e:

        state["last_error"] = str(e)

        log(
            f"[PRIVATE REST ERROR] {e}"
        )

        return {}


# ============================================================
# API CHECK
# ============================================================

def api_ready():

    if not OKX_API_KEY:
        log("[API ERROR] OKX_API_KEY missing")
        return False

    if not OKX_SECRET_KEY:
        log("[API ERROR] OKX_SECRET_KEY missing")
        return False

    if not OKX_PASSPHRASE:
        log("[API ERROR] OKX_PASSPHRASE missing")
        return False

    if not OKX_DEMO and not ALLOW_LIVE:
        log("[SAFETY] LIVE trading blocked")
        return False

    return True


# ============================================================
# SYMBOL VALIDATION
# ============================================================

def validate_symbols():

    global valid_symbols

    valid_symbols = []

    log("Checking OKX SWAP instruments...")

    for symbol in REQUESTED_SYMBOLS:

        result = public_get(
            "/api/v5/public/instruments",
            {
                "instType": "SWAP",
                "instId": symbol
            }
        )

        data = result.get(
            "data",
            []
        )

        if not data:

            symbol_status[symbol] = {
                "status": "UNAVAILABLE"
            }

            log(
                f"[SYMBOL SKIP] {symbol}"
            )

            continue

        inst = data[0]

        if inst.get("state") != "live":

            symbol_status[symbol] = {
                "status": inst.get(
                    "state",
                    "not_live"
                )
            }

            log(
                f"[SYMBOL SKIP] "
                f"{symbol} state="
                f"{inst.get('state')}"
            )

            continue

        valid_symbols.append(symbol)

        instrument_cache[symbol] = inst

        symbol_status[symbol] = {
            "status": "OK",
            "ctVal": inst.get("ctVal"),
            "lotSz": inst.get("lotSz"),
            "minSz": inst.get("minSz"),
            "ctType": inst.get("ctType")
        }

        one_second_data.setdefault(
            symbol,
            []
        )

        candles_15s.setdefault(
            symbol,
            []
        )

        last_trade_time.setdefault(
            symbol,
            0
        )

        log(
            f"[SYMBOL OK] {symbol} | "
            f"ctVal={inst.get('ctVal')} | "
            f"lotSz={inst.get('lotSz')} | "
            f"minSz={inst.get('minSz')} | "
            f"type={inst.get('ctType')}"
        )

    log(
        "[ACTIVE SYMBOLS] " +
        (
            ", ".join(valid_symbols)
            if valid_symbols
            else "NONE"
        )
    )

    return valid_symbols


# ============================================================
# INSTRUMENT
# ============================================================

def get_instrument(symbol):

    if symbol in instrument_cache:
        return instrument_cache[symbol]

    result = public_get(
        "/api/v5/public/instruments",
        {
            "instType": "SWAP",
            "instId": symbol
        }
    )

    data = result.get(
        "data",
        []
    )

    if not data:
        return None

    instrument_cache[symbol] = data[0]

    return data[0]


# ============================================================
# DECIMAL ROUNDING
# ============================================================

def decimal_floor(value, step):

    value = Decimal(str(value))
    step = Decimal(str(step))

    if step <= 0:
        return value

    units = (
        value / step
    ).to_integral_value(
        rounding=ROUND_DOWN
    )

    return units * step


def format_decimal(value):

    s = format(
        Decimal(str(value)),
        "f"
    )

    if "." in s:
        s = s.rstrip("0").rstrip(".")

    return s


# ============================================================
# CORRECT CONTRACT SIZE
# ============================================================

def calculate_size(symbol, price):

    inst = get_instrument(symbol)

    if not inst:
        log(
            f"[SIZE ERROR] "
            f"{symbol}: instrument unavailable"
        )
        return 0, 0, 0

    ct_val = float(
        inst.get("ctVal", "0")
    )

    lot_sz = float(
        inst.get("lotSz", "1")
    )

    min_sz = float(
        inst.get("minSz", "1")
    )

    ct_type = inst.get(
        "ctType",
        "linear"
    )

    if ct_val <= 0:
        log(
            f"[SIZE ERROR] "
            f"{symbol}: invalid ctVal"
        )
        return 0, 0, 0

    if price <= 0:
        return 0, 0, 0

    target_notional = (
        MARGIN_USDT *
        LEVERAGE
    )

    # Linear:
    # notional = contracts x ctVal x price
    if ct_type == "linear":

        raw_contracts = (
            target_notional /
            (price * ct_val)
        )

    else:

        # Inverse contracts use a different
        # notional calculation.
        raw_contracts = (
            target_notional /
            ct_val
        )

    contracts = decimal_floor(
        raw_contracts,
        lot_sz
    )

    minimum = Decimal(
        str(min_sz)
    )

    if contracts < minimum:

        if not ALLOW_MIN_SIZE_OVERSIZE:

            minimum_notional = (
                float(minimum) *
                ct_val *
                price
                if ct_type == "linear"
                else
                float(minimum) *
                ct_val
            )

            log(
                f"[SIZE BLOCKED] {symbol} | "
                f"Target≈{target_notional:.2f} USDT | "
                f"Minimum≈{minimum_notional:.2f} USDT | "
                f"Need larger margin or leverage"
            )

            return 0, 0, 0

        contracts = minimum

    if ct_type == "linear":

        actual_notional = (
            float(contracts) *
            ct_val *
            price
        )

    else:

        actual_notional = (
            float(contracts) *
            ct_val
        )

    actual_margin = (
        actual_notional /
        LEVERAGE
    )

    return (
        float(contracts),
        float(actual_notional),
        float(actual_margin)
    )


# ============================================================
# 5M CANDLES
# ============================================================

def get_5m_candles(symbol):

    result = public_get(
        "/api/v5/market/candles",
        {
            "instId": symbol,
            "bar": "5m",
            "limit": str(
                STRUCTURE_LOOKBACK + 20
            )
        }
    )

    rows = result.get(
        "data",
        []
    )

    if not rows:
        return pd.DataFrame()

    rows = list(
        reversed(rows)
    )

    records = []

    for r in rows:

        if len(r) < 6:
            continue

        records.append({
            "ts": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5])
        })

    df = pd.DataFrame(
        records
    )

    return add_indicators(df)


# ============================================================
# INDICATORS
# ============================================================

def EMA(series, length):

    return series.ewm(
        span=length,
        adjust=False
    ).mean()


def RSI(series, length=14):

    delta = series.diff()

    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )

    avg_gain = gain.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()

    rs = (
        avg_gain /
        avg_loss.replace(
            0,
            np.nan
        )
    )

    value = (
        100 -
        (100 / (1 + rs))
    )

    return value.fillna(50)


def ATR(df, length=14):

    hl = (
        df["high"] -
        df["low"]
    )

    hc = (
        df["high"] -
        df["close"].shift()
    ).abs()

    lc = (
        df["low"] -
        df["close"].shift()
    ).abs()

    tr = pd.concat(
        [hl, hc, lc],
        axis=1
    ).max(axis=1)

    return tr.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()


def add_indicators(df):

    if df.empty:
        return df

    df["ema20"] = EMA(
        df["close"],
        20
    )

    df["ema50"] = EMA(
        df["close"],
        50
    )

    df["rsi"] = RSI(
        df["close"],
        RSI_LENGTH
    )

    df["rsi_ma"] = (
        df["rsi"]
        .rolling(
            RSI_MA_LENGTH
        )
        .mean()
    )

    df["atr"] = ATR(
        df,
        14
    )

    df["volume_ma"] = (
        df["volume"]
        .rolling(20)
        .mean()
    )

    df["volume_ratio"] = (
        df["volume"] /
        df["volume_ma"].replace(
            0,
            np.nan
        )
    )

    return df


# ============================================================
# PIVOTS
# ============================================================

def pivots(df):

    highs = []
    lows = []

    if len(df) < 15:
        return highs, lows

    for i in range(
        PIVOT_LEFT,
        len(df) - PIVOT_RIGHT
    ):

        h = df["high"].iloc[i]
        l = df["low"].iloc[i]

        left_h = df[
            "high"
        ].iloc[
            i-PIVOT_LEFT:i
        ]

        right_h = df[
            "high"
        ].iloc[
            i+1:i+1+PIVOT_RIGHT
        ]

        left_l = df[
            "low"
        ].iloc[
            i-PIVOT_LEFT:i
        ]

        right_l = df[
            "low"
        ].iloc[
            i+1:i+1+PIVOT_RIGHT
        ]

        if (
            h > left_h.max()
            and
            h > right_h.max()
        ):
            highs.append(
                (i, h)
            )

        if (
            l < left_l.min()
            and
            l < right_l.min()
        ):
            lows.append(
                (i, l)
            )

    return highs, lows


# ============================================================
# 5M STRUCTURE
# ============================================================

def analyze_structure(df):

    if len(df) < 25:
        return None

    highs, lows = pivots(df)

    resistance = (
        highs[-1][1]
        if highs
        else None
    )

    support = (
        lows[-1][1]
        if lows
        else None
    )

    close = float(
        df["close"].iloc[-1]
    )

    direction = "NONE"

    if (
        resistance
        and
        close >
        resistance *
        (
            1 +
            BREAK_BUFFER_PCT / 100
        )
    ):
        direction = "BUY"

    elif (
        support
        and
        close <
        support *
        (
            1 -
            BREAK_BUFFER_PCT / 100
        )
    ):
        direction = "SELL"

    return {
        "direction": direction,
        "resistance": resistance,
        "support": support,
        "close": close,
        "atr": float(
            df["atr"].iloc[-1]
        )
    }


# ============================================================
# 1 SECOND DATA
# ============================================================

def add_1s(symbol, candle):

    with data_lock:

        one_second_data[
            symbol
        ].append(candle)

        one_second_data[
            symbol
        ] = one_second_data[
            symbol
        ][-600:]


# ============================================================
# 15 SECOND AGGREGATION
# ============================================================

def build_15s(symbol):

    with data_lock:

        rows = list(
            one_second_data[
                symbol
            ]
        )

    if not rows:
        return False

    buckets = {}

    for r in rows:

        bucket = (
            r["ts"] // 15000
        ) * 15000

        buckets.setdefault(
            bucket,
            []
        ).append(r)

    completed = []

    current_bucket = (
        int(
            time.time() * 1000
        ) // 15000
    ) * 15000

    for bucket, items in buckets.items():

        if bucket >= current_bucket:
            continue

        # Need enough 1-second updates
        if len(items) < 10:
            continue

        items.sort(
            key=lambda x: x["ts"]
        )

        completed.append({
            "ts": bucket,
            "open": items[0]["open"],
            "high": max(
                x["high"]
                for x in items
            ),
            "low": min(
                x["low"]
                for x in items
            ),
            "close": items[-1]["close"],
            "volume": sum(
                x["volume"]
                for x in items
            )
        })

    if not completed:
        return False

    df = pd.DataFrame(
        completed
    )

    df = (
        df
        .drop_duplicates("ts")
        .sort_values("ts")
    )

    df = add_indicators(df)

    with data_lock:

        existing = pd.DataFrame(
            candles_15s[symbol]
        )

        combined = pd.concat(
            [existing, df],
            ignore_index=True
        )

        if not combined.empty:

            combined = (
                combined
                .drop_duplicates(
                    "ts"
                )
                .sort_values("ts")
                .tail(150)
            )

        candles_15s[
            symbol
        ] = combined.to_dict(
            "records"
        )

    return True


# ============================================================
# 15S ENTRY
# ============================================================

def find_entry(symbol, structure):

    with data_lock:

        rows = list(
            candles_15s[
                symbol
            ]
        )

    if len(rows) < 20:
        return None

    df = pd.DataFrame(
        rows
    )

    df = add_indicators(df)

    current = df.iloc[-1]
    previous = df.iloc[-2]

    direction = structure[
        "direction"
    ]

    if direction not in (
        "BUY",
        "SELL"
    ):
        return None

    candle_range = (
        current["high"] -
        current["low"]
    )

    if candle_range <= 0:
        return None

    body = abs(
        current["close"] -
        current["open"]
    )

    body_ratio = (
        body /
        candle_range
    )

    if body_ratio < MIN_BODY_RATIO:
        return None

    volume_ratio = current[
        "volume_ratio"
    ]

    if (
        pd.notna(volume_ratio)
        and
        volume_ratio <
        MIN_VOLUME_RATIO
    ):
        return None

    atr = current["atr"]

    if pd.isna(atr) or atr <= 0:
        return None

    # -------------------------
    # BUY
    # -------------------------

    if direction == "BUY":

        bullish = (
            current["close"] >
            current["open"]
        )

        momentum = (
            current["close"] >
            previous["close"]
        )

        rsi_ok = (
            current["rsi"] > 50
            and
            current["rsi"] >
            current["rsi_ma"]
        )

        ema_ok = (
            current["close"] >
            current["ema20"]
        )

        if (
            bullish
            and momentum
            and rsi_ok
            and ema_ok
        ):

            return {
                "side": "buy",
                "price": float(
                    current["close"]
                ),
                "atr": float(atr),
                "reason":
                    "5M BOS + 15S bullish confirmation"
            }

    # -------------------------
    # SELL
    # -------------------------

    if direction == "SELL":

        bearish = (
            current["close"] <
            current["open"]
        )

        momentum = (
            current["close"] <
            previous["close"]
        )

        rsi_ok = (
            current["rsi"] < 50
            and
            current["rsi"] <
            current["rsi_ma"]
        )

        ema_ok = (
            current["close"] <
            current["ema20"]
        )

        if (
            bearish
            and momentum
            and rsi_ok
            and ema_ok
        ):

            return {
                "side": "sell",
                "price": float(
                    current["close"]
                ),
                "atr": float(atr),
                "reason":
                    "5M BOS + 15S bearish confirmation"
            }

    return None


# ============================================================
# MARKET ORDER
# ============================================================

def market_order(
    symbol,
    side,
    size,
    reduce_only=False
):

    if not api_ready():
        return None

    payload = {
        "instId": symbol,
        "tdMode": TD_MODE,
        "side": side,
        "ordType": "market",
        "sz": format_decimal(size)
    }

    if reduce_only:
        payload["reduceOnly"] = True

    log(
        f"[ORDER REQUEST] "
        f"{symbol} | "
        f"{side.upper()} | "
        f"contracts={size} | "
        f"reduceOnly={reduce_only}"
    )

    result = private_post(
        "/api/v5/trade/order",
        payload
    )

    log(
        f"[ORDER RESPONSE] "
        f"{result}"
    )

    data = result.get(
        "data",
        []
    )

    if not data:
        log(
            "[ORDER FAILED] No data"
        )
        return None

    item = data[0]

    if item.get("sCode") != "0":

        log(
            "[ORDER FAILED] " +
            str(item)
        )

        return None

    log(
        f"[ORDER ACCEPTED] "
        f"ordId={item.get('ordId')}"
    )

    return item


# ============================================================
# OPEN POSITION
# ============================================================

def open_position(symbol, signal):

    if symbol in positions:

        log(
            f"[WAIT] {symbol} "
            f"already has position"
        )

        return

    now = time.time()

    if (
        now -
        last_trade_time.get(
            symbol,
            0
        )
        <
        COOLDOWN_SECONDS
    ):

        log(
            f"[WAIT] {symbol} cooldown"
        )

        return

    if (
        state["daily_pnl"]
        <=
        -MAX_DAILY_LOSS_USDT
    ):

        log(
            "[RISK STOP] "
            "Daily loss limit"
        )

        return

    if (
        state["consecutive_losses"]
        >=
        MAX_CONSECUTIVE_LOSSES
    ):

        log(
            "[RISK STOP] "
            "Consecutive losses"
        )

        return

    price = signal[
        "price"
    ]

    (
        size,
        actual_notional,
        actual_margin
    ) = calculate_size(
        symbol,
        price
    )

    if size <= 0:

        log(
            f"[NO TRADE] {symbol} "
            f"because target size is "
            f"below OKX minimum"
        )

        return

    state["signals"] += 1

    log(
        f"[SIGNAL] {symbol} | "
        f"{signal['side'].upper()} | "
        f"price={price} | "
        f"contracts={size} | "
        f"notional≈{actual_notional:.4f} USDT | "
        f"margin≈{actual_margin:.4f} USDT"
    )

    if not AUTO_TRADE:

        log(
            f"🟡 SIGNAL ONLY | "
            f"{symbol} | "
            f"{signal['side'].upper()}"
        )

        send_whatsapp(
            "🟡 OKX SIGNAL\n"
            f"{symbol}\n"
            f"Side: {signal['side'].upper()}\n"
            f"Price: {price}\n"
            f"Contracts: {size}\n"
            f"Notional: {actual_notional:.4f} USDT\n"
            f"Margin: {actual_margin:.4f} USDT\n"
            f"{signal['reason']}"
        )

        return

    order = market_order(
        symbol,
        signal["side"],
        size
    )

    if not order:
        return

    positions[symbol] = {

        "side": signal["side"],

        "entry": price,

        "size": size,

        "notional": actual_notional,

        "margin": actual_margin,

        "atr": signal["atr"],

        "time": time.time(),

        "best_price": price,

        "ord_id": order.get(
            "ordId"
        )
    }

    last_trade_time[
        symbol
    ] = time.time()

    state["trades"] += 1

    log(
        f"🟢 POSITION OPENED | "
        f"{symbol} | "
        f"{signal['side'].upper()} | "
        f"entry={price} | "
        f"contracts={size} | "
        f"notional≈{actual_notional:.4f}"
    )

    send_whatsapp(
        "🟢 OKX DEMO TRADE OPENED\n"
        f"{symbol}\n"
        f"Side: {signal['side'].upper()}\n"
        f"Entry: {price}\n"
        f"Contracts: {size}\n"
        f"Notional: {actual_notional:.4f} USDT\n"
        f"Margin: {actual_margin:.4f} USDT"
    )


# ============================================================
# CLOSE POSITION
# ============================================================

def close_position(
    symbol,
    price,
    reason
):

    position = positions.get(
        symbol
    )

    if not position:
        return

    side = position[
        "side"
    ]

    close_side = (
        "sell"
        if side == "buy"
        else "buy"
    )

    if AUTO_TRADE:

        order = market_order(
            symbol,
            close_side,
            position["size"],
            reduce_only=True
        )

        if not order:

            log(
                f"[CLOSE FAILED] "
                f"{symbol}"
            )

            return

    entry = position[
        "entry"
    ]

    # Approximate PnL.
    # Exchange fees/slippage are not included.
    if side == "buy":

        pnl = (
            price - entry
        ) * position["size"]

    else:

        pnl = (
            entry - price
        ) * position["size"]

    state["daily_pnl"] += pnl

    if pnl >= 0:

        state["wins"] += 1

        state[
            "consecutive_losses"
        ] = 0

    else:

        state["losses"] += 1

        state[
            "consecutive_losses"
        ] += 1

    log(
        f"🔴 POSITION CLOSED | "
        f"{symbol} | "
        f"entry={entry} | "
        f"exit={price} | "
        f"PnL≈{pnl:.4f} | "
        f"reason={reason}"
    )

    send_whatsapp(
        "🔴 OKX DEMO TRADE CLOSED\n"
        f"{symbol}\n"
        f"Side: {side.upper()}\n"
        f"Entry: {entry}\n"
        f"Exit: {price}\n"
        f"Estimated PnL: {pnl:.4f} USDT\n"
        f"Reason: {reason}"
    )

    del positions[
        symbol
    ]


# ============================================================
# POSITION MANAGEMENT
# ============================================================

def manage_position(symbol):

    position = positions.get(
        symbol
    )

    if not position:
        return

    with data_lock:

        rows = list(
            candles_15s[
                symbol
            ]
        )

    if not rows:
        return

    df = pd.DataFrame(
        rows
    )

    df = add_indicators(df)

    current = df.iloc[-1]

    price = float(
        current["close"]
    )

    atr = float(
        current["atr"]
    )

    age = (
        time.time() -
        position["time"]
    )

    side = position[
        "side"
    ]

    entry = position[
        "entry"
    ]

    if side == "buy":

        if price > position[
            "best_price"
        ]:

            position[
                "best_price"
            ] = price

        emergency_sl = (
            entry -
            atr *
            EMERGENCY_SL_ATR
        )

        trailing_sl = (
            position[
                "best_price"
            ] -
            atr *
            TRAIL_ATR_MULT
        )

        if price <= emergency_sl:

            close_position(
                symbol,
                price,
                "Emergency ATR SL"
            )

            return

        if (
            age >=
            TRAIL_START_SECONDS
            and
            price <= trailing_sl
        ):

            close_position(
                symbol,
                price,
                "Trailing exit"
            )

            return

        if (
            age >= MIN_HOLD_SECONDS
            and
            price <
            current["ema20"]
        ):

            close_position(
                symbol,
                price,
                "15S momentum failure"
            )

            return

    else:

        if price < position[
            "best_price"
        ]:

            position[
                "best_price"
            ] = price

        emergency_sl = (
            entry +
            atr *
            EMERGENCY_SL_ATR
        )

        trailing_sl = (
            position[
                "best_price"
            ] +
            atr *
            TRAIL_ATR_MULT
        )

        if price >= emergency_sl:

            close_position(
                symbol,
                price,
                "Emergency ATR SL"
            )

            return

        if (
            age >=
            TRAIL_START_SECONDS
            and
            price >= trailing_sl
        ):

            close_position(
                symbol,
                price,
                "Trailing exit"
            )

            return

        if (
            age >= MIN_HOLD_SECONDS
            and
            price >
            current["ema20"]
        ):

            close_position(
                symbol,
                price,
                "15S momentum failure"
            )

            return

    if age >= MAX_HOLD_SECONDS:

        close_position(
            symbol,
            price,
            "Maximum 30 second hold"
        )


# ============================================================
# WHATSAPP
# ============================================================

def send_whatsapp(message):

    if not all([
        WHATSAPP_ACCESS_TOKEN,
        WHATSAPP_PHONE_NUMBER_ID,
        WHATSAPP_TO_NUMBER,
        WHATSAPP_API_VERSION
    ]):

        return False

    url = (
        "https://graph.facebook.com/"
        f"{WHATSAPP_API_VERSION}/"
        f"{WHATSAPP_PHONE_NUMBER_ID}/messages"
    )

    payload = {
        "messaging_product":
            "whatsapp",

        "to":
            WHATSAPP_TO_NUMBER,

        "type":
            "text",

        "text": {
            "body": message
        }
    }

    try:

        r = requests.post(
            url,
            headers={
                "Authorization":
                    "Bearer " +
                    WHATSAPP_ACCESS_TOKEN,

                "Content-Type":
                    "application/json"
            },
            json=payload,
            timeout=10
        )

        log(
            f"[WHATSAPP] "
            f"{r.status_code}"
        )

        return r.ok

    except Exception as e:

        log(
            f"[WHATSAPP ERROR] {e}"
        )

        return False


# ============================================================
# WEBSOCKET MESSAGE
# ============================================================

def ws_message(
    ws,
    message
):

    try:

        obj = json.loads(
            message
        )

        if obj.get(
            "event"
        ) == "error":

            log(
                f"❌ WS ERROR: "
                f"{obj}"
            )

            state[
                "last_error"
            ] = str(obj)

            return

        if obj.get(
            "event"
        ) == "subscribe":

            log(
                f"✅ WS SUBSCRIBED: "
                f"{obj.get('arg')}"
            )

            return

        arg = obj.get(
            "arg",
            {}
        )

        channel = arg.get(
            "channel"
        )

        symbol = arg.get(
            "instId"
        )

        if channel != "candle1s":
            return

        if symbol not in valid_symbols:
            return

        data = obj.get(
            "data",
            []
        )

        if not data:
            return

        r = data[0]

        candle = {
            "ts": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5])
        }

        add_1s(
            symbol,
            candle
        )

        state[
            "last_data"
        ] = {
            "symbol": symbol,
            "price": candle[
                "close"
            ],
            "time": candle[
                "ts"
            ]
        }

        built = build_15s(
            symbol
        )

        if built:

            log(
                f"[15S] {symbol} "
                f"candle formed"
            )

        manage_position(
            symbol
        )

    except Exception as e:

        state[
            "last_error"
        ] = str(e)

        log(
            f"❌ WS MESSAGE ERROR: "
            f"{e}"
        )


# ============================================================
# WEBSOCKET OPEN
# ============================================================

def ws_open(ws):

    state[
        "ws_connected"
    ] = True

    state[
        "status"
    ] = "RUNNING"

    log(
        "=========================================="
    )

    log(
        "✅ OKX WEBSOCKET CONNECTED"
    )

    log(
        f"Demo={OKX_DEMO}"
    )

    log(
        "Symbols=" +
        str(valid_symbols)
    )

    log(
        "=========================================="
    )

    args = []

    for symbol in valid_symbols:

        args.append({
            "channel":
                "candle1s",

            "instId":
                symbol
        })

    if not args:

        log(
            "❌ No valid symbols"
        )

        return

    request = {
        "op":
            "subscribe",

        "args":
            args
    }

    ws.send(
        json.dumps(request)
    )

    log(
        "📡 Subscription request sent"
    )


# ============================================================
# WEBSOCKET ERROR / CLOSE
# ============================================================

def ws_error(
    ws,
    error
):

    state[
        "ws_connected"
    ] = False

    state[
        "last_error"
    ] = str(error)

    log(
        f"❌ WS ERROR: "
        f"{error}"
    )


def ws_close(
    ws,
    code,
    message
):

    state[
        "ws_connected"
    ] = False

    log(
        f"⚠️ WS CLOSED: "
        f"{code} {message}"
    )

    log(
        "Reconnecting automatically..."
    )


# ============================================================
# WEBSOCKET LOOP
# ============================================================

def websocket_loop():

    while True:

        try:

            if not valid_symbols:

                validate_symbols()

            log(
                "Connecting to OKX WebSocket..."
            )

            ws = websocket.WebSocketApp(

                OKX_WS_BUSINESS,

                on_open=ws_open,

                on_message=ws_message,

                on_error=ws_error,

                on_close=ws_close
            )

            ws.run_forever(
                ping_interval=15,
                ping_timeout=10
            )

        except Exception as e:

            state[
                "last_error"
            ] = str(e)

            log(
                f"❌ WS LOOP ERROR: "
                f"{e}"
            )

        state[
            "ws_connected"
        ] = False

        log(
            "⏳ WebSocket reconnect "
            "in 5 seconds..."
        )

        time.sleep(5)


# ============================================================
# STRUCTURE LOOP
# ============================================================

def structure_loop():

    last_status = {}

    while True:

        try:

            for symbol in valid_symbols:

                df = get_5m_candles(
                    symbol
                )

                if df.empty:

                    log(
                        f"[5M] {symbol}: "
                        "no candle data"
                    )

                    continue

                # Remove current unfinished 5M candle
                if len(df) > 1:

                    df = (
                        df
                        .iloc[:-1]
                        .copy()
                    )

                structure = (
                    analyze_structure(
                        df
                    )
                )

                if not structure:
                    continue

                direction = (
                    structure[
                        "direction"
                    ]
                )

                price = (
                    structure[
                        "close"
                    ]
                )

                # Only log structure changes.
                if (
                    last_status.get(
                        symbol
                    )
                    !=
                    direction
                ):

                    log(
                        f"[5M STRUCTURE] "
                        f"{symbol} | "
                        f"{direction} | "
                        f"price={price} | "
                        f"R={structure['resistance']} | "
                        f"S={structure['support']}"
                    )

                    last_status[
                        symbol
                    ] = direction

                signal = find_entry(
                    symbol,
                    structure
                )

                if signal:

                    log(
                        f"🚨 SIGNAL FOUND | "
                        f"{symbol} | "
                        f"{signal['side'].upper()} | "
                        f"price={signal['price']} | "
                        f"{signal['reason']}"
                    )

                    state[
                        "last_signal"
                    ] = {
                        "symbol":
                            symbol,

                        **signal
                    }

                    open_position(
                        symbol,
                        signal
                    )

        except Exception as e:

            state[
                "last_error"
            ] = str(e)

            log(
                f"❌ STRUCTURE ERROR: "
                f"{e}"
            )

        time.sleep(5)


# ============================================================
# DASHBOARD
# ============================================================

DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html>
<head>

<meta charset="UTF-8">

<meta name="viewport"
      content="width=device-width,
               initial-scale=1.0">

<title>ICT SwiftEdge</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    font-family:
        Arial,
        sans-serif;

    background:
        linear-gradient(
            135deg,
            #0f172a,
            #111827,
            #020617
        );

    color: #e5e7eb;
}

.container {
    max-width: 1200px;
    margin: auto;
    padding: 18px;
}

.header {
    padding: 22px;
    border-radius: 20px;

    background:
        rgba(255,255,255,0.06);

    border:
        1px solid
        rgba(255,255,255,0.10);

    box-shadow:
        0 10px 35px
        rgba(0,0,0,0.25);

    margin-bottom: 18px;
}

.title {
    font-size: 28px;
    font-weight: 800;
}

.subtitle {
    color: #94a3b8;
    margin-top: 5px;
}

.badge {
    display: inline-block;
    margin-top: 12px;
    padding: 7px 13px;
    border-radius: 20px;
    background: #172554;
    color: #93c5fd;
}

.grid {
    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(170px, 1fr)
        );

    gap: 12px;

    margin-bottom: 18px;
}

.card {
    background:
        rgba(255,255,255,0.06);

    border:
        1px solid
        rgba(255,255,255,0.09);

    border-radius: 18px;

    padding: 18px;

    min-height: 105px;
}

.label {
    color: #94a3b8;
    font-size: 13px;
}

.value {
    font-size: 25px;
    font-weight: 800;
    margin-top: 8px;
}

.green {
    color: #4ade80;
}

.red {
    color: #fb7185;
}

.yellow {
    color: #facc15;
}

.blue {
    color: #60a5fa;
}

.section {
    margin-top: 18px;
    margin-bottom: 10px;
    font-size: 18px;
    font-weight: 800;
}

.symbols {
    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(180px, 1fr)
        );

    gap: 10px;
}

.symbol {
    background:
        rgba(255,255,255,0.05);

    border-radius: 15px;

    padding: 15px;

    border:
        1px solid
        rgba(255,255,255,0.08);
}

.symbol-name {
    font-weight: 800;
}

.symbol-status {
    margin-top: 7px;
    font-size: 13px;
    color: #94a3b8;
}

.position {
    padding: 15px;
    border-radius: 15px;

    background:
        rgba(255,255,255,0.05);

    margin-bottom: 10px;
}

.logs {
    height: 300px;
    overflow-y: auto;

    background: #020617;

    border-radius: 15px;

    padding: 15px;

    font-family:
        monospace;

    font-size: 12px;

    color: #cbd5e1;
}

.footer {
    text-align: center;
    color: #64748b;
    padding: 25px;
}

</style>

</head>

<body>

<div class="container">

<div class="header">

<div class="title">
    ⚡ ICT SwiftEdge
</div>

<div class="subtitle">
    OKX Futures Scalper • 5M Structure • 15S Execution
</div>

<div id="mode"
     class="badge">
    Loading...
</div>

</div>


<div class="grid">

<div class="card">
<div class="label">
Bot Status
</div>
<div id="status"
     class="value">
-
</div>
</div>

<div class="card">
<div class="label">
WebSocket
</div>
<div id="ws"
     class="value">
-
</div>
</div>

<div class="card">
<div class="label">
Trades
</div>
<div id="trades"
     class="value">
0
</div>
</div>

<div class="card">
<div class="label">
Win Rate
</div>
<div id="winrate"
     class="value">
0%
</div>
</div>

<div class="card">
<div class="label">
Daily PnL
</div>
<div id="pnl"
     class="value">
0
</div>
</div>

<div class="card">
<div class="label">
Signals
</div>
<div id="signals"
     class="value">
0
</div>
</div>

</div>


<div class="section">
📊 Markets
</div>

<div id="symbols"
     class="symbols">
</div>


<div class="section">
📌 Open Positions
</div>

<div id="positions">
No open positions
</div>


<div class="section">
🚨 Last Signal
</div>

<div class="card"
     id="lastSignal">
No signal yet
</div>


<div class="section">
🧾 Live Logs
</div>

<div class="logs"
     id="logs">
Loading logs...
</div>


<div class="footer">
ICT SwiftEdge • OKX
</div>

</div>


<script>

async function updateDashboard() {

    try {

        const response =
            await fetch('/api/status');

        const data =
            await response.json();


        document.getElementById(
            'status'
        ).innerText =
            data.status || '-';


        document.getElementById(
            'ws'
        ).innerText =
            data.ws_connected
            ? 'CONNECTED'
            : 'OFFLINE';


        document.getElementById(
            'ws'
        ).className =
            data.ws_connected
            ? 'value green'
            : 'value red';


        document.getElementById(
            'trades'
        ).innerText =
            data.trades || 0;


        let total =
            (data.wins || 0) +
            (data.losses || 0);

        let winrate =
            total > 0
            ? (
                (data.wins / total)
                * 100
              ).toFixed(1)
            : '0';


        document.getElementById(
            'winrate'
        ).innerText =
            winrate + '%';


        const pnl =
            Number(
                data.daily_pnl || 0
            );


        document.getElementById(
            'pnl'
        ).innerText =
            pnl.toFixed(4);


        document.getElementById(
            'pnl'
        ).className =
            pnl >= 0
            ? 'value green'
            : 'value red';


        document.getElementById(
            'signals'
        ).innerText =
            data.signals || 0;


        document.getElementById(
            'mode'
        ).innerText =
            data.demo
            ? '🟢 OKX DEMO'
            : '🔴 LIVE';


        // Symbols

        let symbolsHTML = '';

        (data.symbols || [])
        .forEach(
            function(symbol) {

                symbolsHTML += `
                <div class="symbol">

                    <div class="symbol-name">
                        ${symbol}
                    </div>

                    <div class="symbol-status">
                        ACTIVE
                    </div>

                </div>
                `;

            }
        );


        if (!symbolsHTML) {

            symbolsHTML =
                '<div class="card">No active symbols</div>';
        }


        document.getElementById(
            'symbols'
        ).innerHTML =
            symbolsHTML;


        // Positions

        let positions =
            data.positions || {};

        let positionHTML = '';


        Object.keys(
            positions
        ).forEach(
            function(symbol) {

                let p =
                    positions[symbol];

                positionHTML += `
                <div class="position">

                    <b>${symbol}</b>

                    <br>

                    Side:
                    <span class="${
                        p.side === 'buy'
                        ? 'green'
                        : 'red'
                    }">
                        ${p.side.toUpperCase()}
                    </span>

                    <br>

                    Entry:
                    ${p.entry}

                    <br>

                    Contracts:
                    ${p.size}

                    <br>

                    Notional:
                    ${Number(
                        p.notional || 0
                    ).toFixed(4)}
                    USDT

                    <br>

                    Margin:
                    ${Number(
                        p.margin || 0
                    ).toFixed(4)}
                    USDT

                </div>
                `;

            }
        );


        if (!positionHTML) {

            positionHTML =
                '<div class="card">No open positions</div>';
        }


        document.getElementById(
            'positions'
        ).innerHTML =
            positionHTML;


        // Signal

        let s =
            data.last_signal;

        if (s) {

            document.getElementById(
                'lastSignal'
            ).innerHTML = `

                <b>
                    ${s.symbol}
                </b>

                <br>

                Side:
                <span class="${
                    s.side === 'buy'
                    ? 'green'
                    : 'red'
                }">
                    ${s.side.toUpperCase()}
                </span>

                <br>

                Price:
                ${s.price}

                <br>

                Reason:
                ${s.reason || '-'}

            `;

        }


        // Logs

        let logs =
            data.logs || [];

        document.getElementById(
            'logs'
        ).innerHTML =
            logs.join('<br>');

        let logBox =
            document.getElementById(
                'logs'
            );

        logBox.scrollTop =
            logBox.scrollHeight;


    } catch (error) {

        console.log(error);

    }

}


updateDashboard();

setInterval(
    updateDashboard,
    2000
);

</script>

</body>
</html>
"""


# ============================================================
# DASHBOARD ROUTES
# ============================================================

@app.route("/")
def home():

    return render_template_string(
        DASHBOARD_HTML
    )


@app.route("/health")
def health():

    return "OK", 200


@app.route("/api/status")
def status():

    total = (
        state["wins"] +
        state["losses"]
    )

    winrate = (
        (
            state["wins"] /
            total
        ) * 100
        if total > 0
        else 0
    )

    return jsonify({

        **state,

        "demo":
            OKX_DEMO,

        "auto_trade":
            AUTO_TRADE,

        "leverage":
            LEVERAGE,

        "margin_usdt":
            MARGIN_USDT,

        "target_notional":
            MARGIN_USDT *
            LEVERAGE,

        "websocket":
            state[
                "ws_connected"
            ],

        "symbols":
            valid_symbols,

        "symbol_status":
            symbol_status,

        "positions":
            positions,

        "winrate":
            winrate,

        "logs":
            log_history[-150:]
    })


# ============================================================
# START
# ============================================================

def start():

    log("")
    log(
        "=========================================="
    )

    log(
        "🚀 ICT SWIFTEDGE OKX SCALPER"
    )

    log(
        "=========================================="
    )

    log(
        f"OKX DEMO: {OKX_DEMO}"
    )

    log(
        f"AUTO TRADE: {AUTO_TRADE}"
    )

    log(
        f"MARGIN: {MARGIN_USDT} USDT"
    )

    log(
        f"LEVERAGE: {LEVERAGE}x"
    )

    log(
        f"TARGET NOTIONAL: "
        f"{MARGIN_USDT * LEVERAGE} USDT"
    )

    log(
        f"WS URL: "
        f"{OKX_WS_BUSINESS}"
    )

    log(
        "Requested symbols: " +
        ", ".join(
            REQUESTED_SYMBOLS
        )
    )

    if not api_ready():

        log(
            "⚠️ API credentials "
            "not complete"
        )

    validate_symbols()

    threading.Thread(
        target=websocket_loop,
        daemon=True
    ).start()

    threading.Thread(
        target=structure_loop,
        daemon=True
    ).start()

    state[
        "status"
    ] = "RUNNING"

    log(
        "✅ Bot threads started"
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    start()

    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True
    )
