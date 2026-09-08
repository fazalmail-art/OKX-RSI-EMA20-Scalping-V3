"""
OKX 4H Volume Spike Bot v1.0
--------------------------------
Strategy:
  1. Detect high relative volume on the last COMPLETED 4H candle.
  2. Mark key levels (Open / Close / Body Mid).
  3. On 15m timeframe, wait for clean break of those levels
     with volume confirmation + basic trend filter.
  4. Enter with ATR-based SL/TP + OCO protection.

Designed for OKX SWAP Demo first.
Not financial advice.
"""

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
# CONFIG
# ============================================================
BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "") or os.getenv("OKX_SECRET", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
DEMO = os.getenv("OKX_DEMO", "true").lower() == "true"
AUTO_TRADE = os.getenv("AUTO_TRADE", "false").lower() == "true"
ALLOW_LIVE = os.getenv("ALLOW_LIVE", "false").lower() == "true"

SYMBOLS = [s.strip() for s in os.getenv(
    "SYMBOLS",
    "BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP,DOGE-USDT-SWAP,XRP-USDT-SWAP"
).split(",") if s.strip()]

HTF_BAR = os.getenv("HTF_BAR", "4H")          # 4H volume spike
LTF_BAR = os.getenv("LTF_BAR", "15m")         # Entry timeframe
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))

MARGIN_USDT = Decimal(os.getenv("MARGIN_USDT", "10"))
LEVERAGE = Decimal(os.getenv("LEVERAGE", "5"))
TD_MODE = os.getenv("TD_MODE", "isolated")
MAX_TOTAL_NOTIONAL_USDT = Decimal(os.getenv("MAX_TOTAL_NOTIONAL_USDT", "300"))

# Volume spike settings
VOLUME_MULTIPLIER = Decimal(os.getenv("VOLUME_MULTIPLIER", "2.0"))
VOLUME_LOOKBACK = int(os.getenv("VOLUME_LOOKBACK", "20"))

# Signal filters
MIN_VOLUME_RATIO_LTF = Decimal(os.getenv("MIN_VOLUME_RATIO_LTF", "1.25"))
SL_ATR_MULT = Decimal(os.getenv("SL_ATR_MULT", "1.5"))
TP_ATR_MULT = Decimal(os.getenv("TP_ATR_MULT", "2.2"))
MIN_SL_PCT = Decimal(os.getenv("MIN_SL_PCT", "0.30"))
MAX_SL_PCT = Decimal(os.getenv("MAX_SL_PCT", "2.50"))

COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "900"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "4"))
DAILY_MAX_LOSS_USDT = Decimal(os.getenv("DAILY_MAX_LOSS_USDT", "30"))

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


# ============================================================
# HELPERS
# ============================================================
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
    rows = list(reversed(rows))
    out = []
    for r in rows:
        # Only confirmed candles (confirm flag = 1)
        if len(r) >= 9 and r[8] != "1":
            continue
        out.append({
            "ts": int(r[0]),
            "open": D(r[1]),
            "high": D(r[2]),
            "low": D(r[3]),
            "close": D(r[4]),
            "volume": D(r[5]),
        })
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


def get_instrument(symbol):
    rows = public_get("/api/v5/public/instruments", {"instType": "SWAP", "instId": symbol}).get("data", [])
    if not rows:
        raise RuntimeError(f"Instrument not found: {symbol}")
    r = rows[0]
    return {
        "ctVal": D(r["ctVal"]),
        "lotSz": D(r["lotSz"]),
        "minSz": D(r["minSz"]),
        "tickSz": D(r["tickSz"]),
        "state": r.get("state"),
    }


def ticker(symbol):
    rows = public_get("/api/v5/market/ticker", {"instId": symbol}).get("data", [])
    return D(rows[0]["last"]) if rows else None


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


