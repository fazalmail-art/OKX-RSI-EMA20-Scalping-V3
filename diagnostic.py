import os
import time
import requests
from decimal import Decimal

BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com")

SYMBOLS = [
    x.strip()
    for x in os.getenv(
        "SYMBOLS",
        "BTC-USDT-SWAP,ETH-USDT-SWAP,XRP-USDT-SWAP,DOGE-USDT-SWAP"
    ).split(",")
    if x.strip()
]

BAR = os.getenv("BAR", "5m")
TREND_BAR = os.getenv("TREND_BAR", "15m")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))

ADX_MIN = Decimal(os.getenv("ADX_MIN", "18"))
VOLUME_MULT = Decimal(os.getenv("VOLUME_MULT", "0.8"))
ATR_MIN_PCT = Decimal(os.getenv("ATR_MIN_PCT", "0.05"))


def get_candles(symbol, bar, limit=160):

    response = requests.get(
        BASE_URL + "/api/v5/market/candles",
        params={
            "instId": symbol,
            "bar": bar,
            "limit": str(limit)
        },
        timeout=15
    )

    response.raise_for_status()

    data = response.json()

    if data.get("code") != "0":
        raise RuntimeError(
            f"OKX ERROR: {data.get('msg')}"
        )

    candles = []

    for row in reversed(data.get("data", [])):

        candles.append({
            "ts": int(row[0]),
            "open": Decimal(row[1]),
            "high": Decimal(row[2]),
            "low": Decimal(row[3]),
            "close": Decimal(row[4]),
            "volume": Decimal(row[5]),
            "confirm": row[8] if len(row) > 8 else "1"
        })

    return [
        x for x in candles
        if x["confirm"] == "1"
    ]


def ema(values, period):

    if len(values) < period:
        return None

    value = (
        sum(values[:period], Decimal("0"))
        / Decimal(period)
    )

    multiplier = (
        Decimal("2")
        / Decimal(period + 1)
    )

    for value_now in values[period:]:

        value = (
            value_now * multiplier
            + value * (Decimal("1") - multiplier)
        )

    return value


def rsi(values, period):

    if len(values) <= period:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):

        change = values[i] - values[i - 1]

        gains.append(
            max(change, Decimal("0"))
        )

        losses.append(
            max(-change, Decimal("0"))
        )

    avg_gain = (
        sum(gains[:period], Decimal("0"))
        / Decimal(period)
    )

    avg_loss = (
        sum(losses[:period], Decimal("0"))
        / Decimal(period)
    )

    for i in range(period, len(gains)):

        avg_gain = (
            avg_gain * (period - 1)
            + gains[i]
        ) / Decimal(period)

        avg_loss = (
            avg_loss * (period - 1)
            + losses[i]
        ) / Decimal(period)

    if avg_loss == 0:
        return Decimal("100")

    rs = avg_gain / avg_loss

    return (
        Decimal("100")
        - Decimal("100")
        / (Decimal("1") + rs)
    )


