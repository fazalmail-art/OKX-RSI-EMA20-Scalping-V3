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

import requests
from flask import Flask, jsonify, Response
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# OKX TREND-PULLBACK BOT v1.0
# Designed for OKX SWAP demo first. Not financial advice.
#
# Strategy: 15m entries aligned with 1H trend.
# Entry is either a confirmed breakout or a pullback/reclaim of EMA20.
# No OI, funding, session-window, correlation, or multi-stage PA vetoes.
# Safety retained: closed candles, ATR SL/TP, risk limits, cooldown,
# exposure cap, fresh signal revalidation, and protective OCO.
# ============================================================

BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
DEMO = os.getenv("OKX_DEMO", "true").lower() == "true"
AUTO_TRADE = os.getenv("AUTO_TRADE", "false").lower() == "true"
ALLOW_LIVE = os.getenv("ALLOW_LIVE", "false").lower() == "true"

SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", "ETH-USDT-SWAP,BTC-USDT-SWAP,SOL-USDT-SWAP").split(",") if s.strip()]
ENTRY_BAR = os.getenv("ENTRY_BAR", "15m")
TREND_BAR = os.getenv("TREND_BAR", "1H")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))

MARGIN_USDT = Decimal(os.getenv("MARGIN_USDT", "10"))
LEVERAGE = Decimal(os.getenv("LEVERAGE", "3"))
TD_MODE = os.getenv("TD_MODE", "isolated")
MAX_TOTAL_NOTIONAL_USDT = Decimal(os.getenv("MAX_TOTAL_NOTIONAL_USDT", "300"))

# Signal controls. Trendline-assisted continuation model.
MIN_SCORE = int(os.getenv("MIN_SCORE", "5"))
BREAKOUT_LOOKBACK = int(os.getenv("BREAKOUT_LOOKBACK", "20"))
PULLBACK_LOOKBACK = int(os.getenv("PULLBACK_LOOKBACK", "3"))
PULLBACK_ATR_DISTANCE = Decimal(os.getenv("PULLBACK_ATR_DISTANCE", "0.65"))
TRENDLINE_LOOKBACK = int(os.getenv("TRENDLINE_LOOKBACK", "80"))
TRENDLINE_MIN_TOUCHES = int(os.getenv("TRENDLINE_MIN_TOUCHES", "2"))
TRENDLINE_MAX_DISTANCE_ATR = Decimal(os.getenv("TRENDLINE_MAX_DISTANCE_ATR", "0.70"))
MAX_EXTENSION_ATR = Decimal(os.getenv("MAX_EXTENSION_ATR", "2.50"))
MIN_ADX = Decimal(os.getenv("MIN_ADX", "16"))
MIN_VOLUME_RATIO = Decimal(os.getenv("MIN_VOLUME_RATIO", "0.75"))
ATR_MIN_PCT = Decimal(os.getenv("ATR_MIN_PCT", "0.05"))

SL_ATR_MULT = Decimal(os.getenv("SL_ATR_MULT", "1.5"))
TP_ATR_MULT = Decimal(os.getenv("TP_ATR_MULT", "2.2"))
MIN_SL_PCT = Decimal(os.getenv("MIN_SL_PCT", "0.30"))
MAX_SL_PCT = Decimal(os.getenv("MAX_SL_PCT", "2.50"))

COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "900"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "4"))
DAILY_MAX_LOSS_USDT = Decimal(os.getenv("DAILY_MAX_LOSS_USDT", "30"))
MAX_ENTRY_SLIPPAGE_PCT = Decimal(os.getenv("MAX_ENTRY_SLIPPAGE_PCT", "0.20"))
ENTRY_ORDER_TYPE = os.getenv("ENTRY_ORDER_TYPE", "market")

app = Flask(__name__)
session = requests.Session()
state_lock = threading.Lock()
order_lock = threading.Lock()
state = {s: {"signal": "NONE", "status": "STARTING"} for s in SYMBOLS}
last_close_time = {}
risk = {"halted": False, "reason": "", "day": None, "day_start_equity": None, "consecutive_losses": 0}
position_snapshot = {}
position_snapshot_ts = 0.0
position_mode = "net"
worker_started = False
worker_error = ""


