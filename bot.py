import os
import time
import json
import hmac
import base64
import hashlib
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify
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

RSI_FAST = int(os.getenv("RSI_FAST", "14"))
RSI_SLOW = int(os.getenv("RSI_SLOW", "100"))
EMA_PERIOD = int(os.getenv("EMA_PERIOD", "20"))

SL_PERCENT = Decimal(os.getenv("SL_PERCENT", "0.4"))
TP_PERCENT = Decimal(os.getenv("TP_PERCENT", "0.8"))

EMA_MODE = os.getenv("EMA_MODE", "true").lower() == "true"
EMA_BUY_RSI_MAX = Decimal(os.getenv("EMA_BUY_RSI_MAX", "55"))
EMA_SELL_RSI_MIN = Decimal(os.getenv("EMA_SELL_RSI_MIN", "45"))

USE_ADX = os.getenv("USE_ADX", "true").lower() == "true"
ADX_PERIOD = int(os.getenv("ADX_PERIOD", "14"))
ADX_MIN = Decimal(os.getenv("ADX_MIN", "18"))

USE_VOLUME = os.getenv("USE_VOLUME", "true").lower() == "true"
VOLUME_PERIOD = int(os.getenv("VOLUME_PERIOD", "20"))
VOLUME_MULT = Decimal(os.getenv("VOLUME_MULT", "0.8"))

USE_ATR = os.getenv("USE_ATR", "true").lower() == "true"
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
ATR_MIN_PCT = Decimal(os.getenv("ATR_MIN_PCT", "0.05"))

USE_15M_TREND = os.getenv("USE_15M_TREND", "true").lower() == "true"

SYMBOLS = [
    s.strip()
    for s in os.getenv(
        "SYMBOLS",
        "BTC-USDT-SWAP,ETH-USDT-SWAP,XRP-USDT-SWAP,DOGE-USDT-SWAP"
    ).split(",")
    if s.strip()
]

session = requests.Session()
app = Flask(__name__)

instrument_cache = {}
last_candle = {}


