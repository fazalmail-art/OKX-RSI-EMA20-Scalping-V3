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

load_dotenv()

BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com")
API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
DEMO = os.getenv("OKX_DEMO", "true").lower() == "true"

BAR = os.getenv("BAR", "5m")
TREND_BAR = os.getenv("TREND_BAR", "15m")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))

MARGIN_USDT = Decimal(os.getenv("MARGIN_USDT", "20"))
LEVERAGE = Decimal(os.getenv("LEVERAGE", "5"))
TD_MODE = os.getenv("TD_MODE", "isolated")

SL_PERCENT = Decimal(os.getenv("SL_PERCENT", "0.4"))
TP_PERCENT = Decimal(os.getenv("TP_PERCENT", "0.8"))

ADX_MIN = Decimal(os.getenv("ADX_MIN", "18"))
VOLUME_MULT = Decimal(os.getenv("VOLUME_MULT", "0.8"))
ATR_MIN_PCT = Decimal(os.getenv("ATR_MIN_PCT", "0.05"))

SYMBOLS = [
    x.strip()
    for x in os.getenv(
        "SYMBOLS",
        "BTC-USDT-SWAP,ETH-USDT-SWAP,XRP-USDT-SWAP,DOGE-USDT-SWAP"
    ).split(",")
    if x.strip()
]

session = requests.Session()
app = Flask(__name__)

instrument_cache = {}
last_candle = {}


def utc_iso():
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def sign(timestamp, method, path, body=""):
    message = timestamp + method.upper() + path + body

    digest = hmac.new(
        SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256
    ).digest()

    return base64.b64encode(digest).decode()


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
            f"OKX {data.get('code')}: {data.get('msg')}"
        )

    return data


def private_request(method, path, payload=None, params=None):

    if not API_KEY or not SECRET_KEY or not PASSPHRASE:
        raise RuntimeError(
            "OKX API credentials are missing"
        )

    body = (
        json.dumps(
            payload,
            separators=(",", ":")
        )
        if payload
        else ""
    )

    timestamp = utc_iso()

    headers = {
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": sign(
            timestamp,
            method,
            path,
            body
        ),
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "OK-ACCESS-TIMESTAMP": timestamp
    }

    if DEMO:
        headers["x-simulated-trading"] = "1"

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
            f"OKX {data.get('code')}: {data.get('msg')}"
        )

    return data


def get_candles(inst_id, bar=BAR, limit=160):

    data = public_get(
        "/api/v5/market/candles",
        {
            "instId": inst_id,
            "bar": bar,
            "limit": str(limit)
        }
    )

    candles = []

    for x in reversed(data["data"]):

        candles.append({
            "ts": int(x[0]),
            "open": Decimal(x[1]),
            "high": Decimal(x[2]),
            "low": Decimal(x[3]),
            "close": Decimal(x[4]),
            "volume": Decimal(x[5]),
            "confirm": x[8] if len(x) > 8 else "1"
        })

    return candles


def ema(values, period):

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

    for i in range(period, len(values)):

        value = (
            values[i] * multiplier
            + value * (Decimal("1") - multiplier)
        )

        result[i] = value

    return result


def rsi(values, period):

    result = [None] * len(values)

    if len(values) <= period:
        return result

    gains = []
    losses = []

    for i in range(1, len(values)):

        difference = (
            values[i] - values[i - 1]
        )

        gains.append(
            max(difference, Decimal("0"))
        )

        losses.append(
            max(-difference, Decimal("0"))
        )

    average_gain = (
        sum(gains[:period], Decimal("0"))
        / Decimal(period)
    )

    average_loss = (
        sum(losses[:period], Decimal("0"))
        / Decimal(period)
    )

    def calculate(gain, loss):

        if loss == 0:
            return Decimal("100")

        return (
            Decimal("100")
            - (
                Decimal("100")
                / (
                    Decimal("1")
                    + gain / loss
                )
            )
        )

    result[period] = calculate(
        average_gain,
        average_loss
    )

    for j in range(period, len(gains)):

        average_gain = (
            average_gain * (period - 1)
            + gains[j]
        ) / Decimal(period)

        average_loss = (
            average_loss * (period - 1)
            + losses[j]
        ) / Decimal(period)

        result[j + 1] = calculate(
            average_gain,
            average_loss
        )

    return result


def atr(candles, period=14):

    result = [None] * len(candles)

    if len(candles) <= period:
        return result

    true_ranges = [None]

    for i in range(1, len(candles)):

        true_range = max(
            candles[i]["high"]
            - candles[i]["low"],

            abs(
                candles[i]["high"]
                - candles[i - 1]["close"]
            ),

            abs(
                candles[i]["low"]
                - candles[i - 1]["close"]
            )
        )

        true_ranges.append(true_range)

    value = (
        sum(
            true_ranges[1:period + 1],
            Decimal("0")
        )
        / Decimal(period)
    )

    result[period] = value

    for i in range(period + 1, len(candles)):

        value = (
            value * (period - 1)
            + true_ranges[i]
        ) / Decimal(period)

        result[i] = value

    return result