def D(x):
    return Decimal(str(x))


def fmt(x, places=12):
    if x is None:
        return "-"
    return f"{D(x):.{places}f}".rstrip("0").rstrip(".")


def floor_step(value, step):
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step if step > 0 else value


def ceil_step(value, step):
    return (value / step).to_integral_value(rounding=ROUND_UP) * step if step > 0 else value


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sign(timestamp, method, path, body=""):
    msg = timestamp + method.upper() + path + body
    return base64.b64encode(hmac.new(SECRET_KEY.encode(), msg.encode(), hashlib.sha256).digest()).decode()


def public_get(path, params=None):
    r = session.get(BASE_URL + path, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if str(data.get("code", "0")) != "0":
        raise RuntimeError(data.get("msg", "OKX public API error"))
    return data


def private_request(method, path, params=None, payload=None):
    if not (API_KEY and SECRET_KEY and PASSPHRASE):
        raise RuntimeError("OKX private credentials are not configured")
    body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
    query = ("?" + "&".join(f"{k}={v}" for k, v in (params or {}).items())) if params else ""
    request_path = path + query
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    headers = {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": sign(timestamp, method, request_path, body),
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
    }
    if DEMO:
        headers["x-simulated-trading"] = "1"
    r = session.request(method, BASE_URL + request_path, headers=headers, data=body or None, timeout=15)
    r.raise_for_status()
    data = r.json()
    if str(data.get("code", "")) != "0":
        raise RuntimeError(f"OKX private error {data.get('code')}: {data.get('msg')}")
    return data


def get_candles(symbol, bar, limit=180):
    rows = public_get("/api/v5/market/candles", {"instId": symbol, "bar": bar, "limit": str(limit)}).get("data", [])
    # OKX returns newest first. Exclude the currently-forming candle.
    rows = list(reversed(rows))
    out = []
    for r in rows:
        if len(r) >= 9 and r[8] != "1":
            continue
        out.append({"ts": int(r[0]), "open": D(r[1]), "high": D(r[2]), "low": D(r[3]), "close": D(r[4]), "volume": D(r[5])})
    return out


def ema(values, period):
    if len(values) < period:
        return [None] * len(values)
    out = [None] * len(values)
    x = sum(values[:period], D(0)) / D(period)
    out[period - 1] = x
    k = D(2) / D(period + 1)
    for i in range(period, len(values)):
        x = values[i] * k + x * (D(1) - k)
        out[i] = x
    return out


def atr(candles, period=14):
    if len(candles) <= period:
        return None
    tr = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        tr.append(max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"])))
    x = sum(tr[:period], D(0)) / D(period)
    for v in tr[period:]:
        x = (x * D(period - 1) + v) / D(period)
    return x


def adx(candles, period=14):
    if len(candles) < period * 2 + 3:
        return D(0)
    plus, minus, tr = [], [], []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        up, down = c["high"] - p["high"], p["low"] - c["low"]
        plus.append(up if up > down and up > 0 else D(0))
        minus.append(down if down > up and down > 0 else D(0))
        tr.append(max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"])))
    sp = sum(plus[:period], D(0)) / D(period)
    sm = sum(minus[:period], D(0)) / D(period)
    st = sum(tr[:period], D(0)) / D(period)
    dx = []
    for i in range(period, len(tr)):
        sp = (sp * D(period - 1) + plus[i]) / D(period)
        sm = (sm * D(period - 1) + minus[i]) / D(period)
        st = (st * D(period - 1) + tr[i]) / D(period)
        pdi, mdi = sp / st * 100 if st else D(0), sm / st * 100 if st else D(0)
        dx.append(abs(pdi - mdi) / (pdi + mdi) * 100 if pdi + mdi else D(0))
    return sum(dx[-period:], D(0)) / D(min(period, len(dx))) if dx else D(0)


def vwap(candles, lookback=48):
    q = candles[-lookback:]
    vol = sum(x["volume"] for x in q)
    return sum(((x["high"] + x["low"] + x["close"]) / 3) * x["volume"] for x in q) / vol if vol else q[-1]["close"]


def get_instrument(symbol):
    rows = public_get("/api/v5/public/instruments", {"instType": "SWAP", "instId": symbol}).get("data", [])
    if not rows:
        raise RuntimeError(f"Instrument not found: {symbol}")
    r = rows[0]
    return {"ctVal": D(r["ctVal"]), "lotSz": D(r["lotSz"]), "minSz": D(r["minSz"]), "tickSz": D(r["tickSz"]), "state": r.get("state")}


def ticker(symbol):
    rows = public_get("/api/v5/market/ticker", {"instId": symbol}).get("data", [])
    return D(rows[0]["last"]) if rows else None


def mark_price(symbol):
    rows = public_get("/api/v5/public/mark-price", {"instType": "SWAP", "instId": symbol}).get("data", [])
    return D(rows[0]["markPx"]) if rows else ticker(symbol)


def refresh_positions():
    global position_snapshot, position_snapshot_ts
    data = private_request("GET", "/api/v5/account/positions", {"instType": "SWAP"})
    snap = {r.get("instId"): r for r in data.get("data", []) if D(r.get("pos", "0")) != 0}
    with state_lock:
        position_snapshot, position_snapshot_ts = snap, time.time()
    return snap


def position_notional(symbol, pos):
    if not pos:
        return D(0)
    i = get_instrument(symbol)
    return abs(D(pos.get("pos", "0"))) * i["ctVal"] * D(pos.get("markPx") or pos.get("avgPx") or "0")


def total_exposure(snap=None):
    snap = snap if snap is not None else position_snapshot
    return sum((position_notional(s, p) for s, p in snap.items()), D(0))


def account_equity():
    data = private_request("GET", "/api/v5/account/balance")
    rows = data.get("data", [])
    if not rows:
        return None
    usdt = next((x for x in rows[0].get("details", []) if x.get("ccy") == "USDT"), None)
    return D(usdt.get("eq") or usdt.get("cashBal")) if usdt else D(rows[0].get("totalEq", "0"))


def confirmed_swings(candles, side, lookback):
    """Return confirmed swing lows/highs; the last two candles are never used."""
    q = candles[-lookback:]
    points = []
    for i in range(2, len(q) - 2):
        if side == "low" and q[i]["low"] <= min(q[i - 2]["low"], q[i - 1]["low"], q[i + 1]["low"], q[i + 2]["low"]):
            points.append((len(candles) - lookback + i, q[i]["low"]))
        if side == "high" and q[i]["high"] >= max(q[i - 2]["high"], q[i - 1]["high"], q[i + 1]["high"], q[i + 2]["high"]):
            points.append((len(candles) - lookback + i, q[i]["high"]))
    return points


def trendline_context(candles, atr_value):
    """Build rising/falling lines from the two latest confirmed swings."""
    lows = confirmed_swings(candles, "low", TRENDLINE_LOOKBACK)
    highs = confirmed_swings(candles, "high", TRENDLINE_LOOKBACK)
    i = len(candles) - 1
    result = {"rising": False, "falling": False, "bull_touch": False, "bear_touch": False, "bull_break": False, "bear_break": False, "bull_touches": 0, "bear_touches": 0}
    if len(lows) >= 2:
        (i1, p1), (i2, p2) = lows[-2], lows[-1]
        if i2 > i1 and p2 > p1:
            slope = (p2 - p1) / D(i2 - i1)
            line_now = p1 + slope * D(i - i1)
            distance = abs(candles[-1]["close"] - line_now)
            touches = sum(abs(p - (p1 + slope * D(j - i1))) <= atr_value * TRENDLINE_MAX_DISTANCE_ATR for j, p in lows)
            result.update({"rising": True, "bull_touch": distance <= atr_value * TRENDLINE_MAX_DISTANCE_ATR, "bull_break": candles[-1]["close"] > line_now, "bull_touches": touches, "bull_line": line_now})
    if len(highs) >= 2:
        (i1, p1), (i2, p2) = highs[-2], highs[-1]
        if i2 > i1 and p2 < p1:
            slope = (p2 - p1) / D(i2 - i1)
            line_now = p1 + slope * D(i - i1)
            distance = abs(candles[-1]["close"] - line_now)
            touches = sum(abs(p - (p1 + slope * D(j - i1))) <= atr_value * TRENDLINE_MAX_DISTANCE_ATR for j, p in highs)
            result.update({"falling": True, "bear_touch": distance <= atr_value * TRENDLINE_MAX_DISTANCE_ATR, "bear_break": candles[-1]["close"] < line_now, "bear_touches": touches, "bear_line": line_now})
    return result


def detect_signal(symbol):
    c = get_candles(symbol, ENTRY_BAR, 220)
    h = get_candles(symbol, TREND_BAR, 100)
    if len(c) < 60 or len(h) < 30:
        return {"signal": "NONE", "reason": "Not enough confirmed candles"}
    close = c[-1]["close"]
    vals = [x["close"] for x in c]
    e20 = ema(vals, 20)
    hv = [x["close"] for x in h]
    he20 = ema(hv, 20)
    av = atr(c)
    if not av or not e20[-1] or not he20[-1] or not he20[-2]:
        return {"signal": "NONE", "reason": "Indicator unavailable"}
    trend = "bull" if hv[-1] > he20[-1] and he20[-1] > he20[-2] else "bear" if hv[-1] < he20[-1] and he20[-1] < he20[-2] else "flat"
    vw = vwap(c)
    adx_v = adx(c)
    avg_vol = sum((x["volume"] for x in c[-21:-1]), D(0)) / 20
    vol_ratio = c[-1]["volume"] / avg_vol if avg_vol else D(0)
    atr_pct = av / close * 100
    tl = trendline_context(c, av)
    recent_high = max(x["high"] for x in c[-BREAKOUT_LOOKBACK-1:-1])
    recent_low = min(x["low"] for x in c[-BREAKOUT_LOOKBACK-1:-1])
    bullish_candle = c[-1]["close"] > c[-1]["open"]
    bearish_candle = c[-1]["close"] < c[-1]["open"]
    # A pullback must touch EMA20 or the rising/falling trendline and close back in trend direction.
    bull_tl_ok = tl.get("rising") and tl.get("bull_touches", 0) >= TRENDLINE_MIN_TOUCHES
    bear_tl_ok = tl.get("falling") and tl.get("bear_touches", 0) >= TRENDLINE_MIN_TOUCHES
    pullback_bull = any(x["low"] <= e20[-1] + av * PULLBACK_ATR_DISTANCE for x in c[-PULLBACK_LOOKBACK:]) and (tl.get("bull_touch") or bull_tl_ok) and close > e20[-1] and bullish_candle
    pullback_bear = any(x["high"] >= e20[-1] - av * PULLBACK_ATR_DISTANCE for x in c[-PULLBACK_LOOKBACK:]) and (tl.get("bear_touch") or bear_tl_ok) and close < e20[-1] and bearish_candle
    breakout_bull = close > recent_high and bullish_candle and (tl.get("bull_break") or bull_tl_ok)
    breakout_bear = close < recent_low and bearish_candle and (tl.get("bear_break") or bear_tl_ok)
    not_extended_bull = (close - e20[-1]) <= av * MAX_EXTENSION_ATR
    not_extended_bear = (e20[-1] - close) <= av * MAX_EXTENSION_ATR
    # Eight transparent points: trend, EMA, VWAP, ADX, volume, trendline, trigger, no-chase.
    buy_points = [trend == "bull", close > e20[-1], close > vw, adx_v >= MIN_ADX and atr_pct >= ATR_MIN_PCT, vol_ratio >= MIN_VOLUME_RATIO or breakout_bull, bool(bull_tl_ok), (pullback_bull or breakout_bull), not_extended_bull]
    sell_points = [trend == "bear", close < e20[-1], close < vw, adx_v >= MIN_ADX and atr_pct >= ATR_MIN_PCT, vol_ratio >= MIN_VOLUME_RATIO or breakout_bear, bool(bear_tl_ok), (pullback_bear or breakout_bear), not_extended_bear]
    buy, sell = sum(buy_points), sum(sell_points)
    signal = "BUY" if buy >= MIN_SCORE and buy > sell and trend == "bull" and (pullback_bull or breakout_bull) else "SELL" if sell >= MIN_SCORE and sell > buy and trend == "bear" and (pullback_bear or breakout_bear) else "NONE"
    return {"signal": signal, "score": max(buy, sell), "buy": buy, "sell": sell, "max_score": 8, "required_score": MIN_SCORE, "trend": trend, "entry": close, "atr": av, "atr_pct": atr_pct, "adx": adx_v, "volume_ratio": vol_ratio, "ema20": e20[-1], "vwap": vw, "trendline": tl, "breakout_bull": breakout_bull, "breakout_bear": breakout_bear, "pullback_bull": pullback_bull, "pullback_bear": pullback_bear, "reason": f"trend={trend} | buy={buy}/8 sell={sell}/8 | TL={tl.get('rising')}/{tl.get('falling')} touches={tl.get('bull_touches')}/{tl.get('bear_touches')} | ADX={fmt(adx_v,2)} | VOL={fmt(vol_ratio,2)}x | ATR={fmt(atr_pct,3)}% | breakout={breakout_bull}/{breakout_bear} | pullback={pullback_bull}/{pullback_bear}"}


def circuit_ok():
    if risk["halted"]:
        return False, risk["reason"]
    if risk["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        return False, "Maximum consecutive losses reached"
    return True, ""


def calculate_size(symbol, price):
    i = get_instrument(symbol)
    target = MARGIN_USDT * LEVERAGE
    size = floor_step(target / (i["ctVal"] * price), i["lotSz"])
    if size < i["minSz"]:
        raise RuntimeError(f"Minimum contract size requires more than ${MARGIN_USDT} margin")
    return size, size * i["ctVal"] * price, i


def place_protection(symbol, side, entry, size, info, atr_value):
    sl_pct = max(MIN_SL_PCT, min(MAX_SL_PCT, atr_value / entry * 100 * SL_ATR_MULT))
    tp_pct = atr_value / entry * 100 * TP_ATR_MULT
    if side == "buy":
        sl = floor_step(entry * (1 - sl_pct / 100), info["tickSz"])
        tp = floor_step(entry * (1 + tp_pct / 100), info["tickSz"])
        close_side, pos_side = "sell", ("long" if position_mode == "long_short_mode" else "net")
    else:
        sl = ceil_step(entry * (1 + sl_pct / 100), info["tickSz"])
        tp = ceil_step(entry * (1 - tp_pct / 100), info["tickSz"])
        close_side, pos_side = "buy", ("short" if position_mode == "long_short_mode" else "net")
    payload = {"instId": symbol, "tdMode": TD_MODE, "side": close_side, "ordType": "oco", "reduceOnly": True, "closeFraction": "1", "tpTriggerPx": fmt(tp), "tpOrdPx": "-1", "tpTriggerPxType": "mark", "slTriggerPx": fmt(sl), "slOrdPx": "-1", "slTriggerPxType": "mark", "algoClOrdId": "p" + uuid.uuid4().hex[:28], "posSide": pos_side}
    result = private_request("POST", "/api/v5/trade/order-algo", payload=payload)
    row = (result.get("data") or [{}])[0]
    if row.get("sCode") not in (None, "", "0") or not row.get("algoId"):
        raise RuntimeError(f"Protection rejected: {row}")
    return {"sl": sl, "tp": tp, "algo_id": row["algoId"], "sl_pct": sl_pct, "tp_pct": tp_pct}


def emergency_close(symbol, pos):
    """Best-effort reduce-only market close after protection failure."""
    if not pos:
        return
    side = "sell" if D(pos.get("pos", "0")) > 0 else "buy"
    payload = {"instId": symbol, "tdMode": TD_MODE, "side": side, "ordType": "market", "sz": fmt(abs(D(pos.get("pos", "0")))), "reduceOnly": True, "clOrdId": "e" + uuid.uuid4().hex[:28]}
    if position_mode == "long_short_mode":
        payload["posSide"] = "long" if side == "sell" else "short"
    private_request("POST", "/api/v5/trade/order", payload=payload)


def open_trade(symbol, analysis, snap):
    if not AUTO_TRADE:
        return {"status": "BLOCKED", "reason": "AUTO_TRADE=false"}
    if not DEMO and not ALLOW_LIVE:
        return {"status": "BLOCKED", "reason": "Live trading disabled; set ALLOW_LIVE=true explicitly"}
    ok, why = circuit_ok()
    if not ok:
        return {"status": "BLOCKED", "reason": why}
    if time.time() - last_close_time.get(symbol, 0) < COOLDOWN_SECONDS:
        return {"status": "BLOCKED", "reason": "Symbol cooldown active"}
    if snap.get(symbol):
        return {"status": "BLOCKED", "reason": "Existing position"}
    # Revalidate on the same closed-candle basis immediately before order.
    fresh = detect_signal(symbol)
    if fresh.get("signal") != analysis.get("signal"):
        return {"status": "BLOCKED", "reason": f"Fresh signal changed to {fresh.get('signal')}"}
    with order_lock:
        snap = refresh_positions()
        if snap.get(symbol):
            return {"status": "BLOCKED", "reason": "Position appeared before order"}
        price = ticker(symbol)
        size, notional, info = calculate_size(symbol, price)
        if total_exposure(snap) + notional > MAX_TOTAL_NOTIONAL_USDT:
            return {"status": "BLOCKED", "reason": "Exposure cap reached"}
        side = "buy" if analysis["signal"] == "BUY" else "sell"
        payload = {"instId": symbol, "tdMode": TD_MODE, "side": side, "ordType": "market", "sz": fmt(size), "clOrdId": "b" + uuid.uuid4().hex[:28]}
        if position_mode == "long_short_mode":
            payload["posSide"] = "long" if side == "buy" else "short"
        result = private_request("POST", "/api/v5/trade/order", payload=payload)
        row = (result.get("data") or [{}])[0]
        if row.get("sCode") not in (None, "", "0"):
            raise RuntimeError(f"Entry rejected: {row}")
        time.sleep(2)
        snap = refresh_positions()
        pos = snap.get(symbol)
        if not pos:
            return {"status": "NOT_FILLED", "reason": "Order created no position"}
        entry = D(pos.get("avgPx") or price)
        try:
            protection = place_protection(symbol, side, entry, abs(D(pos.get("pos", "0"))), info, analysis["atr"])
        except Exception as protection_error:
            try:
                emergency_close(symbol, pos)
                return {"status": "EMERGENCY_CLOSED", "reason": f"Protection failed: {protection_error}"}
            except Exception as close_error:
                return {"status": "CRITICAL_UNPROTECTED", "reason": f"Protection failed: {protection_error}; emergency close failed: {close_error}"}
        with state_lock:
            state[symbol].update({"status": "OPEN", "entry_price": str(entry), "current_sl": str(protection["sl"]), "current_tp": str(protection["tp"]), "protection": "ACTIVE"})
        return {"status": "OPENED", "side": side.upper(), "entry": fmt(entry), "sl": fmt(protection["sl"]), "tp": fmt(protection["tp"]), "notional": fmt(notional, 2)}


def worker():
    global worker_started, worker_error
    try:
        if not DEMO and not ALLOW_LIVE:
            raise RuntimeError("Live mode requires ALLOW_LIVE=true")
        refresh_positions()
        worker_started = True
    except Exception as e:
        worker_error = str(e)
        return
    while True:
        started = time.time()
        try:
            snap = refresh_positions()
            for symbol in SYMBOLS:
                try:
                    if snap.get(symbol):
                        with state_lock:
                            state[symbol].update({"status": "POSITION_OPEN", "signal": "NONE"})
                        continue
                    a = detect_signal(symbol)
                    with state_lock:
                        state[symbol].update(a)
                        state[symbol]["last_checked"] = iso_now()
                        state[symbol]["status"] = "SIGNAL" if a.get("signal") != "NONE" else "WAITING"
                    print(f"[{iso_now()}] {symbol} {a.get('signal')} {a.get('score','-')}/8 | {a.get('reason')}", flush=True)
                    if a.get("signal") in ("BUY", "SELL") and a.get("score", 0) >= MIN_SCORE:
                        result = open_trade(symbol, a, snap)
                        with state_lock:
                            state[symbol]["trade_result"] = result
                            state[symbol]["status"] = result.get("status", "UNKNOWN")
                except Exception as e:
                    with state_lock:
                        state[symbol].update({"status": "ERROR", "error": str(e)})
                    print(f"[{iso_now()}] {symbol} ERROR {e}", flush=True)
        except Exception as e:
            worker_error = str(e)
            print(f"[{iso_now()}] WORKER ERROR {e}", flush=True)
        time.sleep(max(1, POLL_SECONDS - int(time.time() - started)))


HTML = """<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>OKX Trend Pullback Bot</title><style>body{font-family:Arial;background:#0d1117;color:#eee;padding:16px}table{border-collapse:collapse;width:100%;min-width:900px}th,td{padding:9px;border-bottom:1px solid #30363d;text-align:left}.wrap{overflow:auto}.buy{color:#3fb950}.sell{color:#f85149}.muted{color:#8b949e}.card{padding:12px;background:#161b22;border:1px solid #30363d;border-radius:8px;margin-bottom:12px}</style></head><body><h2>OKX Trend-Pullback Bot v1.0</h2><div id='top' class='card'>Loading...</div><div class='wrap'><table><thead><tr><th>Pair</th><th>Trend</th><th>Signal</th><th>Score</th><th>Entry</th><th>ATR%</th><th>ADX</th><th>Volume</th><th>Reason</th><th>Status</th></tr></thead><tbody id='rows'></tbody></table></div><script>async function r(){let s=await fetch('/api/status').then(x=>x.json());document.getElementById('top').innerHTML='Mode: <b>'+s.mode+'</b> | Auto: <b>'+s.auto_trade+'</b> | Margin: $'+s.margin+' | Leverage: '+s.leverage+'x | Exposure: $'+s.exposure+' / $'+s.max_exposure+' | Worker: '+s.worker;let h='';for(let [k,x] of Object.entries(s.symbols)){h+='<tr><td>'+k+'</td><td>'+((x.trend||'-'))+'</td><td class="'+(x.signal==='BUY'?'buy':x.signal==='SELL'?'sell':'muted')+'">'+(x.signal||'-')+'</td><td>'+(x.score??'-')+'/6 need '+(x.required_score??'-')+'</td><td>'+(x.entry||'-')+'</td><td>'+(x.atr_pct?Number(x.atr_pct).toFixed(3):'-')+'</td><td>'+(x.adx?Number(x.adx).toFixed(2):'-')+'</td><td>'+(x.volume_ratio?Number(x.volume_ratio).toFixed(2):'-')+'x</td><td class="muted">'+(x.reason||x.error||'-')+'</td><td>'+(x.status||'-')+'</td></tr>'}document.getElementById('rows').innerHTML=h}r();setInterval(r,5000)</script></body></html>"""


@app.get("/")
def home():
    return Response(HTML, mimetype="text/html")


@app.get("/health")
def health():
    return jsonify({"status": "ok", "worker_started": worker_started, "demo": DEMO, "auto_trade": AUTO_TRADE})


@app.get("/api/status")
def status():
    with state_lock:
        symbols = {k: dict(v) for k, v in state.items()}
    return jsonify({"mode": "DEMO" if DEMO else "LIVE", "auto_trade": AUTO_TRADE, "margin": str(MARGIN_USDT), "leverage": str(LEVERAGE), "exposure": str(total_exposure()), "max_exposure": str(MAX_TOTAL_NOTIONAL_USDT), "worker": "started" if worker_started else "stopped", "worker_error": worker_error, "symbols": symbols})


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