def utc_iso():
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def sign(ts, method, path, body=""):
    message = ts + method.upper() + path + body
    digest = hmac.new(
        SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode()


def private_request(method, path, payload=None, params=None):
    if not API_KEY or not SECRET_KEY or not PASSPHRASE:
        raise RuntimeError("OKX API credentials are missing")

    body = json.dumps(
        payload,
        separators=(",", ":")
    ) if payload else ""

    ts = utc_iso()

    headers = {
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": sign(ts, method, path, body),
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "OK-ACCESS-TIMESTAMP": ts
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


def get_instrument(inst_id):
    if inst_id in instrument_cache:
        return instrument_cache[inst_id]

    rows = public_get(
        "/api/v5/public/instruments",
        {
            "instType": "SWAP",
            "instId": inst_id
        }
    )["data"]

    if not rows:
        raise RuntimeError(f"Instrument not found: {inst_id}")

    x = rows[0]

    info = {
        "ctVal": Decimal(x["ctVal"]),
        "ctValCcy": x["ctValCcy"],
        "lotSz": Decimal(x["lotSz"]),
        "minSz": Decimal(x["minSz"]),
        "tickSz": Decimal(x["tickSz"])
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


def get_candles(inst_id, bar="5m", limit=160):
    rows = public_get(
        "/api/v5/market/candles",
        {
            "instId": inst_id,
            "bar": bar,
            "limit": str(limit)
        }
    )["data"]

    candles = []

    for x in reversed(rows):
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


def rsi_series(values, period):
    result = [None] * len(values)

    if len(values) <= period:
        return result

    gains = []
    losses = []

    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]

        gains.append(max(diff, Decimal("0")))
        losses.append(max(-diff, Decimal("0")))

    avg_gain = sum(
        gains[:period],
        Decimal("0")
    ) / Decimal(period)

    avg_loss = sum(
        losses[:period],
        Decimal("0")
    ) / Decimal(period)

    def rsi_value(gain, loss):
        if loss == 0:
            return Decimal("100")

        return Decimal("100") - (
            Decimal("100") /
            (Decimal("1") + gain / loss)
        )

    result[period] = rsi_value(
        avg_gain,
        avg_loss
    )

    for j in range(period, len(gains)):
        avg_gain = (
            (avg_gain * Decimal(period - 1))
            + gains[j]
        ) / Decimal(period)

        avg_loss = (
            (avg_loss * Decimal(period - 1))
            + losses[j]
        ) / Decimal(period)

        result[j + 1] = rsi_value(
            avg_gain,
            avg_loss
        )

    return result


def ema_series(values, period):
    result = [None] * len(values)

    if len(values) < period:
        return result

    multiplier = Decimal("2") / Decimal(period + 1)

    ema = sum(
        values[:period],
        Decimal("0")
    ) / Decimal(period)

    result[period - 1] = ema

    for i in range(period, len(values)):
        ema = (
            values[i] * multiplier
            + ema * (Decimal("1") - multiplier)
        )

        result[i] = ema

    return result


def atr_series(candles, period):
    result = [None] * len(candles)

    if len(candles) <= period:
        return result

    tr = [None]

    for i in range(1, len(candles)):
        true_range = max(
            candles[i]["high"] - candles[i]["low"],
            abs(
                candles[i]["high"]
                - candles[i - 1]["close"]
            ),
            abs(
                candles[i]["low"]
                - candles[i - 1]["close"]
            )
        )

        tr.append(true_range)

    atr = sum(
        tr[1:period + 1],
        Decimal("0")
    ) / Decimal(period)

    result[period] = atr

    for i in range(period + 1, len(candles)):
        atr = (
            (atr * Decimal(period - 1))
            + tr[i]
        ) / Decimal(period)

        result[i] = atr

    return result


def adx_series(candles, period):
    result = [None] * len(candles)

    if len(candles) < 2 * period + 1:
        return result

    trs = [None] * len(candles)
    plus_dm = [None] * len(candles)
    minus_dm = [None] * len(candles)

    for i in range(1, len(candles)):
        up_move = (
            candles[i]["high"]
            - candles[i - 1]["high"]
        )

        down_move = (
            candles[i - 1]["low"]
            - candles[i]["low"]
        )

        trs[i] = max(
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

        plus_dm[i] = (
            up_move
            if up_move > down_move and up_move > 0
            else Decimal("0")
        )

        minus_dm[i] = (
            down_move
            if down_move > up_move and down_move > 0
            else Decimal("0")
        )

    atr = sum(
        trs[1:period + 1],
        Decimal("0")
    ) / Decimal(period)

    plus = sum(
        plus_dm[1:period + 1],
        Decimal("0")
    ) / Decimal(period)

    minus = sum(
        minus_dm[1:period + 1],
        Decimal("0")
    ) / Decimal(period)

    dx_values = []

    for i in range(period, len(candles)):
        if i > period:
            atr = (
                atr * Decimal(period - 1)
                + trs[i]
            ) / Decimal(period)

            plus = (
                plus * Decimal(period - 1)
                + plus_dm[i]
            ) / Decimal(period)

            minus = (
                minus * Decimal(period - 1)
                + minus_dm[i]
            ) / Decimal(period)

        plus_di = (
            Decimal("100") * plus / atr
            if atr else Decimal("0")
        )

        minus_di = (
            Decimal("100") * minus / atr
            if atr else Decimal("0")
        )

        denominator = plus_di + minus_di

        dx = (
            Decimal("100")
            * abs(plus_di - minus_di)
            / denominator
            if denominator
            else Decimal("0")
        )

        dx_values.append(dx)

        if len(dx_values) >= period:
            result[i] = (
                sum(
                    dx_values[-period:],
                    Decimal("0")
                )
                / Decimal(period)
            )

    return result


def trend_direction(candles):
    candles = [
        x for x in candles
        if x["confirm"] == "1"
    ]

    if len(candles) < EMA_PERIOD + 2:
        return None

    closes = [
        x["close"]
        for x in candles
    ]

    ema = ema_series(
        closes,
        EMA_PERIOD
    )

    i = len(candles) - 1

    if ema[i] is None:
        return None

    if (
        closes[i] > ema[i]
        and ema[i] > ema[i - 1]
    ):
        return "bull"

    if (
        closes[i] < ema[i]
        and ema[i] < ema[i - 1]
    ):
        return "bear"

    return "flat"


def calculate_signal(candles, trend15=None):
    candles = [
        x for x in candles
        if x["confirm"] == "1"
    ]

    required = max(
        RSI_SLOW,
        EMA_PERIOD,
        VOLUME_PERIOD,
        ATR_PERIOD,
        ADX_PERIOD
    ) + 5

    if len(candles) < required:
        return None

    closes = [
        x["close"]
        for x in candles
    ]

    i = len(candles) - 1

    rsi14 = rsi_series(
        closes,
        RSI_FAST
    )

    rsi100 = rsi_series(
        closes,
        RSI_SLOW
    )

    ema20 = ema_series(
        closes,
        EMA_PERIOD
    )

    atr = atr_series(
        candles,
        ATR_PERIOD
    )

    adx = adx_series(
        candles,
        ADX_PERIOD
    )

    if any(
        value[i] is None
        or value[i - 1] is None
        for value in (
            rsi14,
            rsi100,
            ema20
        )
    ):
        return None

    signal = None
    reason = None

    # RSI 14/100 primary signal
    if (
        rsi14[i - 1] <= rsi100[i - 1]
        and rsi14[i] > rsi100[i]
    ):
        signal = "buy"
        reason = "RSI_CROSS"

    elif (
        rsi14[i - 1] >= rsi100[i - 1]
        and rsi14[i] < rsi100[i]
    ):
        signal = "sell"
        reason = "RSI_CROSS"

    # EMA20 alternative signal
    if signal is None and EMA_MODE:

        if (
            closes[i - 1] <= ema20[i - 1]
            and closes[i] > ema20[i]
            and rsi14[i] <= EMA_BUY_RSI_MAX
        ):
            signal = "buy"
            reason = "EMA20_RECLAIM"

        elif (
            closes[i - 1] >= ema20[i - 1]
            and closes[i] < ema20[i]
            and rsi14[i] >= EMA_SELL_RSI_MIN
        ):
            signal = "sell"
            reason = "EMA20_REJECTION"

    if signal is None:
        return None

    # 15-minute confirmation
    if USE_15M_TREND:
        if trend15 == "bull" and signal != "buy":
            return None

        if trend15 == "bear" and signal != "sell":
            return None

    # ADX
    if USE_ADX:
        if adx[i] is None or adx[i] < ADX_MIN:
            return None

    # Volume
    if USE_VOLUME:
        average_volume = (
            sum(
                x["volume"]
                for x in candles[
                    -(VOLUME_PERIOD + 1):-1
                ]
            )
            / Decimal(VOLUME_PERIOD)
        )

        if (
            candles[i]["volume"]
            < average_volume * VOLUME_MULT
        ):
            return None

    # ATR
    if USE_ATR:
        if atr[i] is None:
            return None

        atr_percent = (
            atr[i]
            / closes[i]
            * Decimal("100")
        )

        if atr_percent < ATR_MIN_PCT:
            return None

    return {
        "signal": signal,
        "reason": reason,
        "entry": closes[i],
        "rsi14": rsi14[i],
        "rsi100": rsi100[i],
        "ema20": ema20[i],
        "adx": adx[i],
        "atr": atr[i],
        "trend15": trend15
    }


def get_positions(inst_id):
    rows = private_request(
        "GET",
        "/api/v5/account/positions",
        params={"instId": inst_id}
    )["data"]

    return [
        p for p in rows
        if Decimal(p.get("pos", "0")) != 0
    ]


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


def contract_size(inst_id, entry):
    info = get_instrument(inst_id)

    if info["ctValCcy"].upper() == "USDT":
        value_per_contract = info["ctVal"]
    else:
        value_per_contract = (
            info["ctVal"] * entry
        )

    target_position = (
        MARGIN_USDT * LEVERAGE
    )

    size = round_down(
        target_position / value_per_contract,
        info["lotSz"]
    )

    return max(
        size,
        info["minSz"]
    )


def open_position(inst_id, signal):
    entry = signal["entry"]

    info = get_instrument(inst_id)

    size = contract_size(
        inst_id,
        entry
    )

    set_leverage(inst_id)

    if signal["signal"] == "buy":

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

        side = "buy"
        position_side = "long"

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

        side = "sell"
        position_side = "short"

    sl = round_down(
        sl,
        info["tickSz"]
    )

    tp = round_down(
        tp,
        info["tickSz"]
    )

    order = {
        "instId": inst_id,
        "tdMode": TD_MODE,
        "side": side,
        "posSide": position_side,
        "ordType": "market",
        "sz": str(size),
        "clOrdId": (
            "v3"
            + str(int(time.time() * 1000))
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
        "signal": signal["signal"],
        "reason": signal["reason"],
        "entry": str(entry),
        "sl": str(sl),
        "tp": str(tp),
        "size": str(size),
        "position": position_side,
        "result": result
    }


def close_position(inst_id, position):
    side = (
        "sell"
        if position["posSide"] == "long"
        else "buy"
    )

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


def execute(inst_id, signal):
    positions = get_positions(inst_id)

    desired = (
        "long"
        if signal["signal"] == "buy"
        else "short"
    )

    if any(
        p["posSide"] == desired
        for p in positions
    ):
        return {
            "status": "ignored",
            "message": "Already in desired direction"
        }

    for position in positions:
        close_position(
            inst_id,
            position
        )

    return open_position(
        inst_id,
        signal
    )


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "demo": DEMO,
        "symbols": SYMBOLS,
        "timeframe": BAR,
        "trend_timeframe": TREND_BAR,
        "margin": str(MARGIN_USDT),
        "leverage": str(LEVERAGE),
        "sl": str(SL_PERCENT),
        "tp": str(TP_PERCENT),
        "adx_min": str(ADX_MIN),
        "volume_multiplier": str(VOLUME_MULT),
        "atr_min_percent": str(ATR_MIN_PCT),
        "ema20": EMA_MODE,
        "15m_confirmation": USE_15M_TREND
    })


def worker():

    print(
        "OKX RSI + EMA20 + ADX + ATR + Volume V3 started"
    )

    print(
        f"Demo={DEMO} "
        f"Margin=${MARGIN_USDT} "
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
                    x for x in candles
                    if x["confirm"] == "1"
                ]

                if not confirmed:
                    continue

                candle_time = confirmed[-1]["ts"]

                if last_candle.get(symbol) == candle_time:
                    continue

                last_candle[symbol] = candle_time

                if USE_15M_TREND:

                    trend15 = trend_direction(
                        get_candles(
                            symbol,
                            TREND_BAR,
                            80
                        )
                    )

                else:
                    trend15 = None

                signal = calculate_signal(
                    candles,
                    trend15
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
