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
# OKX / RAILWAY
# ============================================================

BASE_URL = "https://www.okx.com"


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

    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be a number"
        ) from exc


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)

    if value is None or value == "":
        return default

    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be an integer"
        ) from exc


class Config:

    # ========================================================
    # OKX
    # ========================================================

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

    # ========================================================
    # RAILWAY
    # ========================================================

    # Railway automatically provides PORT.
    # 8080 is fallback for local testing.
    port = env_int(
        "PORT",
        8080,
    )

    # ========================================================
    # TIMEFRAMES
    # ========================================================

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

    # ========================================================
    # MONEY MANAGEMENT
    # ========================================================

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

    # ========================================================
    # STRATEGY
    # ========================================================

    ema_fast = 20
    ema_slow = 50

    rsi_period = 14

    breakout_period = 20

    atr_period = 14

    stop_atr_multiplier = env_float(
        "STOP_ATR_MULTIPLIER",
        1.5,
    )

    reward_to_risk = env_float(
        "REWARD_TO_RISK",
        2.0,
    )

    # Retest tolerance = 0.2%
    retest_tolerance = env_float(
        "RETEST_TOLERANCE",
        0.002,
    )

    # Long RSI
    long_rsi_min = env_float(
        "LONG_RSI_MIN",
        50,
    )

    long_rsi_max = env_float(
        "LONG_RSI_MAX",
        70,
    )

    # Short RSI
    short_rsi_min = env_float(
        "SHORT_RSI_MIN",
        30,
    )

    short_rsi_max = env_float(
        "SHORT_RSI_MAX",
        50,
    )

    # ========================================================
    # STATE
    # ========================================================

    state_file = os.getenv(
        "STATE_FILE",
        "./data/state.json",
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

    calculated_position = (
        Config.margin_usdt *
        Config.leverage
    )

    if calculated_position > Config.max_position_usdt:
        raise ValueError(
            "MARGIN_USDT x LEVERAGE exceeds "
            "MAX_POSITION_USDT"
        )

    if Config.margin_mode not in {
        "isolated",
        "cross",
    }:
        raise ValueError(
            "MARGIN_MODE must be isolated or cross"
        )

    if Config.stop_atr_multiplier <= 0:
        raise ValueError(
            "STOP_ATR_MULTIPLIER must be positive"
        )

    if Config.reward_to_risk <= 0:
        raise ValueError(
            "REWARD_TO_RISK must be positive"
        )

    if not Config.dry_run:

        required = (
            "OKX_API_KEY",
            "OKX_SECRET_KEY",
            "OKX_PASSPHRASE",
        )

        for name in required:

            if not os.getenv(name):

                raise ValueError(
                    f"{name} is required when "
                    "DRY_RUN=false"
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
# LOGGING
# ============================================================

def now_iso() -> str:

    return (
        datetime.now(
            timezone.utc
        )
        .isoformat(
            timespec="milliseconds"
        )
        .replace(
            "+00:00",
            "Z",
        )
    )


def log_event(
    message: str,
    **fields: Any,
) -> None:

    payload = {
        "time": now_iso(),
        "message": message,
        **fields,
    }

    print(
        json.dumps(
            payload,
            separators=(",", ":"),
        ),
        flush=True,
    )


def clean_number(
    value: float,
) -> float:

    return float(
        f"{value:.12f}"
    )


def floor_to_step(
    value: float,
    step: float,
) -> float:

    if step <= 0:
        return value

    a = Decimal(
        str(value)
    )

    b = Decimal(
        str(step)
    )

    return float(
        (a // b) * b
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

    result = [
        values[0]
    ]

    for value in values[1:]:

        result.append(
            (
                value -
                result[-1]
            )
            * multiplier
            + result[-1]
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

    avg_gain = (
        gains /
        period
    )

    avg_loss = (
        losses /
        period
    )

    if avg_loss == 0:

        result[period] = 100.0

    else:

        rs = (
            avg_gain /
            avg_loss
        )

        result[period] = (
            100 -
            (
                100 /
                (1 + rs)
            )
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
            )
            + gain
        ) / period

        avg_loss = (
            (
                avg_loss *
                (period - 1)
            )
            + loss
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
                (
                    100 /
                    (1 + rs)
                )
            )

    return result


def atr(
    candles: list[
        dict[str, float]
    ],
    period: int = 14,
) -> list[float | None]:

    tr = []

    for i, candle in enumerate(
        candles
    ):

        if i == 0:

            tr.append(
                candle["high"] -
                candle["low"]
            )

        else:

            previous_close = candles[
                i - 1
            ]["close"]

            tr.append(
                max(
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
            )

    result = [
        None
    ] * len(candles)

    if len(candles) <= period:
        return result

    average = (
        sum(
            tr[
                1:
                period + 1
            ]
        )
        / period
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
            + tr[i]
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
                    x["high"]
                    for x in candles[
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
                    x["low"]
                    for x in candles[
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
    trend_candles: list[
        dict[str, float]
    ],
    entry_candles: list[
        dict[str, float]
    ],
) -> dict[str, Any]:

    if len(trend_candles) < 60:
        raise ValueError(
            "Not enough 1H candles"
        )

    if len(entry_candles) < 60:
        raise ValueError(
            "Not enough 15m candles"
        )

    # --------------------------------------------------------
    # 1H TREND
    # --------------------------------------------------------

    trend_closes = [
        x["close"]
        for x in trend_candles
    ]

    trend_ema20 = ema(
        trend_closes,
        Config.ema_fast,
    )

    trend_ema50 = ema(
        trend_closes,
        Config.ema_slow,
    )

    ti = (
        len(trend_candles)
        - 1
    )

    trend_price = (
        trend_candles[
            ti
        ]["close"]
    )

    trend_up = (
        trend_ema20[ti]
        >
        trend_ema50[ti]
        and
        trend_price
        >
        trend_ema50[ti]
    )

    trend_down = (
        trend_ema20[ti]
        <
        trend_ema50[ti]
        and
        trend_price
        <
        trend_ema50[ti]
    )

    # --------------------------------------------------------
    # 15M
    # --------------------------------------------------------

    closes = [
        x["close"]
        for x in entry_candles
    ]

    ema20_values = ema(
        closes,
        Config.ema_fast,
    )

    ema50_values = ema(
        closes,
        Config.ema_slow,
    )

    rsi_values = rsi(
        closes,
        Config.rsi_period,
    )

    atr_values = atr(
        entry_candles,
        Config.atr_period,
    )

    highs = rolling_high(
        entry_candles,
        Config.breakout_period,
    )

    lows = rolling_low(
        entry_candles,
        Config.breakout_period,
    )

    i = (
        len(entry_candles)
        - 1
    )

    previous_i = i - 1

    current = entry_candles[i]

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
            "Resistance not ready"
        )

    if support is None:
        raise ValueError(
            "Support not ready"
        )

    if rsi_values[i] is None:
        raise ValueError(
            "RSI not ready"
        )

    if atr_values[i] is None:
        raise ValueError(
            "ATR not ready"
        )

    current_rsi = float(
        rsi_values[i]
    )

    current_atr = float(
        atr_values[i]
    )

    # --------------------------------------------------------
    # BREAKOUT
    # --------------------------------------------------------

    bullish_breakout = (
        previous["close"]
        >
        resistance
    )

    bearish_breakdown = (
        previous["close"]
        <
        support
    )

    # --------------------------------------------------------
    # RETEST
    # --------------------------------------------------------

    long_retest = (
        current["low"]
        <=
        resistance *
        (
            1 +
            Config.retest_tolerance
        )
        and
        current["close"]
        >
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
        current["close"]
        <
        support
    )

    # --------------------------------------------------------
    # FAKE BREAKOUT
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # EMA
    # --------------------------------------------------------

    ema_long = (
        ema20_values[i]
        >
        ema50_values[i]
        and
        current["close"]
        >
        ema20_values[i]
    )

    ema_short = (
        ema20_values[i]
        <
        ema50_values[i]
        and
        current["close"]
        <
        ema20_values[i]
    )

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    long_rsi = (
        Config.long_rsi_min
        <=
        current_rsi
        <=
        Config.long_rsi_max
    )

    short_rsi = (
        Config.short_rsi_min
        <=
        current_rsi
        <=
        Config.short_rsi_max
    )

    # --------------------------------------------------------
    # FINAL
    # --------------------------------------------------------

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

    exit_long = (
        current["close"]
        <
        ema20_values[i]
        or
        current_rsi < 45
        or
        ema20_values[i]
        <
        ema50_values[i]
    )

    exit_short = (
        current["close"]
        >
        ema20_values[i]
        or
        current_rsi > 55
        or
        ema20_values[i]
        >
        ema50_values[i]
    )

    return {

        "timestamp":
            current["timestamp"],

        "candle":
            current,

        "direction":
            direction,

        "long_signal":
            long_signal,

        "short_signal":
            short_signal,

        "exit_long":
            exit_long,

        "exit_short":
            exit_short,

        "fake_breakout":
            fake_breakout,

        "fake_breakdown":
            fake_breakdown,

        "resistance":
            resistance,

        "support":
            support,

        "ema20":
            ema20_values[i],

        "ema50":
            ema50_values[i],

        "trend_ema20":
            trend_ema20[ti],

        "trend_ema50":
            trend_ema50[ti],

        "rsi":
            current_rsi,

        "atr":
            current_atr,
    }


# ============================================================
# OKX API CLIENT
# ============================================================

class OKXClient:

    def __init__(self):

        self.last_request_error = None

    # --------------------------------------------------------
    # REQUEST
    # --------------------------------------------------------

    def request(
        self,
        path: str,
        method: str = "GET",
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        private: bool = False,
    ) -> list[dict[str, Any]]:

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
            path
            +
            (
                "?"
                +
                query_string
                if query_string
                else ""
            )
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

        # ----------------------------------------------------
        # PRIVATE API AUTH
        # ----------------------------------------------------

        if private:

            api_key = os.getenv(
                "OKX_API_KEY"
            )

            secret_key = os.getenv(
                "OKX_SECRET_KEY"
            )

            passphrase = os.getenv(
                "OKX_PASSPHRASE"
            )

            if not api_key:
                raise RuntimeError(
                    "OKX_API_KEY missing"
                )

            if not secret_key:
                raise RuntimeError(
                    "OKX_SECRET_KEY missing"
                )

            if not passphrase:
                raise RuntimeError(
                    "OKX_PASSPHRASE missing"
                )

            timestamp = now_iso()

            prehash = (
                timestamp
                +
                method
                +
                request_path
                +
                payload
            )

            digest = hmac.new(
                secret_key.encode(),
                prehash.encode(),
                hashlib.sha256,
            ).digest()

            signature = (
                base64.b64encode(
                    digest
                )
                .decode()
            )

            headers.update(
                {
                    "OK-ACCESS-KEY":
                        api_key,

                    "OK-ACCESS-SIGN":
                        signature,

                    "OK-ACCESS-TIMESTAMP":
                        timestamp,

                    "OK-ACCESS-PASSPHRASE":
                        passphrase,
                }
            )

            # OKX Demo Trading
            if Config.okx_demo:

                headers[
                    "x-simulated-trading"
                ] = "1"

        request = Request(

            BASE_URL
            +
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
                timeout=20,
            ) as response:

                result = json.loads(
                    response
                    .read()
                    .decode()
                )

        except HTTPError as exc:

            detail = (
                exc.read()
                .decode(
                    errors="replace"
                )
            )

            raise RuntimeError(
                f"OKX HTTP {exc.code}: "
                f"{detail[:500]}"
            ) from exc

        except (
            URLError,
            TimeoutError,
        ) as exc:

            raise RuntimeError(
                f"OKX network error: "
                f"{exc}"
            ) from exc

        if result.get("code") != "0":

            raise RuntimeError(
                "OKX error: "
                +
                str(
                    result.get(
                        "msg",
                        "unknown error",
                    )
                )
            )

        return result.get(
            "data",
            [],
        )

    # --------------------------------------------------------
    # PUBLIC CONNECTION TEST
    # --------------------------------------------------------

    def test_public_connection(
        self,
    ) -> bool:

        data = self.request(
            "/api/v5/public/time"
        )

        return bool(data)

    # --------------------------------------------------------
    # PRIVATE CONNECTION TEST
    # --------------------------------------------------------

    def test_private_connection(
        self,
    ) -> dict[str, Any]:

        data = self.request(
            "/api/v5/account/balance",
            private=True,
        )

        if not data:

            raise RuntimeError(
                "OKX account API returned no data"
            )

        return data[0]

    # --------------------------------------------------------
    # INSTRUMENT
    # --------------------------------------------------------

    def get_instrument(
        self,
    ) -> dict[str, Any]:

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
                f"Instrument not found: "
                f"{Config.inst_id}"
            )

        item = data[0]

        return {

            "inst_id":
                item["instId"],

            "ct_val":
                float(
                    item["ctVal"]
                ),

            "lot_sz":
                float(
                    item["lotSz"]
                ),

            "min_sz":
                float(
                    item["minSz"]
                ),

            "tick_sz":
                float(
                    item["tickSz"]
                ),

            "settle_ccy":
                item.get(
                    "settleCcy",
                    "USDT",
                ),
        }

    # --------------------------------------------------------
    # CANDLES
    # --------------------------------------------------------

    def get_candles(
        self,
        bar: str,
    ) -> list[
        dict[str, float]
    ]:

        rows = self.request(

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

        for row in rows:

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
                    value
                )
                for value
                in candle.values()
            ):

                candles.append(
                    candle
                )

        candles.sort(
            key=lambda x:
                x["timestamp"]
        )

        return candles

    # --------------------------------------------------------
    # LEVERAGE
    # --------------------------------------------------------

    def set_leverage(
        self,
    ) -> None:

        self.request(

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

    # --------------------------------------------------------
    # CURRENT POSITION
    # --------------------------------------------------------

    def get_position(
        self,
    ) -> dict[str, Any] | None:

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

        for item in data:

            try:

                pos = float(
                    item.get(
                        "pos",
                        "0",
                    )
                    or 0
                )

            except (
                ValueError,
                TypeError,
            ):

                pos = 0.0

            if abs(pos) > 0:

                return item

        return None

    # --------------------------------------------------------
    # MARKET ORDER
    # --------------------------------------------------------

    def market_order(
        self,
        side: str,
        contracts: float,
    ) -> dict[str, Any]:

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
                )
                .rstrip("0")
                .rstrip("."),

            "posSide":
                "net",

            "clOrdId":
                (
                    "bot"
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
                "Empty OKX order response"
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

    # --------------------------------------------------------
    # ORDER STATUS
    # --------------------------------------------------------

    def get_order(
        self,
        order_id: str,
    ) -> dict[str, Any] | None:

        data = self.request(

            "/api/v5/trade/order",

            query={

                "instId":
                    Config.inst_id,

                "ordId":
                    order_id,
            },

            private=True,
        )

        return (
            data[0]
            if data
            else None
        )

    # --------------------------------------------------------
    # CLOSE NET POSITION
    # --------------------------------------------------------

    def close_position(
        self,
        direction: str,
        contracts: float,
    ) -> dict[str, Any]:

        side = (
            "sell"
            if direction == "long"
            else "buy"
        )

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
                )
                .rstrip("0")
                .rstrip("."),

            "posSide":
                "net",

            "reduceOnly":
                True,

            "clOrdId":
                (
                    "close"
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

def default_state() -> dict[str, Any]:

    return {

        "symbol":
            Config.inst_id,

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

        "last_candle_timestamp":
            None,

        "last_order_id":
            None,
    }


def load_state() -> dict[str, Any]:

    path = Path(
        Config.state_file
    )

    default = default_state()

    if not path.exists():

        return default

    try:

        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

        return {
            **default,
            **data,
        }

    except Exception as exc:

        raise RuntimeError(
            f"Invalid state file: "
            f"{path}"
        ) from exc


def save_state(
    state: dict[str, Any],
) -> None:

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
        )
        +
        "\n",
        encoding="utf-8",
    )

    temp.replace(
        path
    )


# ============================================================
# BOT
# ============================================================

class TradingBot:

    def __init__(self):

        self.client = OKXClient()

        self.state = load_state()

        self.instrument = None

        self.stop_event = (
            threading.Event()
        )

        self.busy = False

        self.status = {

            "running":
                False,

            "okx_public":
                False,

            "okx_private":
                False,

            "demo":
                Config.okx_demo,

            "dry_run":
                Config.dry_run,

            "symbol":
                Config.inst_id,

            "trend_bar":
                Config.trend_bar,

            "entry_bar":
                Config.entry_bar,

            "margin":
                Config.margin_usdt,

            "leverage":
                Config.leverage,

            "max_position":
                Config.max_position_usdt,

            "last_run":
                None,

            "last_signal":
                "none",

            "last_error":
                None,
        }

    # --------------------------------------------------------
    # CONNECTION TEST
    # --------------------------------------------------------

    def startup_connection_test(
        self,
    ) -> None:

        log_event(
            "railway_server",
            host="0.0.0.0",
            port=Config.port,
        )

        # Public OKX API
        self.client.test_public_connection()

        self.status[
            "okx_public"
        ] = True

        log_event(
            "OKX_PUBLIC_CONNECTION_SUCCESS"
        )

        # Instrument
        self.instrument = (
            self.client.get_instrument()
        )

        log_event(

            "OKX_INSTRUMENT_READY",

            symbol=
                Config.inst_id,

            contract_value=
                self.instrument[
                    "ct_val"
                ],

            lot_size=
                self.instrument[
                    "lot_sz"
                ],

            min_size=
                self.instrument[
                    "min_sz"
                ],
        )

        # Private API
        if Config.dry_run:

            log_event(
                "DRY_RUN=true",
                message2=(
                    "Private order connection "
                    "test skipped"
                ),
            )

        else:

            self.client.test_private_connection()

            self.status[
                "okx_private"
            ] = True

            log_event(
                "OKX_PRIVATE_CONNECTION_SUCCESS"
            )

            self.client.set_leverage()

            log_event(

                "LEVERAGE_READY",

                leverage=
                    Config.leverage,

                margin_mode=
                    Config.margin_mode,
            )

        log_event(

            "BOT_CONFIGURATION_READY",

            symbol=
                Config.inst_id,

            margin=
                Config.margin_usdt,

            leverage=
                Config.leverage,

            max_position=
                Config.max_position_usdt,

            trend=
                Config.trend_bar,

            entry=
                Config.entry_bar,
        )

    # --------------------------------------------------------
    # CONTRACT SIZE
    # --------------------------------------------------------

    def calculate_contracts(
        self,
        price: float,
    ) -> float:

        if not self.instrument:

            raise RuntimeError(
                "Instrument not loaded"
            )

        ct_val = self.instrument[
            "ct_val"
        ]

        lot_size = self.instrument[
            "lot_sz"
        ]

        min_size = self.instrument[
            "min_sz"
        ]

        max_notional = min(

            Config.margin_usdt
            *
            Config.leverage,

            Config.max_position_usdt,
        )

        contracts = (
            max_notional
            /
            (
                ct_val
                *
                price
            )
        )

        contracts = floor_to_step(
            contracts,
            lot_size,
        )

        contracts = clean_number(
            contracts
        )

        if contracts < min_size:

            raise RuntimeError(

                f"Calculated contracts "
                f"{contracts} below "
                f"OKX minimum "
                f"{min_size}"
            )

        return contracts

    # --------------------------------------------------------
    # OPEN POSITION
    # --------------------------------------------------------

    def open_position(
        self,
        signal_data: dict[str, Any],
    ) -> None:

        direction = signal_data[
            "direction"
        ]

        entry_price = signal_data[
            "candle"
        ]["close"]

        atr_value = signal_data[
            "atr"
        ]

        stop_distance = (
            atr_value
            *
            Config.stop_atr_multiplier
        )

        if direction == "long":

            side = "buy"

            stop_price = (
                entry_price
                -
                stop_distance
            )

            target_price = (
                entry_price
                +
                stop_distance
                *
                Config.reward_to_risk
            )

        elif direction == "short":

            side = "sell"

            stop_price = (
                entry_price
                +
                stop_distance
            )

            target_price = (
                entry_price
                -
                stop_distance
                *
                Config.reward_to_risk
            )

        else:

            return

        contracts = (
            self.calculate_contracts(
                entry_price
            )
        )

        notional = (
            contracts
            *
            self.instrument[
                "ct_val"
            ]
            *
            entry_price
        )

        actual_margin = (
            notional
            /
            Config.leverage
        )

        # ----------------------------------------------------
        # PAPER MODE
        # ----------------------------------------------------

        if Config.dry_run:

            fill_price = (
                entry_price
            )

            order_id = (
                "paper-entry"
            )

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
                        fill_price
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
                self.client.market_order(
                    side,
                    contracts,
                )
            )

            order_id = order[
                "ordId"
            ]

            fill_price = (
                entry_price
            )

            time.sleep(1)

            fill = (
                self.client.get_order(
                    order_id
                )
            )

            if fill:

                try:

                    fill_price = float(
                        fill.get(
                            "avgPx"
                        )
                        or
                        entry_price
                    )

                except (
                    ValueError,
                    TypeError,
                ):

                    fill_price = (
                        entry_price
                    )

            # Recalculate based on fill
            if direction == "long":

                stop_price = (
                    fill_price
                    -
                    stop_distance
                )

                target_price = (
                    fill_price
                    +
                    stop_distance
                    *
                    Config.reward_to_risk
                )

            else:

                stop_price = (
                    fill_price
                    +
                    stop_distance
                )

                target_price = (
                    fill_price
                    -
                    stop_distance
                    *
                    Config.reward_to_risk
                )

            log_event(

                "OKX_DEMO_ENTRY",

                direction=
                    direction,

                order_id=
                    order_id,

                contracts=
                    contracts,

                entry=
                    fill_price,

                stop=
                    stop_price,

                target=
                    target_price,
            )

            # Note:
            # SL/TP is managed by the bot logic below.
            # This keeps the implementation simple and
            # avoids relying on an unsupported conditional
            # order combination.

        self.state.update(

            {

                "in_position":
                    True,

                "direction":
                    direction,

                "quantity":
                    contracts,

                "entry_price":
                    fill_price,

                "stop_price":
                    stop_price,

                "target_price":
                    target_price,

                "last_order_id":
                    order_id,
            }
        )

        save_state(
            self.state
        )

    # --------------------------------------------------------
    # CLOSE POSITION
    # --------------------------------------------------------

    def close_position(
        self,
        reason: str,
        price: float | None = None,
    ) -> None:

        if not self.state[
            "in_position"
        ]:

            return

        direction = self.state[
            "direction"
        ]

        contracts = float(
            self.state[
                "quantity"
            ]
        )

        if Config.dry_run:

            log_event(

                "PAPER_EXIT",

                reason=
                    reason,

                direction=
                    direction,

                contracts=
                    contracts,

                price=
                    price,
            )

        else:

            order = (
                self.client.close_position(
                    direction,
                    contracts,
                )
            )

            log_event(

                "OKX_DEMO_EXIT",

                reason=
                    reason,

                direction=
                    direction,

                contracts=
                    contracts,

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

    # --------------------------------------------------------
    # POSITION RECONCILIATION
    # --------------------------------------------------------

    def reconcile_position(
        self,
    ) -> None:

        if Config.dry_run:
            return

        position = (
            self.client.get_position()
        )

        if position is None:

            if self.state[
                "in_position"
            ]:

                log_event(
                    "EXCHANGE_POSITION_FLAT"
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
                position.get(
                    "pos",
                    "0",
                )
                or 0
            )

        except (
            ValueError,
            TypeError,
        ):

            pos = 0

        if abs(pos) <= 0:
            return

        avg_px = float(
            position.get(
                "avgPx",
                "0",
            )
            or 0
        )

        pos_side = position.get(
            "posSide",
            "net",
        )

        if pos_side == "short":

            direction = "short"

        elif pos_side == "long":

            direction = "long"

        else:

            direction = (
                "long"
                if pos > 0
                else "short"
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

        if avg_px > 0:

            self.state[
                "entry_price"
            ] = avg_px

        save_state(
            self.state
        )

    # --------------------------------------------------------
    # ONE LOOP
    # --------------------------------------------------------

    def run_once(
        self,
    ) -> None:

        if self.busy:
            return

        self.busy = True

        self.status[
            "last_run"
        ] = now_iso()

        try:

            self.reconcile_position()

            trend_candles = (
                self.client.get_candles(
                    Config.trend_bar
                )
            )

            entry_candles = (
                self.client.get_candles(
                    Config.entry_bar
                )
            )

            signal_data = (
                calculate_signal(
                    trend_candles,
                    entry_candles,
                )
            )

            timestamp = (
                signal_data[
                    "timestamp"
                ]
            )

            # Do not process same candle twice
            if (
                self.state[
                    "last_candle_timestamp"
                ]
                ==
                timestamp
            ):

                return

            direction = signal_data[
                "direction"
            ]

            self.status[
                "last_signal"
            ] = direction

            log_event(

                "SIGNAL",

                direction=
                    direction,

                price=
                    signal_data[
                        "candle"
                    ]["close"],

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

                resistance=
                    clean_number(
                        signal_data[
                            "resistance"
                        ]
                    ),

                support=
                    clean_number(
                        signal_data[
                            "support"
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

            # ------------------------------------------------
            # EXISTING POSITION
            # ------------------------------------------------

            if self.state[
                "in_position"
            ]:

                current_direction = (
                    self.state[
                        "direction"
                    ]
                )

                candle = signal_data[
                    "candle"
                ]

                stop_price = float(
                    self.state[
                        "stop_price"
                    ]
                )

                target_price = float(
                    self.state[
                        "target_price"
                    ]
                )

                # LONG
                if (
                    current_direction
                    ==
                    "long"
                ):

                    stop_hit = (
                        candle["low"]
                        <=
                        stop_price
                    )

                    target_hit = (
                        candle["high"]
                        >=
                        target_price
                    )

                    trend_exit = (
                        signal_data[
                            "exit_long"
                        ]
                    )

                    if stop_hit:

                        self.close_position(
                            "stop_loss",
                            candle["close"],
                        )

                    elif target_hit:

                        self.close_position(
                            "take_profit",
                            candle["close"],
                        )

                    elif trend_exit:

                        self.close_position(
                            "trend_exit",
                            candle["close"],
                        )

                # SHORT
                elif (
                    current_direction
                    ==
                    "short"
                ):

                    stop_hit = (
                        candle["high"]
                        >=
                        stop_price
                    )

                    target_hit = (
                        candle["low"]
                        <=
                        target_price
                    )

                    trend_exit = (
                        signal_data[
                            "exit_short"
                        ]
                    )

                    if stop_hit:

                        self.close_position(
                            "stop_loss",
                            candle["close"],
                        )

                    elif target_hit:

                        self.close_position(
                            "take_profit",
                            candle["close"],
                        )

                    elif trend_exit:

                        self.close_position(
                            "trend_exit",
                            candle["close"],
                        )

            # ------------------------------------------------
            # NEW POSITION
            # ------------------------------------------------

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
                "last_candle_timestamp"
            ] = timestamp

            save_state(
                self.state
            )

            self.status[
                "last_error"
            ] = None

        except Exception as exc:

            self.status[
                "last_error"
            ] = str(exc)

            log_event(
                "TICK_FAILED",
                error=str(exc),
            )

        finally:

            self.busy = False

    # --------------------------------------------------------
    # RUN
    # --------------------------------------------------------

    def run(
        self,
    ) -> None:

        self.startup_connection_test()

        self.status[
            "running"
        ] = True

        log_event(
            "BOT_RUNNING"
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

    bot: TradingBot

    def do_GET(
        self,
    ) -> None:

        if self.path in {
            "/",
            "/health",
        }:

            response = json.dumps(
                {
                    "ok": True,
                    **self.bot.status,
                    "position":
                        self.bot.state,
                }
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

            return

        self.send_response(
            404
        )

        self.end_headers()

    def log_message(
        self,
        _format: str,
        *_args: Any,
    ) -> None:

        # Silence HTTP access logs
        return


def start_railway_server(
    bot: TradingBot,
) -> ThreadingHTTPServer:

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

        "RAILWAY_SERVER_STARTED",

        host=
            "0.0.0.0",

        port=
            Config.port,

        health=
            "/health",
    )

    return server


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    validate_config()

    bot = TradingBot()

    server = (
        start_railway_server(
            bot
        )
    )

    def shutdown(
        _signum: int,
        _frame: Any,
    ) -> None:

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

    except Exception as exc:

        log_event(
            "FATAL_ERROR",
            error=str(exc),
        )

        raise

    finally:

        server.server_close()


if __name__ == "__main__":

    main()
