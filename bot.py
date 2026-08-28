from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import signal
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# ============================================================
# OKX + RAILWAY READY FUTURES BOT
# ============================================================
#
# Strategy:
#
# 1H  -> Trend direction
# 15M -> Breakout + Retest + Fake Breakout filter
#
# Indicators:
# EMA20
# EMA50
# RSI14
# ATR14
#
# Money:
# Margin = $10
# Leverage = 3x
# Max position = $30
#
# SL = 1.5 ATR
# TP = 2R
#
# Demo first.
#
# Railway:
# PORT is automatically supplied by Railway.
#
# Start command:
# python bot.py
# ============================================================


BASE_URL = os.getenv(
    "OKX_BASE_URL",
    "https://www.okx.com",
)


# ============================================================
# ENV HELPERS
# ============================================================

def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)

    if value is None or value == "":
        return default

    return value.lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)

    if value is None or value == "":
        return default

    return float(value)


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)

    if value is None or value == "":
        return default

    return int(value)


# ============================================================
# CONFIG
# ============================================================

class Config:

    # ----------------------------
    # OKX
    # ----------------------------

    inst_id = os.getenv(
        "OKX_INST_ID",
        "BTC-USDT-SWAP",
    ).upper()

    okx_demo = env_bool(
        "OKX_DEMO",
        True,
    )

    dry_run = env_bool(
        "DRY_RUN",
        True,
    )

    api_key = os.getenv(
        "OKX_API_KEY",
        "",
    )

    secret_key = os.getenv(
        "OKX_SECRET_KEY",
        "",
    )

    passphrase = os.getenv(
        "OKX_PASSPHRASE",
        "",
    )

    # ----------------------------
    # TIMEFRAMES
    # ----------------------------

    trend_bar = os.getenv(
        "TREND_BAR",
        "1H",
    )

    entry_bar = os.getenv(
        "ENTRY_BAR",
        "15m",
    )

    candle_limit = max(
        100,
        env_int(
            "CANDLE_LIMIT",
            250,
        ),
    )

    poll_seconds = max(
        15,
        env_int(
            "POLL_SECONDS",
            30,
        ),
    )

    # ----------------------------
    # MONEY
    # ----------------------------

    margin_usdt = env_float(
        "MARGIN_USDT",
        10.0,
    )

    leverage = env_float(
        "LEVERAGE",
        3.0,
    )

    max_position_usdt = env_float(
        "MAX_POSITION_USDT",
        30.0,
    )

    margin_mode = os.getenv(
        "MARGIN_MODE",
        "isolated",
    ).lower()

    # ----------------------------
    # STRATEGY
    # ----------------------------

    ema_fast = 20
    ema_slow = 50

    rsi_period = 14

    atr_period = 14

    breakout_period = 20

    stop_atr_multiplier = env_float(
        "STOP_ATR_MULTIPLIER",
        1.5,
    )

    reward_to_risk = env_float(
        "REWARD_TO_RISK",
        2.0,
    )

    retest_tolerance = env_float(
        "RETEST_TOLERANCE",
        0.002,
    )

    # ----------------------------
    # RSI
    # ----------------------------

    long_rsi_min = env_float(
        "LONG_RSI_MIN",
        50.0,
    )

    long_rsi_max = env_float(
        "LONG_RSI_MAX",
        70.0,
    )

    short_rsi_min = env_float(
        "SHORT_RSI_MIN",
        30.0,
    )

    short_rsi_max = env_float(
        "SHORT_RSI_MAX",
        50.0,
    )

    # ----------------------------
    # RAILWAY
    # ----------------------------

    port = env_int(
        "PORT",
        8080,
    )

    # ----------------------------
    # STATE
    # ----------------------------

    state_file = os.getenv(
        "STATE_FILE",
        "./data/state.json",
    )


# ============================================================
# GENERAL
# ============================================================

def now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def log_event(
    message: str,
    **fields: Any,
) -> None:

    print(
        json.dumps(
            {
                "time": now_iso(),
                "message": message,
                **fields,
            }
        ),
        flush=True,
    )


def clean_number(value: float) -> float:
    return float(
        f"{value:.12f}"
    )