def atr(candles, period=14):

    if len(candles) <= period:
        return None

    trs = []

    for i in range(1, len(candles)):

        high = candles[i]["high"]
        low = candles[i]["low"]
        previous_close = candles[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        trs.append(tr)

    return (
        sum(trs[-period:], Decimal("0"))
        / Decimal(period)
    )


def adx(candles, period=14):

    if len(candles) < period + 2:
        return Decimal("0")

    plus = Decimal("0")
    minus = Decimal("0")
    total_range = Decimal("0")

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

        if up_move > down_move and up_move > 0:
            plus += up_move

        if down_move > up_move and down_move > 0:
            minus += down_move

        total_range += (
            candles[i]["high"]
            - candles[i]["low"]
        )

    if total_range == 0:
        return Decimal("0")

    plus_di = (
        plus / total_range * Decimal("100")
    )

    minus_di = (
        minus / total_range * Decimal("100")
    )

    total = plus_di + minus_di

    if total == 0:
        return Decimal("0")

    return (
        abs(plus_di - minus_di)
        / total
        * Decimal("100")
    )


def analyze(symbol):

    candles = get_candles(
        symbol,
        BAR,
        160
    )

    if len(candles) < 105:

        print(
            f"{symbol}: NOT ENOUGH DATA"
        )

        return

    closes = [
        x["close"]
        for x in candles
    ]

    current = len(candles) - 1
    previous = current - 1

    rsi14_now = rsi(closes, 14)

    rsi14_previous = rsi(
        closes[:current],
        14
    )

    rsi100_now = rsi(
        closes,
        100
    )

    rsi100_previous = rsi(
        closes[:current],
        100
    )

    ema20_now = ema(
        closes,
        20
    )

    ema20_previous = ema(
        closes[:current],
        20
    )

    current_price = closes[current]

    current_atr = atr(
        candles,
        14
    )

    current_adx = adx(
        candles,
        14
    )

    average_volume = (
        sum(
            x["volume"]
            for x in candles[-21:-1]
        )
        / Decimal("20")
    )

    current_volume = candles[current]["volume"]

    volume_ok = (
        current_volume
        >= average_volume * VOLUME_MULT
    )

    atr_percent = Decimal("0")

    if current_atr and current_price:

        atr_percent = (
            current_atr
            / current_price
            * Decimal("100")
        )

    adx_ok = current_adx >= ADX_MIN

    atr_ok = atr_percent >= ATR_MIN_PCT

    buy_rsi = (
        rsi14_previous is not None
        and rsi100_previous is not None
        and rsi14_now is not None
        and rsi100_now is not None
        and rsi14_previous <= rsi100_previous
        and rsi14_now > rsi100_now
    )

    sell_rsi = (
        rsi14_previous is not None
        and rsi100_previous is not None
        and rsi14_now is not None
        and rsi100_now is not None
        and rsi14_previous >= rsi100_previous
        and rsi14_now < rsi100_now
    )

    buy_ema = (
        closes[previous] <= ema20_previous
        and closes[current] > ema20_now
        and rsi14_now <= Decimal("55")
    )

    sell_ema = (
        closes[previous] >= ema20_previous
        and closes[current] < ema20_now
        and rsi14_now >= Decimal("45")
    )

    trend = "FLAT"

    try:

        trend_candles = get_candles(
            symbol,
            TREND_BAR,
            80
        )

        trend_closes = [
            x["close"]
            for x in trend_candles
        ]

        trend_ema = ema(
            trend_closes,
            20
        )

        if trend_ema:

            trend_price = trend_closes[-1]

            if trend_price > trend_ema:
                trend = "BULL"

            elif trend_price < trend_ema:
                trend = "BEAR"

    except Exception as error:

        trend = "ERROR"

        print(
            f"{symbol}: TREND ERROR: {error}"
        )

    rsi_signal = "NONE"

    if buy_rsi:
        rsi_signal = "BUY"

    elif sell_rsi:
        rsi_signal = "SELL"

    ema_signal = "NONE"

    if buy_ema:
        ema_signal = "BUY"

    elif sell_ema:
        ema_signal = "SELL"

    final_signal = "NONE"

    if rsi_signal == "BUY" or ema_signal == "BUY":
        final_signal = "BUY"

    elif rsi_signal == "SELL" or ema_signal == "SELL":
        final_signal = "SELL"

    trend_ok = True

    if trend == "BULL" and final_signal == "SELL":
        trend_ok = False

    if trend == "BEAR" and final_signal == "BUY":
        trend_ok = False

    all_filters_ok = (
        final_signal != "NONE"
        and adx_ok
        and volume_ok
        and atr_ok
        and trend_ok
    )

    print("")
    print("=" * 55)
    print(f"{symbol} | {BAR}")
    print("=" * 55)

    print(
        f"Price       : {current_price}"
    )

    print(
        f"RSI14       : {rsi14_now}"
    )

    print(
        f"RSI100      : {rsi100_now}"
    )

    print(
        f"RSI Signal  : {rsi_signal}"
    )

    print(
        f"EMA20       : {ema20_now}"
    )

    print(
        f"EMA Signal  : {ema_signal}"
    )

    print(
        f"ADX         : {current_adx} "
        f"({'PASS' if adx_ok else 'FAIL'})"
    )

    print(
        f"Volume      : {current_volume}"
    )

    print(
        f"Volume Avg  : {average_volume}"
    )

    print(
        f"Volume Test : "
        f"{'PASS' if volume_ok else 'FAIL'}"
    )

    print(
        f"ATR %       : {atr_percent} "
        f"({'PASS' if atr_ok else 'FAIL'})"
    )

    print(
        f"15m Trend   : {trend}"
    )

    print(
        f"Trend Test  : "
        f"{'PASS' if trend_ok else 'FAIL'}"
    )

    print("-" * 55)

    if all_filters_ok:

        print(
            f"FINAL SIGNAL: {final_signal} "
            f"*** ALL FILTERS PASS ***"
        )

    elif final_signal == "NONE":

        print(
            "FINAL SIGNAL: NONE "
            "| RSI/EMA entry not triggered"
        )

    else:

        print(
            f"FINAL SIGNAL: BLOCKED "
            f"| ADX={adx_ok} "
            f"| Volume={volume_ok} "
            f"| ATR={atr_ok} "
            f"| Trend={trend_ok}"
        )

    print("=" * 55)


def main():

    print("")
    print(
        "OKX SCALPING DIAGNOSTIC V1"
    )

    print(
        "This program ONLY analyzes."
    )

    print(
        "It DOES NOT place orders."
    )

    print(
        f"Symbols: {SYMBOLS}"
    )

    print(
        f"Timeframe: {BAR}"
    )

    print(
        f"Trend timeframe: {TREND_BAR}"
    )

    while True:

        for symbol in SYMBOLS:

            try:

                analyze(symbol)

            except Exception as error:

                print(
                    f"{symbol}: ERROR: "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

        print(
            f"\nWaiting {POLL_SECONDS} seconds..."
        )

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