# ============================================================
# STRATEGY: 4H VOLUME SPIKE + 15m BREAK
# ============================================================
def detect_4h_volume_spike(symbol):
    """
    Returns levels dict if last completed 4H candle has relative volume spike.
    """
    candles = get_candles(symbol, HTF_BAR, limit=VOLUME_LOOKBACK + 5)
    if len(candles) < VOLUME_LOOKBACK + 2:
        return None

    # Last completed candle
    candle = candles[-1]
    prev_volumes = [c["volume"] for c in candles[-(VOLUME_LOOKBACK + 1):-1]]
    avg_vol = sum(prev_volumes, D(0)) / D(len(prev_volumes)) if prev_volumes else D(0)

    if avg_vol <= 0:
        return None

    relative_volume = candle["volume"] / avg_vol
    if relative_volume < VOLUME_MULTIPLIER:
        return None

    body_mid = (candle["open"] + candle["close"]) / 2
    return {
        "symbol": symbol,
        "ts": candle["ts"],
        "open": candle["open"],
        "high": candle["high"],
        "low": candle["low"],
        "close": candle["close"],
        "body_mid": body_mid,
        "volume": candle["volume"],
        "avg_volume": avg_vol,
        "relative_volume": relative_volume,
        "is_bullish": candle["close"] > candle["open"],
    }


def detect_signal(symbol):
    """
    Main signal logic:
    - Need active 4H volume spike
    - On 15m: price breaks body_mid or close of that 4H candle
    - 15m volume also elevated
    """
    levels = detect_4h_volume_spike(symbol)
    if not levels:
        return {
            "signal": "NONE",
            "reason": "No 4H volume spike",
            "score": 0,
            "required_score": 1,
        }

    c = get_candles(symbol, LTF_BAR, 80)
    if len(c) < 30:
        return {"signal": "NONE", "reason": "Not enough 15m candles", "score": 0}

    last = c[-1]
    prev = c[-2]
    avg_vol_ltf = sum((x["volume"] for x in c[-21:-1]), D(0)) / 20
    vol_ratio = last["volume"] / avg_vol_ltf if avg_vol_ltf > 0 else D(0)

    av = atr(c)
    if not av:
        return {"signal": "NONE", "reason": "ATR unavailable", "score": 0}

    # Volume confirmation on entry bar
    vol_ok = vol_ratio >= MIN_VOLUME_RATIO_LTF

    signal = "NONE"
    broken_level = None
    reason_parts = [f"4H_RV={fmt(levels['relative_volume'], 2)}x"]

    mid = levels["body_mid"]
    close_lvl = levels["close"]
    open_lvl = levels["open"]

    if vol_ok:
        # LONG breaks
        if prev["close"] <= mid < last["close"]:
            signal = "BUY"
            broken_level = mid
            reason_parts.append("broke 4H body_mid")
        elif prev["close"] <= close_lvl < last["close"]:
            signal = "BUY"
            broken_level = close_lvl
            reason_parts.append("broke 4H close")
        elif prev["close"] <= open_lvl < last["close"] and levels["is_bullish"]:
            signal = "BUY"
            broken_level = open_lvl
            reason_parts.append("broke 4H open (bullish candle)")

        # SHORT breaks
        elif prev["close"] >= mid > last["close"]:
            signal = "SELL"
            broken_level = mid
            reason_parts.append("broke 4H body_mid")
        elif prev["close"] >= close_lvl > last["close"]:
            signal = "SELL"
            broken_level = close_lvl
            reason_parts.append("broke 4H close")
        elif prev["close"] >= open_lvl > last["close"] and not levels["is_bullish"]:
            signal = "SELL"
            broken_level = open_lvl
            reason_parts.append("broke 4H open (bearish candle)")

    if signal == "NONE":
        reason_parts.append(f"LTF_VOL={fmt(vol_ratio, 2)}x (need >{MIN_VOLUME_RATIO_LTF})")
        return {
            "signal": "NONE",
            "reason": " | ".join(reason_parts),
            "score": 0,
            "required_score": 1,
            "4h_rv": float(levels["relative_volume"]),
            "levels": {k: float(v) if isinstance(v, Decimal) else v for k, v in levels.items() if k != "symbol"},
        }

    return {
        "signal": signal,
        "score": 1,
        "required_score": 1,
        "entry": last["close"],
        "atr": av,
        "atr_pct": av / last["close"] * 100,
        "volume_ratio": vol_ratio,
        "broken_level": broken_level,
        "4h_rv": float(levels["relative_volume"]),
        "levels": {k: float(v) if isinstance(v, Decimal) else v for k, v in levels.items() if k != "symbol"},
        "reason": " | ".join(reason_parts) + f" | LTF_VOL={fmt(vol_ratio, 2)}x",
    }