def adx_simple(candles, period=14):

    if len(candles) < period + 2:
        return None

    upward = Decimal("0")
    downward = Decimal("0")
    ranges = Decimal("0")

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
            upward += up_move

        if (
            down_move > up_move
            and down_move > 0
        ):
            downward += down_move

        ranges += (
            candles[i]["high"]
            - candles[i]["low"]
        )

    if ranges == 0:
        return Decimal("0")

    plus_di = (
        upward / ranges * Decimal("100")
    )

    minus_di = (
        downward / ranges * Decimal("100")
    )

    total = plus_di + minus_di

    if total == 0:
        return Decimal("0")

    return (
        abs(plus_di - minus_di)
        / total
        * Decimal("100")
    )


def get_15m_trend(symbol):

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

    ema_values = ema(
        closes,
        20
    )

    i = len(candles) - 1

    if (
        ema_values[i] is None
        or ema_values[i - 1] is None
    ):
        return "flat"

    if (
        closes[i] > ema_values[i]
        and ema_values[i] > ema_values[i - 1]
    ):
        return "bull"

    if (
        closes[i] < ema_values[i]
        and ema_values[i] < ema_values[i - 1]
    ):
        return "bear"

    return "flat"


def calculate_signal(symbol):

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
        return None

    closes = [
        x["close"]
        for x in candles
    ]

    rsi14 = rsi(
        closes,
        14
    )

    rsi100 = rsi(
        closes,
        100
    )

    ema20 = ema(
        closes,
        20
    )

    atr_values = atr(
        candles,
        14
    )

    adx = adx_simple(
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
        or atr_values[i] is None
        or adx is None
    ):
        return None

    buy_rsi = (
        rsi14[i - 1] <= rsi100[i - 1]
        and rsi14[i] > rsi100[i]
    )

    sell_rsi = (
        rsi14[i - 1] >= rsi100[i - 1]
        and rsi14[i] < rsi100[i]
    )

    buy_ema = (
        closes[i - 1] <= ema20[i - 1]
        and closes[i] > ema20[i]
        and rsi14[i] <= 55
    )

    sell_ema = (
        closes[i - 1] >= ema20[i - 1]
        and closes[i] < ema20[i]
        and rsi14[i] >= 45
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
            x["volume"]
            for x in candles[-21:-1]
        )
        / Decimal("20")
    )

    volume_ok = (
        candles[i]["volume"]
        >= average_volume * VOLUME_MULT
    )

    atr_percent = (
        atr_values[i]
        / closes[i]
        * Decimal("100")
    )

    if adx < ADX_MIN:
        return None

    if not volume_ok:
        return None

    if atr_percent < ATR_MIN_PCT:
        return None

    trend = get_15m_trend(
        symbol
    )

    if (
        trend == "bull"
        and signal != "buy"
    ):
        return None

    if (
        trend == "bear"
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
        "atr": atr_values[i],
        "trend15": trend
    }


def get_instrument(inst_id):

    if inst_id in instrument_cache:
        return instrument_cache[inst_id]

    data = public_get(
        "/api/v5/public/instruments",
        {
            "instType": "SWAP",
            "instId": inst_id
        }
    )

    if not data["data"]:
        raise RuntimeError(
            f"Instrument not found: {inst_id}"
        )

    item = data["data"][0]

    info = {
        "ctVal": Decimal(item["ctVal"]),
        "ctValCcy": item["ctValCcy"],
        "lotSz": Decimal(item["lotSz"]),
        "minSz": Decimal(item["minSz"]),
        "tickSz": Decimal(item["tickSz"])
    }

    instrument_cache[inst_id] = info

    return info


