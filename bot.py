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
# OKX
# ============================================================

BASE_URL = "https://www.okx.com"


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)

    if value is None or value == "":
        return default

    return value.strip().lower() in {
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

    # --------------------------------------------------------
    # OKX
    # --------------------------------------------------------

    inst_id = os.getenv(
        "OKX_INST_ID",
        "DOGE-USDT-SWAP",
    ).strip().upper()

    okx_demo = env_bool(
        "OKX_DEMO",
        True,
    )

    dry_run = env_bool(
        "DRY_RUN",
        True,
    )

    # --------------------------------------------------------
    # Railway
    # --------------------------------------------------------

    port = env_int(
        "PORT",
        8080,
    )

    # --------------------------------------------------------
    # Timeframes
    # --------------------------------------------------------

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
            200,
        ),
    )

    poll_seconds = max(
        15,
        env_int(
            "POLL_SECONDS",
            30,
        ),
    )

    # --------------------------------------------------------
    # Money management
    # --------------------------------------------------------

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
    ).strip().lower()

    # --------------------------------------------------------
    # Strategy
    # --------------------------------------------------------

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

    long_rsi_min = env_float(
        "LONG_RSI_MIN",
        50,
    )

    long_rsi_max = env_float(
        "LONG_RSI_MAX",
        70,
    )

    short_rsi_min = env_float(
        "SHORT_RSI_MIN",
        30,
    )

    short_rsi_max = env_float(
        "SHORT_RSI_MAX",
        50,
    )

    # --------------------------------------------------------
    # State
    # --------------------------------------------------------

    state_file = os.getenv(
        "STATE_FILE",
        "./data/state.json",
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
            default=str,
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
# VALIDATION
# ============================================================

def validate_config() -> None:

    if not Config.inst_id.endswith(
        "-USDT-SWAP"
    ):
        raise ValueError(
            "OKX_INST_ID must look like "
            "DOGE-USDT-SWAP"
        )

    if Config.margin_usdt <= 0:
        raise ValueError(
            "MARGIN_USDT must be greater than 0"
        )

    if Config.leverage <= 0:
        raise ValueError(
            "LEVERAGE must be greater than 0"
        )

    if Config.max_position_usdt <= 0:
        raise ValueError(
            "MAX_POSITION_USDT must be greater than 0"
        )

    position = (
        Config.margin_usdt
        * Config.leverage
    )

    if position > Config.max_position_usdt:
        raise ValueError(
            "MARGIN_USDT x LEVERAGE is greater "
            "than MAX_POSITION_USDT"
        )

    if Config.margin_mode not in {
        "isolated",
        "cross",
    }:
        raise ValueError(
            "MARGIN_MODE must be isolated or cross"
        )

    if not Config.dry_run:

        required = [
            "OKX_API_KEY",
            "OKX_SECRET_KEY",
            "OKX_PASSPHRASE",
        ]

        missing = [
            x
            for x in required
            if not os.getenv(x)
        ]

        if missing:
            raise ValueError(
                "Missing variables: "
                + ", ".join(missing)
            )

        if os.getenv(
            "LIVE_TRADING_CONFIRMATION"
        ) != "I_UNDERSTAND":

            raise ValueError(
                "Set LIVE_TRADING_CONFIRMATION="
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

    result = [
        values[0]
    ]

    for value in values[1:]:

        result.append(
            result[-1]
            +
            (
                value
                -
                result[-1]
            )
            * multiplier
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
            values[i]
            -
            values[i - 1]
        )

        if change > 0:
            gains += change
        else:
            losses -= change

    avg_gain = (
        gains / period
    )

    avg_loss = (
        losses / period
    )

    if avg_loss == 0:

        result[period] = 100.0

    else:

        rs = (
            avg_gain
            /
            avg_loss
        )

        result[period] = (
            100
            -
            (
                100
                /
                (1 + rs)
            )
        )

    for i in range(
        period + 1,
        len(values),
    ):

        change = (
            values[i]
            -
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
                avg_gain
                * (period - 1)
            )
            +
            gain
        ) / period

        avg_loss = (
            (
                avg_loss
                * (period - 1)
            )
            +
            loss
        ) / period

        if avg_loss == 0:

            result[i] = 100.0

        else:

            rs = (
                avg_gain
                /
                avg_loss
            )

            result[i] = (
                100
                -
                (
                    100
                    /
                    (1 + rs)
                )
            )

    return result


def atr(
    candles: list[dict[str, float]],
    period: int = 14,
) -> list[float | None]:

    if len(candles) <= period:
        return [
            None
        ] * len(candles)

    true_ranges = []

    for i, candle in enumerate(
        candles
    ):

        if i == 0:

            tr = (
                candle["high"]
                -
                candle["low"]
            )

        else:

            previous_close = candles[
                i - 1
            ]["close"]

            tr = max(
                candle["high"]
                -
                candle["low"],

                abs(
                    candle["high"]
                    -
                    previous_close
                ),

                abs(
                    candle["low"]
                    -
                    previous_close
                ),
            )

        true_ranges.append(
            tr
        )

    result = [
        None
    ] * len(candles)

    average = (
        sum(
            true_ranges[
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
                average
                * (period - 1)
            )
            +
            true_ranges[i]
        ) / period

        result[i] = average

    return result


def rolling_high(
    candles: list[dict[str, float]],
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
    candles: list[dict[str, float]],
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
    trend_candles: list[dict[str, float]],
    entry_candles: list[dict[str, float]],
) -> dict[str, Any]:

    if len(trend_candles) < 60:
        raise ValueError(
            "Not enough trend candles"
        )

    if len(entry_candles) < 60:
        raise ValueError(
            "Not enough entry candles"
        )

    # --------------------------------------------------------
    # 1H TREND
    # --------------------------------------------------------

    trend_close = [
        x["close"]
        for x in trend_candles
    ]

    trend_ema20 = ema(
        trend_close,
        Config.ema_fast,
    )

    trend_ema50 = ema(
        trend_close,
        Config.ema_slow,
    )

    ti = (
        len(trend_candles) - 1
    )

    trend_price = trend_close[ti]

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
        len(entry_candles) - 1
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
    # BREAKOUT / BREAKDOWN
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
        resistance
        *
        (
            1
            +
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
        support
        *
        (
            1
            -
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
        previous["high"]
        >
        resistance
        and
        previous["close"]
        <=
        resistance
    )

    fake_breakdown = (
        previous["low"]
        <
        support
        and
        previous["close"]
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
    # FINAL SIGNAL
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

    # --------------------------------------------------------
    # EXIT
    # --------------------------------------------------------

    exit_long = (
        current["close"]
        <
        ema20_values[i]
        or
        current_rsi
        <
        45
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
        current_rsi
        >
        55
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
# OKX CLIENT
# ============================================================

class OKXClient:

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

        request_path = path

        if query_string:
            request_path += (
                "?"
                +
                query_string
            )

        payload = ""

        if body is not None:

            payload = json.dumps(
                body,
                separators=(
                    ",",
                    ":",
                ),
            )

        headers = {
            "Content-Type":
                "application/json",
            "User-Agent":
                "Railway-OKX-Trading-Bot/1.0",
        }

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

                raw = (
                    response
                    .read()
                    .decode(
                        errors="replace"
                    )
                )

                result = json.loads(
                    raw
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
                f"OKX network error: {exc}"
            ) from exc

        except json.JSONDecodeError as exc:

            raise RuntimeError(
                "OKX returned invalid JSON"
            ) from exc

        if result.get("code") != "0":

            raise RuntimeError(
                "OKX error "
                +
                str(
                    result.get(
                        "msg",
                        "unknown",
                    )
                )
            )

        return result.get(
            "data",
            [],
        )

    def public_test(self) -> bool:

        data = self.request(
            "/api/v5/public/time"
        )

        return bool(data)

    def private_test(self) -> bool:

        data = self.request(
            "/api/v5/account/balance",
            private=True,
        )

        return bool(data)

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
                "Instrument not found: "
                +
                Config.inst_id
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
        }

    def get_candles(
        self,
        bar: str,
    ) -> list[dict[str, float]]:

        rows = self.request(

            "/api/v5/market/candles",

            query={
                "instId":
                    Config.inst_id,

                "bar":
                    bar,

                "limit":
                    str(
                        min(
                            Config.candle_limit,
                            300,
                        )
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

            try:

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

            except (
                ValueError,
                TypeError,
            ):

                continue

            if all(
                math.isfinite(x)
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
                (
                    format(
                        contracts,
                        ".12f",
                    )
                    .rstrip("0")
                    .rstrip(".")
                ),

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
                "Empty order response"
            )

        item = data[0]

        if item.get(
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
                    item.get(
                        "sMsg",
                        "",
                    )
                )
            )

        return item

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
                (
                    format(
                        contracts,
                        ".12f",
                    )
                    .rstrip("0")
                    .rstrip(".")
                ),

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

        item = data[0]

        if item.get(
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
                    item.get(
                        "sMsg",
                        "",
                    )
                )
            )

        return item


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

        if not isinstance(
            data,
            dict,
        ):
            return default

        return {
            **default,
            **data,
        }

    except Exception:

        log_event(
            "STATE_RESET"
        )

        return default


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
# TRADING BOT
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
    # STARTUP
    # --------------------------------------------------------

    def startup(self) -> None:

        log_event(
            "BOT_STARTING",
            symbol=Config.inst_id,
            demo=Config.okx_demo,
            dry_run=Config.dry_run,
            port=Config.port,
        )

        # Public connection
        self.client.public_test()

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
            "INSTRUMENT_READY",
            symbol=Config.inst_id,
            contract_value=
                self.instrument["ct_val"],
            lot_size=
                self.instrument["lot_sz"],
            min_size=
                self.instrument["min_sz"],
        )

        # Private connection
        if Config.dry_run:

            log_event(
                "PAPER_MODE",
                message2=(
                    "DRY_RUN=true; "
                    "real orders disabled"
                ),
            )

        else:

            self.client.private_test()

            self.status[
                "okx_private"
            ] = True

            log_event(
                "OKX_PRIVATE_CONNECTION_SUCCESS"
            )

            self.client.set_leverage()

            log_event(
                "LEVERAGE_SET",
                leverage=Config.leverage,
                margin_mode=Config.margin_mode,
            )

        log_event(
            "CONFIG_READY",
            margin=Config.margin_usdt,
            leverage=Config.leverage,
            maximum_position=
                Config.max_position_usdt,
        )

    # --------------------------------------------------------
    # CONTRACT CALCULATION
    # --------------------------------------------------------

    def calculate_contracts(
        self,
        price: float,
    ) -> float:

        if not self.instrument:

            raise RuntimeError(
                "Instrument unavailable"
            )

        ct_val = self.instrument[
            "ct_val"
        ]

        lot_sz = self.instrument[
            "lot_sz"
        ]

        min_sz = self.instrument[
            "min_sz"
        ]

        position_usdt = min(
            Config.margin_usdt
            *
            Config.leverage,

            Config.max_position_usdt,
        )

        contracts = (
            position_usdt
            /
            (
                ct_val
                *
                price
            )
        )

        contracts = floor_to_step(
            contracts,
            lot_sz,
        )

        contracts = clean_number(
            contracts
        )

        if contracts < min_sz:

            raise RuntimeError(
                f"Calculated size {contracts} "
                f"is below minimum {min_sz}"
            )

        return contracts

    # --------------------------------------------------------
    # OPEN
    # --------------------------------------------------------

    def open_position(
        self,
        signal_data: dict[str, Any],
    ) -> None:

        direction = signal_data[
            "direction"
        ]

        entry = float(
            signal_data[
                "candle"
            ]["close"]
        )

        atr_value = float(
            signal_data["atr"]
        )

        distance = (
            atr_value
            *
            Config.stop_atr_multiplier
        )

        if direction == "long":

            side = "buy"

            stop = (
                entry
                -
                distance
            )

            target = (
                entry
                +
                distance
                *
                Config.reward_to_risk
            )

        elif direction == "short":

            side = "sell"

            stop = (
                entry
                +
                distance
            )

            target = (
                entry
                -
                distance
                *
                Config.reward_to_risk
            )

        else:

            return

        contracts = (
            self.calculate_contracts(
                entry
            )
        )

        notional = (
            contracts
            *
            self.instrument[
                "ct_val"
            ]
            *
            entry
        )

        actual_margin = (
            notional
            /
            Config.leverage
        )

        # ----------------------------------------------------
        # PAPER
        # ----------------------------------------------------

        if Config.dry_run:

            order_id = (
                "paper-"
                +
                str(
                    int(
                        time.time()
                        * 1000
                    )
                )
            )

            fill_price = entry

            log_event(
                "PAPER_ENTRY",
                direction=direction,
                contracts=contracts,
                notional=
                    clean_number(
                        notional
                    ),
                margin=
                    clean_number(
                        actual_margin
                    ),
                leverage=Config.leverage,
                entry=
                    clean_number(
                        fill_price
                    ),
                stop=
                    clean_number(
                        stop
                    ),
                target=
                    clean_number(
                        target
                    ),
            )

        # ----------------------------------------------------
        # OKX
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

            fill_price = entry

            log_event(
                "OKX_ENTRY_ORDER",
                direction=direction,
                order_id=order_id,
                contracts=contracts,
            )

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
                    stop,

                "target_price":
                    target,

                "last_order_id":
                    order_id,
            }
        )

        save_state(
            self.state
        )

    # --------------------------------------------------------
    # CLOSE
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

        quantity = float(
            self.state[
                "quantity"
            ]
        )

        if Config.dry_run:

            log_event(
                "PAPER_EXIT",
                reason=reason,
                direction=direction,
                quantity=quantity,
                price=price,
            )

        else:

            order = (
                self.client.close_position(
                    direction,
                    quantity,
                )
            )

            log_event(
                "OKX_EXIT",
                reason=reason,
                direction=direction,
                quantity=quantity,
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
    # EXCHANGE RECONCILIATION
    # --------------------------------------------------------

    def reconcile(self) -> None:

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
                    "EXCHANGE_POSITION_CLOSED"
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

        direction = "long"

        if position.get(
            "posSide"
        ) == "short":

            direction = "short"

        elif position.get(
            "posSide"
        ) == "long":

            direction = "long"

        else:

            if pos < 0:
                direction = "short"

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
    # ONE TICK
    # --------------------------------------------------------

    def run_once(self) -> None:

        if self.busy:
            return

        self.busy = True

        self.status[
            "last_run"
        ] = now_iso()

        try:

            self.reconcile()

            trend = (
                self.client.get_candles(
                    Config.trend_bar
                )
            )

            entry = (
                self.client.get_candles(
                    Config.entry_bar
                )
            )

            signal_data = (
                calculate_signal(
                    trend,
                    entry,
                )
            )

            timestamp = (
                signal_data[
                    "timestamp"
                ]
            )

            # ----------------------------------------------
            # Same candle protection
            # ----------------------------------------------

            if (
                self.state[
                    "last_candle_timestamp"
                ]
                ==
                timestamp
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

            candle = signal_data[
                "candle"
            ]

            log_event(
                "SIGNAL",
                direction=direction,
                price=candle["close"],
                ema20=
                    clean_number(
                        signal_data["ema20"]
                    ),
                ema50=
                    clean_number(
                        signal_data["ema50"]
                    ),
                rsi=
                    clean_number(
                        signal_data["rsi"]
                    ),
                atr=
                    clean_number(
                        signal_data["atr"]
                    ),
                resistance=
                    clean_number(
                        signal_data["resistance"]
                    ),
                support=
                    clean_number(
                        signal_data["support"]
                    ),
            )

            # ----------------------------------------------
            # Existing position
            # ----------------------------------------------

            if self.state[
                "in_position"
            ]:

                current_direction = (
                    self.state[
                        "direction"
                    ]
                )

                stop = float(
                    self.state[
                        "stop_price"
                    ]
                )

                target = float(
                    self.state[
                        "target_price"
                    ]
                )

                if current_direction == "long":

                    if candle["low"] <= stop:

                        self.close_position(
                            "stop_loss",
                            candle["close"],
                        )

                    elif candle["high"] >= target:

                        self.close_position(
                            "take_profit",
                            candle["close"],
                        )

                    elif signal_data[
                        "exit_long"
                    ]:

                        self.close_position(
                            "trend_exit",
                            candle["close"],
                        )

                elif current_direction == "short":

                    if candle["high"] >= stop:

                        self.close_position(
                            "stop_loss",
                            candle["close"],
                        )

                    elif candle["low"] <= target:

                        self.close_position(
                            "take_profit",
                            candle["close"],
                        )

                    elif signal_data[
                        "exit_short"
                    ]:

                        self.close_position(
                            "trend_exit",
                            candle["close"],
                        )

            # ----------------------------------------------
            # New position
            # ----------------------------------------------

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
                "TICK_ERROR",
                error=str(exc),
            )

        finally:

            self.busy = False

    # --------------------------------------------------------
    # MAIN LOOP
    # --------------------------------------------------------

    def run(self) -> None:

        # Startup errors should not kill Railway.
        while not self.stop_event.is_set():

            try:

                if not self.status[
                    "okx_public"
                ]:

                    self.startup()

                    self.status[
                        "running"
                    ] = True

                    log_event(
                        "BOT_RUNNING"
                    )

                self.run_once()

            except Exception as exc:

                self.status[
                    "last_error"
                ] = str(exc)

                self.status[
                    "okx_public"
                ] = False

                self.status[
                    "okx_private"
                ] = False

                log_event(
                    "CONNECTION_OR_STARTUP_ERROR",
                    error=str(exc),
                    retry_seconds=30,
                )

                self.stop_event.wait(
                    30
                )

                continue

            self.stop_event.wait(
                Config.poll_seconds
            )


# ============================================================
# RAILWAY HEALTH SERVER
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    bot: TradingBot | None = None

    def do_GET(
        self,
    ) -> None:

        if self.path in {
            "/",
            "/health",
        }:

            bot = self.bot

            if bot is None:

                payload = {
                    "ok": False,
                    "error":
                        "bot not initialized",
                }

                code = 503

            else:

                payload = {
                    "ok": True,
                    **bot.status,
                    "position":
                        bot.state,
                }

                code = 200

            response = json.dumps(
                payload,
                default=str,
            ).encode()

            self.send_response(
                code
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

        return


def start_server(
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
        target=server.serve_forever,
        daemon=True,
    )

    thread.start()

    log_event(
        "RAILWAY_SERVER_STARTED",
        host="0.0.0.0",
        port=Config.port,
        health="/health",
    )

    return server


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    validate_config()

    bot = TradingBot()

    # Start Railway server FIRST.
    server = start_server(
        bot
    )

    def shutdown(
        _signum: int,
        _frame: Any,
    ) -> None:

        log_event(
            "SHUTDOWN"
        )

        bot.stop_event.set()

        try:
            server.shutdown()
        except Exception:
            pass

    signal.signal(
        signal.SIGTERM,
        shutdown,
    )

    signal.signal(
        signal.SIGINT,
        shutdown,
    )

    try:

        # Bot itself contains retry logic.
        bot.run()

    except Exception as exc:

        log_event(
            "FATAL_ERROR",
            error=str(exc),
        )

        raise

    finally:

        try:
            server.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