# ============================================================
# RISK & ORDER
# ============================================================
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
        close_side = "sell"
        pos_side = "long" if position_mode == "long_short_mode" else "net"
    else:
        sl = ceil_step(entry * (1 + sl_pct / 100), info["tickSz"])
        tp = ceil_step(entry * (1 - tp_pct / 100), info["tickSz"])
        close_side = "buy"
        pos_side = "short" if position_mode == "long_short_mode" else "net"

    payload = {
        "instId": symbol,
        "tdMode": TD_MODE,
        "side": close_side,
        "ordType": "oco",
        "reduceOnly": True,
        "closeFraction": "1",
        "tpTriggerPx": fmt(tp),
        "tpOrdPx": "-1",
        "tpTriggerPxType": "mark",
        "slTriggerPx": fmt(sl),
        "slOrdPx": "-1",
        "slTriggerPxType": "mark",
        "algoClOrdId": "p" + uuid.uuid4().hex[:28],
        "posSide": pos_side,
    }
    result = private_request("POST", "/api/v5/trade/order-algo", payload=payload)
    row = (result.get("data") or [{}])[0]
    if row.get("sCode") not in (None, "", "0") or not row.get("algoId"):
        raise RuntimeError(f"Protection rejected: {row}")
    return {"sl": sl, "tp": tp, "algo_id": row["algoId"], "sl_pct": sl_pct, "tp_pct": tp_pct}


def emergency_close(symbol, pos):
    if not pos:
        return
    side = "sell" if D(pos.get("pos", "0")) > 0 else "buy"
    payload = {
        "instId": symbol,
        "tdMode": TD_MODE,
        "side": side,
        "ordType": "market",
        "sz": fmt(abs(D(pos.get("pos", "0")))),
        "reduceOnly": True,
        "clOrdId": "e" + uuid.uuid4().hex[:28],
    }
    if position_mode == "long_short_mode":
        payload["posSide"] = "long" if side == "sell" else "short"
    private_request("POST", "/api/v5/trade/order", payload=payload)


def open_trade(symbol, analysis, snap):
    if not AUTO_TRADE:
        return {"status": "BLOCKED", "reason": "AUTO_TRADE=false"}
    if not DEMO and not ALLOW_LIVE:
        return {"status": "BLOCKED", "reason": "Live trading disabled; set ALLOW_LIVE=true"}
    ok, why = circuit_ok()
    if not ok:
        return {"status": "BLOCKED", "reason": why}
    if time.time() - last_close_time.get(symbol, 0) < COOLDOWN_SECONDS:
        return {"status": "BLOCKED", "reason": "Symbol cooldown active"}
    if snap.get(symbol):
        return {"status": "BLOCKED", "reason": "Existing position"}

    # Revalidate
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
        payload = {
            "instId": symbol,
            "tdMode": TD_MODE,
            "side": side,
            "ordType": "market",
            "sz": fmt(size),
            "clOrdId": "b" + uuid.uuid4().hex[:28],
        }
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
                return {
                    "status": "CRITICAL_UNPROTECTED",
                    "reason": f"Protection failed: {protection_error}; emergency close failed: {close_error}",
                }

        with state_lock:
            state[symbol].update({
                "status": "OPEN",
                "entry_price": str(entry),
                "current_sl": str(protection["sl"]),
                "current_tp": str(protection["tp"]),
                "protection": "ACTIVE",
            })
        return {
            "status": "OPENED",
            "side": side.upper(),
            "entry": fmt(entry),
            "sl": fmt(protection["sl"]),
            "tp": fmt(protection["tp"]),
            "notional": fmt(notional, 2),
        }


# ============================================================
# WORKER
# ============================================================
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

                    print(f"[{iso_now()}] {symbol} {a.get('signal')} | {a.get('reason')}", flush=True)

                    if a.get("signal") in ("BUY", "SELL"):
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