def round_down(value, step):

    if step <= 0:
        return value

    return (
        value / step
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * step


def set_leverage(inst_id):

    return private_request(
        "POST",
        "/api/v5/account/set-leverage",
        {
            "instId": inst_id,
            "lever": str(LEVERAGE),
            "mgnMode": TD_MODE
        }
    )


def get_position_size(
    inst_id,
    price
):

    info = get_instrument(
        inst_id
    )

    if (
        info["ctValCcy"].upper()
        == "USDT"
    ):
        contract_value = info["ctVal"]

    else:
        contract_value = (
            info["ctVal"]
            * price
        )

    target_value = (
        MARGIN_USDT
        * LEVERAGE
    )

    size = round_down(
        target_value / contract_value,
        info["lotSz"]
    )

    return max(
        size,
        info["minSz"]
    )


def get_positions(inst_id):

    data = private_request(
        "GET",
        "/api/v5/account/positions",
        params={
            "instId": inst_id
        }
    )

    return [
        position
        for position in data["data"]
        if Decimal(
            position.get(
                "pos",
                "0"
            )
        ) != 0
    ]


def close_position(
    inst_id,
    position
):

    if position["posSide"] == "long":
        side = "sell"
    else:
        side = "buy"

    return private_request(
        "POST",
        "/api/v5/trade/order",
        {
            "instId": inst_id,
            "tdMode": TD_MODE,
            "side": side,
            "posSide": position["posSide"],
            "ordType": "market",
            "sz": position["pos"],
            "reduceOnly": "true"
        }
    )


def open_position(
    symbol,
    signal
):

    entry = signal["entry"]

    info = get_instrument(
        symbol
    )

    size = get_position_size(
        symbol,
        entry
    )

    set_leverage(
        symbol
    )

    if signal["signal"] == "buy":

        side = "buy"
        pos_side = "long"

        sl = (
            entry
            * (
                Decimal("1")
                - SL_PERCENT
                / Decimal("100")
            )
        )

        tp = (
            entry
            * (
                Decimal("1")
                + TP_PERCENT
                / Decimal("100")
            )
        )

    else:

        side = "sell"
        pos_side = "short"

        sl = (
            entry
            * (
                Decimal("1")
                + SL_PERCENT
                / Decimal("100")
            )
        )

        tp = (
            entry
            * (
                Decimal("1")
                - TP_PERCENT
                / Decimal("100")
            )
        )

    sl = round_down(
        sl,
        info["tickSz"]
    )

    tp = round_down(
        tp,
        info["tickSz"]
    )

    order = {
        "instId": symbol,
        "tdMode": TD_MODE,
        "side": side,
        "posSide": pos_side,
        "ordType": "market",
        "sz": str(size),
        "clOrdId": (
            "v3"
            + str(
                int(
                    time.time() * 1000
                )
            )
        )[-32:],
        "attachAlgoOrds": [
            {
                "tpTriggerPx": str(tp),
                "tpOrdPx": "-1",
                "tpTriggerPxType": "mark",
                "slTriggerPx": str(sl),
                "slOrdPx": "-1",
                "slTriggerPxType": "mark"
            }
        ]
    }

    result = private_request(
        "POST",
        "/api/v5/trade/order",
        order
    )

    return {
        "status": "opened",
        "symbol": symbol,
        "signal": signal["signal"],
        "reason": signal["reason"],
        "entry": str(entry),
        "sl": str(sl),
        "tp": str(tp),
        "size": str(size),
        "result": result
    }


def execute(
    symbol,
    signal
):

    positions = get_positions(
        symbol
    )

    wanted = (
        "long"
        if signal["signal"] == "buy"
        else "short"
    )

    if any(
        position["posSide"] == wanted
        for position in positions
    ):
        return {
            "status": "ignored",
            "message": (
                "Position already open"
            )
        }

    for position in positions:

        close_position(
            symbol,
            position
        )

    return open_position(
        symbol,
        signal
    )


@app.get("/")
def home():

    return jsonify({
        "bot": (
            "OKX RSI EMA20 ADX ATR "
            "Volume Scalping V3"
        ),
        "status": "running",
        "demo": DEMO
    })


@app.get("/health")
def health():

    return jsonify({
        "ok": True,
        "demo": DEMO,
        "margin_usdt": str(
            MARGIN_USDT
        ),
        "leverage": str(
            LEVERAGE
        ),
        "sl_percent": str(
            SL_PERCENT
        ),
        "tp_percent": str(
            TP_PERCENT
        ),
        "symbols": SYMBOLS,
        "bar": BAR,
        "trend_bar": TREND_BAR
    })


def worker():

    print(
        "OKX RSI + EMA20 + ADX + ATR "
        "+ Volume V3 started"
    )

    print(
        f"Demo={DEMO}, "
        f"Margin=${MARGIN_USDT}, "
        f"Leverage={LEVERAGE}x"
    )

    while True:

        for symbol in SYMBOLS:

            try:

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

                if not confirmed:
                    continue

                candle_time = (
                    confirmed[-1]["ts"]
                )

                if (
                    last_candle.get(symbol)
                    == candle_time
                ):
                    continue

                last_candle[symbol] = (
                    candle_time
                )

                signal = calculate_signal(
                    symbol
                )

                if signal:

                    print(
                        f"{datetime.now()} "
                        f"{symbol}: "
                        f"{signal['signal'].upper()} "
                        f"[{signal['reason']}] "
                        f"ADX={signal['adx']} "
                        f"Trend15={signal['trend15']}"
                    )

                    result = execute(
                        symbol,
                        signal
                    )

                    print(
                        json.dumps(
                            result,
                            default=str
                        )
                    )

            except Exception as error:

                print(
                    f"[{symbol}] ERROR: "
                    f"{error}"
                )

        time.sleep(
            POLL_SECONDS
        )


if __name__ == "__main__":

    import threading

    threading.Thread(
        target=worker,
        daemon=True
    ).start()

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "8080"
            )
        )
    )