def floor_to_step(
    value: float,
    step: float,
) -> float:

    if step <= 0:
        return value

    v = Decimal(str(value))
    s = Decimal(str(step))

    return float(
        (v // s) * s
    )


# ============================================================
# VALIDATION
# ============================================================

def validate_config() -> None:

    if not Config.inst_id.endswith(
        "-SWAP"
    ):
        raise ValueError(
            "OKX_INST_ID must be a SWAP instrument, "
            "for example BTC-USDT-SWAP"
        )

    if Config.margin_usdt <= 0:
        raise ValueError(
            "MARGIN_USDT must be greater than 0"
        )

    if Config.leverage <= 0:
        raise ValueError(
            "LEVERAGE must be greater than 0"
        )

    if (
        Config.margin_usdt *
        Config.leverage
        >
        Config.max_position_usdt
    ):
        raise ValueError(
            "Margin x leverage is greater than "
            "MAX_POSITION_USDT"
        )

    if Config.margin_mode not in {
        "isolated",
        "cross",
    }:
        raise ValueError(
            "MARGIN_MODE must be isolated or cross"
        )

    if not Config.dry_run:

        if not Config.api_key:
            raise ValueError(
                "OKX_API_KEY is required"
            )

        if not Config.secret_key:
            raise ValueError(
                "OKX_SECRET_KEY is required"
            )

        if not Config.passphrase:
            raise ValueError(
                "OKX_PASSPHRASE is required"
            )

        if os.getenv(
            "LIVE_TRADING_CONFIRMATION"
        ) != "I_UNDERSTAND":

            raise ValueError(
                "Set "
                "LIVE_TRADING_CONFIRMATION="
                "I_UNDERSTAND"
            )


# ============================================================
# INDICATORS
# ============================================================

def ema(
    values: list[float],
    period: int,
) -> list[float]:

    if not values:
        return []

    multiplier = 2 / (
        period + 1
    )

    result = [values[0]]

    for value in values[1:]:

        result.append(
            (
                value -
                result[-1]
            ) *
            multiplier +
            result[-1]
        )

    return result


def rsi(
    values: list[float],
    period: int = 14,
) -> list[float | None]:

    result = [
        None
    ] * len(values)

    if len(values) <= period:
        return result

    gains = 0.0
    losses = 0.0

    for i in range(
        1,
        period + 1,
    ):

        change = (
            values[i] -
            values[i - 1]
        )

        if change >= 0:
            gains += change
        else:
            losses -= change

    avg_gain = gains / period
    avg_loss = losses / period

    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = (
            avg_gain /
            avg_loss
        )

        result[period] = (
            100 -
            100 /
            (1 + rs)
        )

    for i in range(
        period + 1,
        len(values),
    ):

        change = (
            values[i] -
            values[i - 1]
        )

        gain = max(
            change,
            0,
        )

        loss = max(
            -change,
            0,
        )

        avg_gain = (
            (
                avg_gain *
                (period - 1)
            ) +
            gain
        ) / period

        avg_loss = (
            (
                avg_loss *
                (period - 1)
            ) +
            loss
        ) / period

        if avg_loss == 0:

            result[i] = 100.0

        else:

            rs = (
                avg_gain /
                avg_loss
            )

            result[i] = (
                100 -
                100 /
                (1 + rs)
            )

    return result


def atr(
    candles: list[
        dict[str, float]
    ],
    period: int = 14,
) -> list[float | None]:

    trs = []

    for i, candle in enumerate(
        candles
    ):

        if i == 0:

            trs.append(
                candle["high"] -
                candle["low"]
            )

            continue

        previous_close = candles[
            i - 1
        ]["close"]

        tr = max(

            candle["high"] -
            candle["low"],

            abs(
                candle["high"] -
                previous_close
            ),

            abs(
                candle["low"] -
                previous_close
            ),
        )

        trs.append(tr)

    result = [
        None
    ] * len(candles)

    if len(candles) <= period:
        return result

    average = (
        sum(
            trs[
                1:
                period + 1
            ]
        )
        /
        period
    )

    result[period] = average

    for i in range(
        period + 1,
        len(candles),
    ):

        average = (
            (
                average *
                (period - 1)
            )
            +
            trs[i]
        ) / period

        result[i] = average

    return result


def rolling_high(
    candles: list[
        dict[str, float]
    ],
    period: int,
) -> list[float | None]:

    result = []

    for i in range(
        len(candles)
    ):

        if i < period:

            result.append(None)

        else:

            result.append(
                max(
                    c["high"]
                    for c in candles[
                        i - period:
                        i
                    ]
                )
            )

    return result


def rolling_low(
    candles: list[
        dict[str, float]
    ],
    period: int,
) -> list[float | None]:

    result = []

    for i in range(
        len(candles)
    ):

        if i < period:

            result.append(None)

        else:

            result.append(
                min(
                    c["low"]
                    for c in candles[
                        i - period:
                        i
                    ]
                )
            )

    return result


# ============================================================
# STRATEGY
# ============================================================

def calculate_signal(
    trend_candles,
    entry_candles,
):

    if len(trend_candles) < 60:
        raise ValueError(
            "Not enough 1H candles"
        )

    if len(entry_candles) < 60:
        raise ValueError(
            "Not enough 15M candles"
        )

    # ========================================================
    # 1H TREND
    # ========================================================

    trend_closes = [
        c["close"]
        for c in trend_candles
    ]

    t_ema20 = ema(
        trend_closes,
        20,
    )

    t_ema50 = ema(
        trend_closes,
        50,
    )

    ti = len(
        trend_candles
    ) - 1

    trend_price = trend_candles[
        ti
    ]["close"]

    trend_up = (
        t_ema20[ti] >
        t_ema50[ti]
        and
        trend_price >
        t_ema50[ti]
    )

    trend_down = (
        t_ema20[ti] <
        t_ema50[ti]
        and
        trend_price <
        t_ema50[ti]
    )

    # ========================================================
    # 15M
    # ========================================================

    closes = [
        c["close"]
        for c in entry_candles
    ]

    e20 = ema(
        closes,
        20,
    )

    e50 = ema(
        closes,
        50,
    )

    rsi_values = rsi(
        closes,
        14,
    )

    atr_values = atr(
        entry_candles,
        14,
    )

    highs = rolling_high(
        entry_candles,
        20,
    )

    lows = rolling_low(
        entry_candles,
        20,
    )

    i = len(
        entry_candles
    ) - 1

    previous_i = i - 1

    current = entry_candles[
        i
    ]

    previous = entry_candles[
        previous_i
    ]

    resistance = highs[
        previous_i
    ]

    support = lows[
        previous_i
    ]

    if resistance is None:
        raise ValueError(
            "Resistance unavailable"
        )

    if support is None:
        raise ValueError(
            "Support unavailable"
        )

    if rsi_values[i] is None:
        raise ValueError(
            "RSI unavailable"
        )

    if atr_values[i] is None:
        raise ValueError(
            "ATR unavailable"
        )

    current_rsi = float(
        rsi_values[i]
    )

    # ========================================================
    # BREAKOUT
    # ========================================================

    bullish_breakout = (
        previous["close"] >
        resistance
    )

    bearish_breakdown = (
        previous["close"] <
        support
    )

    # ========================================================
    # RETEST
    # ========================================================

    long_retest = (
        current["low"]
        <=
        resistance *
        (
            1 +
            Config.retest_tolerance
        )
        and
        current["close"] >
        resistance
    )

    short_retest = (
        current["high"]
        >=
        support *
        (
            1 -
            Config.retest_tolerance
        )
        and
        current["close"] <
        support
    )

    # ========================================================
    # FAKE BREAKOUT
    # ========================================================

    fake_breakout = (
        bullish_breakout
        and
        current["close"]
        <=
        resistance
    )

    fake_breakdown = (
        bearish_breakdown
        and
        current["close"]
        >=
        support
    )

    # ========================================================
    # EMA
    # ========================================================

    ema_long = (
        e20[i] >
        e50[i]
        and
        current["close"] >
        e20[i]
    )

    ema_short = (
        e20[i] <
        e50[i]
        and
        current["close"] <
        e20[i]
    )

    # ========================================================
    # RSI
    # ========================================================

    long_rsi = (
        50 <=
        current_rsi <=
        70
    )

    short_rsi = (
        30 <=
        current_rsi <=
        50
    )

    # ========================================================
    # FINAL SIGNAL
    # ========================================================

    long_signal = (
        trend_up
        and
        bullish_breakout
        and
        long_retest
        and
        not fake_breakout
        and
        ema_long
        and
        long_rsi
    )

    short_signal = (
        trend_down
        and
        bearish_breakdown
        and
        short_retest
        and
        not fake_breakdown
        and
        ema_short
        and
        short_rsi
    )

    if long_signal:
        direction = "long"

    elif short_signal:
        direction = "short"

    else:
        direction = "none"

    # ========================================================
    # EXIT
    # ========================================================

    exit_long = (
        current["close"] <
        e20[i]
        or
        current_rsi <
        45
        or
        e20[i] <
        e50[i]
    )

    exit_short = (
        current["close"] >
        e20[i]
        or
        current_rsi >
        55
        or
        e20[i] >
        e50[i]
    )

    return {

        "timestamp":
            current["timestamp"],

        "candle":
            current,

        "direction":
            direction,

        "exit_long":
            exit_long,

        "exit_short":
            exit_short,

        "fake_breakout":
            fake_breakout,

        "fake_breakdown":
            fake_breakdown,

        "support":
            support,

        "resistance":
            resistance,

        "ema20":
            e20[i],

        "ema50":
            e50[i],

        "rsi":
            current_rsi,

        "atr":
            atr_values[i],

        "trend_ema20":
            t_ema20[ti],

        "trend_ema50":
            t_ema50[ti],
    }


# ============================================================
# OKX CLIENT
# ============================================================

class OkxClient:

    def request(
        self,
        path: str,
        method: str = "GET",
        query=None,
        body=None,
        private=False,
    ):

        query = query or {}

        query_string = urlencode(
            {
                k: v
                for k, v in query.items()
                if v is not None
                and v != ""
            }
        )

        request_path = (
            f"{path}?{query_string}"
            if query_string
            else path
        )

        payload = (
            json.dumps(
                body,
                separators=(
                    ",",
                    ":",
                ),
            )
            if body is not None
            else ""
        )

        headers = {
            "Content-Type":
                "application/json",
        }

        if private:

            timestamp = now_iso()

            prehash = (
                timestamp +
                method +
                request_path +
                payload
            )

            signature = base64.b64encode(
                hmac.new(
                    Config.secret_key.encode(),
                    prehash.encode(),
                    hashlib.sha256,
                ).digest()
            ).decode()

            headers.update(
                {
                    "OK-ACCESS-KEY":
                        Config.api_key,

                    "OK-ACCESS-SIGN":
                        signature,

                    "OK-ACCESS-TIMESTAMP":
                        timestamp,

                    "OK-ACCESS-PASSPHRASE":
                        Config.passphrase,
                }
            )

            if Config.okx_demo:

                headers[
                    "x-simulated-trading"
                ] = "1"

        request = Request(

            BASE_URL +
            request_path,

            data=(
                payload.encode()
                if payload
                else None
            ),

            headers=headers,

            method=method,
        )

        try:

            with urlopen(
                request,
                timeout=15,
            ) as response:

                result = json.loads(
                    response
                    .read()
                    .decode()
                )

        except HTTPError as error:

            detail = error.read().decode(
                errors="replace"
            )

            raise RuntimeError(
                f"OKX HTTP {error.code}: "
                f"{detail[:500]}"
            )

        except (
            URLError,
            TimeoutError,
        ) as error:

            raise RuntimeError(
                f"OKX network error: {error}"
            )

        if result.get("code") != "0":

            raise RuntimeError(
                "OKX API error: "
                f"{result.get('msg')}"
            )

        return result.get(
            "data",
            []
        )

    # ========================================================
    # CONNECTION TEST
    # ========================================================

    def test_connection(self):

        if Config.dry_run:

            # Public API test
            self.request(
                "/api/v5/public/time"
            )

            return {
                "connected":
                    True,

                "private":
                    False,

                "mode":
                    "DRY_RUN",
            }

        # Private authenticated test
        data = self.request(
            "/api/v5/account/config",
            private=True,
        )

        return {
            "connected":
                True,

            "private":
                True,

            "mode":
                "DEMO"
                if Config.okx_demo
                else "LIVE",

            "account":
                data[0]
                if data
                else {},
        }

    # ========================================================
    # ACCOUNT CONFIG
    # ========================================================

    def account_config(self):

        data = self.request(
            "/api/v5/account/config",
            private=True,
        )

        return (
            data[0]
            if data
            else {}
        )

    # ========================================================
    # CANDLES
    # ========================================================

    def candles(
        self,
        bar: str,
    ):

        data = self.request(

            "/api/v5/market/candles",

            query={
                "instId":
                    Config.inst_id,

                "bar":
                    bar,

                "limit":
                    str(
                        Config.candle_limit
                    ),
            },
        )

        candles = []

        for row in data:

            if len(row) < 9:
                continue

            # Only completed candles
            if row[8] != "1":
                continue

            candle = {

                "timestamp":
                    int(row[0]),

                "open":
                    float(row[1]),

                "high":
                    float(row[2]),

                "low":
                    float(row[3]),

                "close":
                    float(row[4]),

                "volume":
                    float(row[5]),
            }

            if all(
                math.isfinite(
                    x
                )
                for x in candle.values()
            ):

                candles.append(
                    candle
                )

        candles.sort(
            key=lambda x:
                x["timestamp"]
        )

        return candles

    # ========================================================
    # INSTRUMENT
    # ========================================================

    def instrument(self):

        data = self.request(

            "/api/v5/public/instruments",

            query={

                "instType":
                    "SWAP",

                "instId":
                    Config.inst_id,
            },
        )

        if not data:

            raise RuntimeError(
                "Instrument not found: "
                f"{Config.inst_id}"
            )

        x = data[0]

        return {

            "instId":
                x["instId"],

            "ctVal":
                float(
                    x["ctVal"]
                ),

            "lotSz":
                float(
                    x["lotSz"]
                ),

            "minSz":
                float(
                    x["minSz"]
                ),

            "tickSz":
                float(
                    x["tickSz"]
                ),
        }

    # ========================================================
    # SET LEVERAGE
    # ========================================================

    def set_leverage(self):

        data = self.request(

            "/api/v5/account/set-leverage",

            method="POST",

            body={

                "instId":
                    Config.inst_id,

                "lever":
                    str(
                        Config.leverage
                    ),

                "mgnMode":
                    Config.margin_mode,
            },

            private=True,
        )

        return data

    # ========================================================
    # POSITION
    # ========================================================

    def position(self):

        data = self.request(

            "/api/v5/account/positions",

            query={

                "instType":
                    "SWAP",

                "instId":
                    Config.inst_id,
            },

            private=True,
        )

        for p in data:

            try:
                pos = float(
                    p.get(
                        "pos",
                        "0",
                    )
                    or 0
                )
            except Exception:
                pos = 0.0

            if abs(pos) > 0:

                return p

        return None

    # ========================================================
    # MARKET ORDER + ATTACHED SL/TP
    # ========================================================

    def open_order(
        self,
        side: str,
        contracts: float,
        stop_price: float,
        target_price: float,
    ):

        body = {

            "instId":
                Config.inst_id,

            "tdMode":
                Config.margin_mode,

            "side":
                side,

            "ordType":
                "market",

            "sz":
                format(
                    contracts,
                    ".12f",
                ).rstrip(
                    "0"
                ).rstrip(
                    "."
                ),

            "posSide":
                "net",

            "clOrdId":
                (
                    "BOT"
                    +
                    str(
                        int(
                            time.time()
                            * 1000
                        )
                    )
                )[-32:],

            "attachAlgoOrds": [

                {

                    "attachAlgoClOrdId":
                        (
                            "TPSL"
                            +
                            str(
                                int(
                                    time.time()
                                    * 1000
                                )
                            )
                        )[-32:],

                    "tpTriggerPx":
                        str(
                            clean_number(
                                target_price
                            )
                        ),

                    "tpOrdPx":
                        "-1",

                    "tpTriggerPxType":
                        "mark",

                    "slTriggerPx":
                        str(
                            clean_number(
                                stop_price
                            )
                        ),

                    "slOrdPx":
                        "-1",

                    "slTriggerPxType":
                        "mark",
                }
            ],
        }

        data = self.request(

            "/api/v5/trade/order",

            method="POST",

            body=body,

            private=True,
        )

        if not data:

            raise RuntimeError(
                "OKX returned empty order"
            )

        if data[0].get(
            "sCode"
        ) not in (
            None,
            "",
            "0",
        ):

            raise RuntimeError(
                "Order rejected: "
                +
                str(
                    data[0].get(
                        "sMsg"
                    )
                )
            )

        return data[0]

    # ========================================================
    # CLOSE POSITION
    # ========================================================

    def close_order(
        self,
        side: str,
        contracts: float,
    ):

        body = {

            "instId":
                Config.inst_id,

            "tdMode":
                Config.margin_mode,

            "side":
                side,

            "ordType":
                "market",

            "sz":
                format(
                    contracts,
                    ".12f",
                ).rstrip(
                    "0"
                ).rstrip(
                    "."
                ),

            "posSide":
                "net",

            "reduceOnly":
                True,

            "clOrdId":
                (
                    "CLS"
                    +
                    str(
                        int(
                            time.time()
                            * 1000
                        )
                    )
                )[-32:],
        }

        data = self.request(

            "/api/v5/trade/order",

            method="POST",

            body=body,

            private=True,
        )

        if not data:

            raise RuntimeError(
                "Empty close response"
            )

        if data[0].get(
            "sCode"
        ) not in (
            None,
            "",
            "0",
        ):

            raise RuntimeError(
                "Close rejected: "
                +
                str(
                    data[0].get(
                        "sMsg"
                    )
                )
            )

        return data[0]


# ============================================================
# STATE
# ============================================================

def default_state():

    return {

        "in_position":
            False,

        "direction":
            "none",

        "quantity":
            0.0,

        "entry_price":
            None,

        "stop_price":
            None,

        "target_price":
            None,

        "last_candle":
            None,

        "last_order_id":
            None,
    }


def load_state():

    path = Path(
        Config.state_file
    )

    if not path.exists():

        return default_state()

    try:

        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

        return {
            **default_state(),
            **data,
        }

    except Exception as error:

        raise RuntimeError(
            f"Invalid state file: {path}"
        ) from error


def save_state(state):

    path = Path(
        Config.state_file
    )

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp = path.with_suffix(
        ".tmp"
    )

    temp.write_text(
        json.dumps(
            state,
            indent=2,
        ),
        encoding="utf-8",
    )

    temp.replace(path)


# ============================================================
# TRADING BOT
# ============================================================

class TradingBot:

    def __init__(self):

        self.client = OkxClient()

        self.state = load_state()

        self.instrument = None

        self.stop_event = (
            threading.Event()
        )

        self.busy = False

        self.status = {

            "running":
                False,

            "okx_connected":
                False,

            "private_connected":
                False,

            "last_run":
                None,

            "last_signal":
                "none",

            "last_error":
                None,

            "symbol":
                Config.inst_id,

            "trend":
                Config.trend_bar,

            "entry":
                Config.entry_bar,

            "margin":
                Config.margin_usdt,

            "leverage":
                Config.leverage,

            "max_position":
                Config.max_position_usdt,

            "dry_run":
                Config.dry_run,

            "demo":
                Config.okx_demo,
        }

    # ========================================================
    # STARTUP CONNECTION
    # ========================================================

    def startup_check(self):

        log_event(
            "startup_check",
            symbol=
                Config.inst_id,
            demo=
                Config.okx_demo,
            dry_run=
                Config.dry_run,
        )

        connection = (
            self.client
            .test_connection()
        )

        self.status[
            "okx_connected"
        ] = connection[
            "connected"
        ]

        self.status[
            "private_connected"
        ] = connection[
            "private"
        ]

        self.instrument = (
            self.client.instrument()
        )

        log_event(

            "instrument_ready",

            symbol=
                Config.inst_id,

            contract_value=
                self.instrument[
                    "ctVal"
                ],

            lot_size=
                self.instrument[
                    "lotSz"
                ],

            minimum_size=
                self.instrument[
                    "minSz"
                ],

            tick_size=
                self.instrument[
                    "tickSz"
                ],
        )

        if not Config.dry_run:

            account = (
                self.client
                .account_config()
            )

            pos_mode = account.get(
                "posMode",
                "unknown",
            )

            log_event(
                "account_config",
                pos_mode=pos_mode,
            )

            # This bot uses net position mode.
            if pos_mode != "net_mode":

                raise RuntimeError(
                    "This bot requires OKX "
                    "Position Mode = Net Mode. "
                    f"Current mode: {pos_mode}"
                )

            self.client.set_leverage()

            log_event(
                "leverage_ready",
                leverage=
                    Config.leverage,
                margin_mode=
                    Config.margin_mode,
            )

        log_event(
            "OKX_CONNECTION_SUCCESS",
            demo=
                Config.okx_demo,
            dry_run=
                Config.dry_run,
        )

    # ========================================================
    # CONTRACT CALCULATION
    # ========================================================

    def calculate_contracts(
        self,
        price: float,
    ) -> float:

        ct_val = self.instrument[
            "ctVal"
        ]

        lot_size = self.instrument[
            "lotSz"
        ]

        min_size = self.instrument[
            "minSz"
        ]

        notional = min(

            Config.margin_usdt *
            Config.leverage,

            Config.max_position_usdt,
        )

        raw = (
            notional /
            (
                ct_val *
                price
            )
        )

        contracts = floor_to_step(
            raw,
            lot_size,
        )

        contracts = clean_number(
            contracts
        )

        if contracts < min_size:

            raise RuntimeError(

                f"Position size {contracts} "
                f"contracts is below OKX "
                f"minimum {min_size}"
            )

        return contracts

    # ========================================================
    # OPEN
    # ========================================================

    def open_position(
        self,
        signal_data,
    ):

        direction = signal_data[
            "direction"
        ]

        price = signal_data[
            "candle"
        ]["close"]

        atr_value = signal_data[
            "atr"
        ]

        stop_distance = (
            atr_value *
            Config.stop_atr_multiplier
        )

        if direction == "long":

            stop_price = (
                price -
                stop_distance
            )

            target_price = (
                price +
                stop_distance *
                Config.reward_to_risk
            )

            side = "buy"

        elif direction == "short":

            stop_price = (
                price +
                stop_distance
            )

            target_price = (
                price -
                stop_distance *
                Config.reward_to_risk
            )

            side = "sell"

        else:

            return

        contracts = (
            self.calculate_contracts(
                price
            )
        )

        notional = (
            contracts *
            self.instrument[
                "ctVal"
            ] *
            price
        )

        actual_margin = (
            notional /
            Config.leverage
        )

        # ----------------------------------------------------
        # DRY RUN
        # ----------------------------------------------------

        if Config.dry_run:

            order_id = "paper"

            log_event(

                "PAPER_ENTRY",

                direction=
                    direction,

                contracts=
                    contracts,

                notional=
                    clean_number(
                        notional
                    ),

                margin=
                    clean_number(
                        actual_margin
                    ),

                leverage=
                    Config.leverage,

                entry=
                    clean_number(
                        price
                    ),

                stop=
                    clean_number(
                        stop_price
                    ),

                target=
                    clean_number(
                        target_price
                    ),
            )

        # ----------------------------------------------------
        # OKX DEMO
        # ----------------------------------------------------

        else:

            order = (
                self.client
                .open_order(
                    side,
                    contracts,
                    stop_price,
                    target_price,
                )
            )

            order_id = order[
                "ordId"
            ]

            log_event(

                "DEMO_ORDER_PLACED",

                direction=
                    direction,

                order_id=
                    order_id,

                contracts=
                    contracts,

                notional=
                    clean_number(
                        notional
                    ),

                margin=
                    clean_number(
                        actual_margin
                    ),

                entry=
                    price,

                stop=
                    stop_price,

                target=
                    target_price,
            )

        self.state.update({

            "in_position":
                True,

            "direction":
                direction,

            "quantity":
                contracts,

            "entry_price":
                price,

            "stop_price":
                stop_price,

            "target_price":
                target_price,

            "last_order_id":
                order_id,
        })

        save_state(
            self.state
        )

    # ========================================================
    # CLOSE
    # ========================================================

    def close_position(
        self,
        reason: str,
    ):

        if not self.state[
            "in_position"
        ]:

            return

        direction = self.state[
            "direction"
        ]

        quantity = self.state[
            "quantity"
        ]

        if Config.dry_run:

            log_event(

                "PAPER_EXIT",

                reason=
                    reason,

                direction=
                    direction,

                contracts=
                    quantity,
            )

        else:

            # Long -> SELL
            # Short -> BUY

            side = (
                "sell"
                if direction ==
                "long"
                else
                "buy"
            )

            order = (
                self.client
                .close_order(
                    side,
                    quantity,
                )
            )

            log_event(

                "DEMO_EXIT",

                reason=
                    reason,

                direction=
                    direction,

                contracts=
                    quantity,

                order_id=
                    order.get(
                        "ordId"
                    ),
            )

        self.state = (
            default_state()
        )

        save_state(
            self.state
        )

    # ========================================================
    # RECONCILE
    # ========================================================

    def reconcile(self):

        if Config.dry_run:
            return

        exchange_position = (
            self.client.position()
        )

        if exchange_position is None:

            if self.state[
                "in_position"
            ]:

                log_event(
                    "exchange_position_flat"
                )

                self.state = (
                    default_state()
                )

                save_state(
                    self.state
                )

            return

        try:

            pos = float(
                exchange_position.get(
                    "pos",
                    "0",
                )
                or 0
            )

        except Exception:

            pos = 0.0

        if abs(pos) <= 0:
            return

        avg_price = float(
            exchange_position.get(
                "avgPx",
                "0",
            )
            or 0
        )

        direction = (
            "long"
            if pos > 0
            else
            "short"
        )

        self.state[
            "in_position"
        ] = True

        self.state[
            "direction"
        ] = direction

        self.state[
            "quantity"
        ] = abs(pos)

        if avg_price > 0:

            self.state[
                "entry_price"
            ] = avg_price

        save_state(
            self.state
        )

    # ========================================================
    # ONE LOOP
    # ========================================================

    def run_once(self):

        if self.busy:
            return

        self.busy = True

        self.status[
            "last_run"
        ] = now_iso()

        try:

            self.reconcile()

            trend_candles = (
                self.client.candles(
                    Config.trend_bar
                )
            )

            entry_candles = (
                self.client.candles(
                    Config.entry_bar
                )
            )

            signal_data = (
                calculate_signal(
                    trend_candles,
                    entry_candles,
                )
            )

            candle_timestamp = (
                signal_data[
                    "timestamp"
                ]
            )

            # Only process once per completed 15M candle
            if (
                self.state[
                    "last_candle"
                ]
                ==
                candle_timestamp
            ):

                return

            direction = (
                signal_data[
                    "direction"
                ]
            )

            self.status[
                "last_signal"
            ] = direction

            log_event(

                "SIGNAL",

                direction=
                    direction,

                close=
                    signal_data[
                        "candle"
                    ]["close"],

                trend_ema20=
                    clean_number(
                        signal_data[
                            "trend_ema20"
                        ]
                    ),

                trend_ema50=
                    clean_number(
                        signal_data[
                            "trend_ema50"
                        ]
                    ),

                ema20=
                    clean_number(
                        signal_data[
                            "ema20"
                        ]
                    ),

                ema50=
                    clean_number(
                        signal_data[
                            "ema50"
                        ]
                    ),

                rsi=
                    clean_number(
                        signal_data[
                            "rsi"
                        ]
                    ),

                atr=
                    clean_number(
                        signal_data[
                            "atr"
                        ]
                    ),

                support=
                    clean_number(
                        signal_data[
                            "support"
                        ]
                    ),

                resistance=
                    clean_number(
                        signal_data[
                            "resistance"
                        ]
                    ),

                fake_breakout=
                    signal_data[
                        "fake_breakout"
                    ],

                fake_breakdown=
                    signal_data[
                        "fake_breakdown"
                    ],
            )

            # =================================================
            # EXISTING POSITION
            # =================================================

            if self.state[
                "in_position"
            ]:

                current_direction = (
                    self.state[
                        "direction"
                    ]
                )

                if (
                    current_direction ==
                    "long"
                    and
                    signal_data[
                        "exit_long"
                    ]
                ):

                    self.close_position(
                        "trend_exit"
                    )

                elif (
                    current_direction ==
                    "short"
                    and
                    signal_data[
                        "exit_short"
                    ]
                ):

                    self.close_position(
                        "trend_exit"
                    )

                else:

                    log_event(
                        "POSITION_HOLD",
                        direction=
                            current_direction,
                        quantity=
                            self.state[
                                "quantity"
                            ],
                        entry=
                            self.state[
                                "entry_price"
                            ],
                        stop=
                            self.state[
                                "stop_price"
                            ],
                        target=
                            self.state[
                                "target_price"
                            ],
                    )

            # =================================================
            # NO POSITION
            # =================================================

            else:

                if direction in {
                    "long",
                    "short",
                }:

                    self.open_position(
                        signal_data
                    )

                else:

                    log_event(
                        "NO_ENTRY"
                    )

            self.state[
                "last_candle"
            ] = candle_timestamp

            save_state(
                self.state
            )

            self.status[
                "last_error"
            ] = None

        except Exception as error:

            self.status[
                "last_error"
            ] = str(error)

            log_event(
                "ERROR",
                error=str(error),
            )

        finally:

            self.busy = False

    # ========================================================
    # STATUS
    # ========================================================

    def status_payload(self):

        return {

            **self.status,

            "position": {

                "in_position":
                    self.state[
                        "in_position"
                    ],

                "direction":
                    self.state[
                        "direction"
                    ],

                "quantity":
                    self.state[
                        "quantity"
                    ],

                "entry_price":
                    self.state[
                        "entry_price"
                    ],

                "stop_price":
                    self.state[
                        "stop_price"
                    ],

                "target_price":
                    self.state[
                        "target_price"
                    ],
            },
        }

    # ========================================================
    # RUN
    # ========================================================

    def run(self):

        self.startup_check()

        self.status[
            "running"
        ] = True

        log_event(

            "BOT_RUNNING",

            symbol=
                Config.inst_id,

            strategy=
                "1H + 15M Breakout Retest",

            margin=
                Config.margin_usdt,

            leverage=
                Config.leverage,

            max_position=
                Config.max_position_usdt,

            demo=
                Config.okx_demo,

            dry_run=
                Config.dry_run,

            railway_port=
                Config.port,
        )

        while not self.stop_event.is_set():

            self.run_once()

            self.stop_event.wait(
                Config.poll_seconds
            )


# ============================================================
# RAILWAY HEALTH SERVER
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    bot = None

    def do_GET(self):

        if self.path not in {
            "/",
            "/health",
        }:

            self.send_response(
                404
            )

            self.end_headers()

            return

        payload = {

            "ok":
                True,

            **self.bot.status_payload(),
        }

        response = json.dumps(
            payload
        ).encode()

        self.send_response(
            200
        )

        self.send_header(
            "Content-Type",
            "application/json",
        )

        self.send_header(
            "Content-Length",
            str(
                len(response)
            ),
        )

        self.end_headers()

        self.wfile.write(
            response
        )

    def log_message(
        self,
        format,
        *args,
    ):

        return


def start_health_server(
    bot,
):

    HealthHandler.bot = bot

    server = ThreadingHTTPServer(

        (
            "0.0.0.0",
            Config.port,
        ),

        HealthHandler,
    )

    thread = threading.Thread(

        target=
            server.serve_forever,

        daemon=True,
    )

    thread.start()

    log_event(

        "RAILWAY_HEALTH_SERVER",

        host=
            "0.0.0.0",

        port=
            Config.port,
    )

    return server


# ============================================================
# MAIN
# ============================================================

def main():

    validate_config()

    bot = TradingBot()

    server = (
        start_health_server(
            bot
        )
    )

    def shutdown(
        _signum,
        _frame,
    ):

        log_event(
            "SHUTDOWN"
        )

        bot.stop_event.set()

        server.shutdown()

    signal.signal(
        signal.SIGTERM,
        shutdown,
    )

    signal.signal(
        signal.SIGINT,
        shutdown,
    )

    try:

        bot.run()

    except Exception as error:

        log_event(
            "FATAL_ERROR",
            error=str(error),
        )

        raise

    finally:

        server.server_close()


if __name__ == "__main__":

    main()