# ============================================================
# WEB DASHBOARD
# ============================================================
HTML = """<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OKX 4H Volume Spike Bot</title>
<style>
body{font-family:Arial,sans-serif;background:#0d1117;color:#eee;padding:16px;margin:0}
h2{margin-top:0}
table{border-collapse:collapse;width:100%;min-width:1000px}
th,td{padding:9px;border-bottom:1px solid #30363d;text-align:left;font-size:14px}
.wrap{overflow:auto}
.buy{color:#3fb950}.sell{color:#f85149}.muted{color:#8b949e}
.card{padding:14px;background:#161b22;border:1px solid #30363d;border-radius:8px;margin-bottom:14px}
.badge{display:inline-block;padding:3px 8px;border-radius:12px;font-size:12px;background:#21262d}
</style>
</head>
<body>
<h2>OKX 4H Volume Spike Bot <span class="badge">DEMO • SWAP</span></h2>
<div id="top" class="card">Loading...</div>
<div class="wrap">
<table>
<thead>
<tr>
<th>Pair</th><th>Signal</th><th>4H RV</th><th>Entry</th><th>ATR%</th>
<th>LTF Vol</th><th>Reason</th><th>Status</th>
</tr>
</thead>
<tbody id="rows"></tbody>
</table>
</div>
<script>
async function r(){
  let s = await fetch('/api/status').then(x=>x.json());
  document.getElementById('top').innerHTML =
    'Mode: <b>'+s.mode+'</b> | Auto: <b>'+s.auto_trade+'</b> | ' +
    'Margin: $'+s.margin+' | Leverage: '+s.leverage+'x | ' +
    'Exposure: $'+s.exposure+' / $'+s.max_exposure+' | Worker: '+s.worker +
    (s.worker_error ? ' <span class="sell">('+s.worker_error+')</span>' : '');
  let h = '';
  for(let [k,x] of Object.entries(s.symbols)){
    h += '<tr>' +
      '<td>'+k+'</td>' +
      '<td class="'+(x.signal==='BUY'?'buy':x.signal==='SELL'?'sell':'muted')+'">'+(x.signal||'-')+'</td>' +
      '<td>'+(x['4h_rv']?Number(x['4h_rv']).toFixed(2)+'x':'-')+'</td>' +
      '<td>'+(x.entry||'-')+'</td>' +
      '<td>'+(x.atr_pct?Number(x.atr_pct).toFixed(3):'-')+'</td>' +
      '<td>'+(x.volume_ratio?Number(x.volume_ratio).toFixed(2)+'x':'-')+'</td>' +
      '<td class="muted">'+(x.reason||x.error||'-')+'</td>' +
      '<td>'+(x.status||'-')+'</td>' +
      '</tr>';
  }
  document.getElementById('rows').innerHTML = h;
}
r();
setInterval(r, 5000);
</script>
</body>
</html>"""


@app.get("/")
def home():
    return Response(HTML, mimetype="text/html")


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "worker_started": worker_started,
        "demo": DEMO,
        "auto_trade": AUTO_TRADE,
    })


@app.get("/api/status")
def status():
    with state_lock:
        symbols = {k: dict(v) for k, v in state.items()}
    return jsonify({
        "mode": "DEMO" if DEMO else "LIVE",
        "auto_trade": AUTO_TRADE,
        "margin": str(MARGIN_USDT),
        "leverage": str(LEVERAGE),
        "exposure": str(total_exposure()),
        "max_exposure": str(MAX_TOTAL_NOTIONAL_USDT),
        "worker": "started" if worker_started else "stopped",
        "worker_error": worker_error,
        "symbols": symbols,
    })


# ============================================================
# START
# ============================================================
if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    port = int(os.getenv("PORT", "8080"))
    print(f"Starting OKX 4H Volume Spike Bot on 0.0.0.0:{port}")
    print(f"Symbols: {SYMBOLS}")
    print(f"HTF={HTF_BAR} | LTF={LTF_BAR} | Volume Mult={VOLUME_MULTIPLIER}x")
    app.run(host="0.0.0.0", port=port)
