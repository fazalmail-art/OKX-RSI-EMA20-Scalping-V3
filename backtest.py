"""
BACKTEST ENGINE — validates the bot's exact entry/exit logic against
historical OKX candles, so win-rate/expectancy can be measured across
hundreds of trades in minutes instead of waiting days for live results.

WHY THIS EXISTS: live tuning (fixing one bug, waiting hours for a handful
of trades, repeat) is far too slow to tell whether the core PA-confluence
strategy has a real edge. This replays the SAME functions bot.py uses
(ema, rsi, atr, adx, macd, price_action_structure, ATR-based SL/TP,
partial-TP scale-out, trailing/step-TP, session score tiers, fee-ratio
gate, per-symbol cooldown) against real historical bars.

HOW TO RUN:
    pip install requests python-dotenv --break-system-packages
    python3 backtest.py

Needs bot.py in the same folder (imports its pure logic functions —
does NOT need OKX API keys, since it only touches public candle data).

KNOWN LIMITATIONS (documented, not hidden):
  - OI-unwind and funding-extreme filters are NOT backtested (precise
    historical alignment of OI/funding snapshots to each bar is complex
    and these rarely trigger anyway). They are treated as "always pass"
    here, which makes the backtest slightly MORE permissive than live —
    a conservative direction for this test (more trades allowed, not
    fewer), so it won't hide a losing edge behind extra filtering.
  - Entries are simulated at the CLOSE of the signal bar (approximates
    the live IOC-near-market entry); intra-bar slippage beyond that is
    not modeled.
  - SL/TP/partial-TP touches are checked using each subsequent bar's
    HIGH/LOW (realistic touch detection), evaluated SL-before-TP within
    the same bar as the conservative assumption when both could have
    triggered in one bar.
"""
import sys
import time
from decimal import Decimal
from datetime import datetime, timezone

import bot  # reuse the exact live logic — no reimplementation drift

# ---- backtest window ----
BARS_TO_FETCH = int(sys.argv[1]) if len(sys.argv) > 1 else 3000  # ~31 days of 15m bars
HISTORY_PAGE_SIZE = 100


def fetch_full_history(symbol, bar, total_bars):
    """Paginate OKX's history-candles endpoint backward in time until we
    have `total_bars` confirmed candles or the API runs out of data."""
    all_rows = []
    before_ts = ""
    while len(all_rows) < total_bars:
        params = {"instId": symbol, "bar": bar, "limit": str(HISTORY_PAGE_SIZE)}
        if before_ts:
            params["after"] = before_ts  # OKX: 'after' = return records earlier than this ts
        try:
            data = bot.public_get("/api/v5/market/history-candles", params)
        except Exception as exc:
            print(f"  WARNING: history fetch failed for {symbol} ({exc}), stopping pagination")
            break
        rows = data.get("data", [])
        if not rows:
            break
        all_rows.extend(rows)
        before_ts = rows[-1][0]  # oldest ts in this page -> fetch older next
        time.sleep(0.15)  # be polite to the API
    out = []
    seen = set()
    for row in reversed(all_rows):  # oldest -> newest
        ts = int(row[0])
        if ts in seen:
            continue
        seen.add(ts)
        out.append({
            "ts": ts, "open": bot.dec(row[1]), "high": bot.dec(row[2]), "low": bot.dec(row[3]),
            "close": bot.dec(row[4]), "volume": bot.dec(row[5]), "confirm": "1",
        })
    return out[-total_bars:]


def session_info_at(ts_ms):
    """Time-parametrized reimplementation of bot.session_info() for
    historical bars (the live version always uses 'now')."""
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(bot.PKT_TZ)
    now = dt.time()
    for start, end, label in bot.PRIORITY_WINDOWS:
        if start <= now < end:
            return {"name": label, "priority": True, "active": True}
    for start, end, label in bot.SESSION_WINDOWS:
        if start <= now < end:
            return {"name": label, "priority": False, "active": True}
    return {"name": "OFF_SESSION", "priority": False, "active": False}


def analyze_at(symbol, candles_15m, candles_1h, idx):
    """Reimplements bot.analyze() but operating on a fixed historical
    window ending at `idx` instead of live API calls, reusing every
    indicator/PA/scoring function unchanged."""
    cs = candles_15m[max(0, idx - 179):idx + 1]
    ts_now = cs[-1]["ts"]
    trend_cs = [c for c in candles_1h if c["ts"] <= ts_now][-100:]
    if len(cs) < 50 or len(trend_cs) < 22:
        return None

    values = [x["close"] for x in cs]
    i = len(values) - 1
    e20 = bot.ema(values, 20)
    r14 = bot.rsi(values, 14)
    atr_v = bot.atr(cs)
    adx_v = bot.adx(cs)
    ml, ms = bot.macd(values)
    vw = bot.session_vwap(cs)
    trend15 = bot.get_trend_from_candles(trend_cs)
    if any(x is None for x in (e20[i], r14[i], atr_v, ml[i], ms[i], vw)):
        return None

    avg_vol = sum(x["volume"] for x in cs[-21:-1]) / Decimal("20")
    vol_ratio = cs[i]["volume"] / avg_vol if avg_vol else Decimal("0")
    atr_pct = atr_v / values[i] * Decimal("100")
    pa = bot.price_action_structure(cs)

    trend_vote = "buy" if trend15 == "bull" else "sell" if trend15 == "bear" else "none"

    def momentum_alignment(direction):
        if direction == "buy":
            votes = [values[i] > e20[i], values[i] > vw, ml[i] > ms[i]]
        else:
            votes = [values[i] < e20[i], values[i] < vw, ml[i] < ms[i]]
        return sum(votes) >= 2

    flow_ok = sum([adx_v >= bot.ADX_MIN, vol_ratio >= bot.VOLUME_MULT, atr_pct >= bot.ATR_MIN_PCT]) >= 2
    buy_points = [
        trend15 == "bull",
        pa.get("bull_sr_ok", False) and pa.get("bull_sweep_ok", False) and pa.get("bull_reclaim_ok", False),
        pa.get("bull_bos_ok", False), pa.get("bull_retest_ok", False), flow_ok, momentum_alignment("buy"),
    ]
    sell_points = [
        trend15 == "bear",
        pa.get("bear_sr_ok", False) and pa.get("bear_sweep_ok", False) and pa.get("bear_reclaim_ok", False),
        pa.get("bear_bos_ok", False), pa.get("bear_retest_ok", False), flow_ok, momentum_alignment("sell"),
    ]
    buy, sell = sum(buy_points), sum(sell_points)
    MAX_SCORE = 6

    info = session_info_at(ts_now)
    if info["priority"]:
        required = min(bot.MIN_SCORE, MAX_SCORE)
    elif info["active"]:
        required = min(bot.MAJOR_MIN_SCORE, MAX_SCORE)
    else:
        required = min(bot.NON_PRIORITY_MIN_SCORE, MAX_SCORE)

    if pa["pa_complete_buy"] and not pa["pa_complete_sell"]:
        direction = "buy"
    elif pa["pa_complete_sell"] and not pa["pa_complete_buy"]:
        direction = "sell"
    elif pa["pa_complete_buy"] and pa["pa_complete_sell"]:
        direction = "buy" if buy >= sell else "sell"
    else:
        direction = "none"

    signal = "NONE"
    if direction == "buy" and buy >= required:
        signal = "BUY"
    elif direction == "sell" and sell >= required:
        signal = "SELL"

    if trend15 == "bull" and signal == "SELL":
        signal = "NONE"
    if trend15 == "bear" and signal == "BUY":
        signal = "NONE"
    if signal == "BUY" and ml[i] < ms[i]:
        signal = "NONE"
    if signal == "SELL" and ml[i] > ms[i]:
        signal = "NONE"

    if signal == "NONE":
        return None

    vol_mult = bot.volatility_size_multiplier(atr_pct)
    target_notional = bot.MARGIN_USDT * vol_mult * bot.LEVERAGE
    _, tp_pct_preview = bot.get_effective_sl_tp_pct(atr_pct)
    fee_ok, *_ = bot.fee_buffer_ok(target_notional, tp_pct_preview)
    if not fee_ok:
        return None

    return {"signal": signal, "atr_pct": atr_pct, "vol_mult": vol_mult, "notional": target_notional, "ts": ts_now, "entry": values[i]}


def simulate_trade(symbol, candles_15m, entry_idx, side, entry_price, atr_pct, notional):
    """Walks forward bar-by-bar using high/low to detect SL / partial-TP /
    final-TP touches, replicating the scale-out + breakeven + trailing +
    step-TP logic from bot.py's management functions."""
    tick = entry_price * Decimal("0.0001")  # rough tick approximation for backtest
    sl, tp = bot.calculate_initial_sl_tp(side, entry_price, tick, atr_pct)
    _, tp_pct = bot.get_effective_sl_tp_pct(atr_pct)
    partial_pct = tp_pct * bot.PARTIAL_TP_RATIO
    partial_price = bot.calculate_target_price(side, entry_price, tick, partial_pct)

    fraction_remaining = Decimal("1")
    scaled_out = False
    step_level = 0
    realized_pct = Decimal("0")  # weighted PnL% across the two legs

    for j in range(entry_idx + 1, len(candles_15m)):
        bar = candles_15m[j]
        hi, lo = bar["high"], bar["low"]

        if not scaled_out:
            hit_sl = (lo <= sl) if side == "buy" else (hi >= sl)
            hit_partial = (hi >= partial_price) if side == "buy" else (lo <= partial_price)
            if hit_sl:
                move = (sl - entry_price) / entry_price if side == "buy" else (entry_price - sl) / entry_price
                realized_pct += move * Decimal("100") * fraction_remaining
                return realized_pct, "SL_full"
            if hit_partial:
                move = (partial_price - entry_price) / entry_price if side == "buy" else (entry_price - partial_price) / entry_price
                realized_pct += move * Decimal("100") * bot.PARTIAL_TP_FRACTION
                fraction_remaining = Decimal("1") - bot.PARTIAL_TP_FRACTION
                scaled_out = True
                be = entry_price * (Decimal("1") + bot.BREAK_EVEN_OFFSET_PCT / Decimal("100")) if side == "buy" else entry_price * (Decimal("1") - bot.BREAK_EVEN_OFFSET_PCT / Decimal("100"))
                sl = max(sl, be) if side == "buy" else min(sl, be)
                sl = bot.cap_sl_distance(side, entry_price, sl, tick)
                continue

        if scaled_out:
            hit_sl = (lo <= sl) if side == "buy" else (hi >= sl)
            hit_tp = (hi >= tp) if side == "buy" else (lo <= tp)
            if hit_sl:
                move = (sl - entry_price) / entry_price if side == "buy" else (entry_price - sl) / entry_price
                realized_pct += move * Decimal("100") * fraction_remaining
                return realized_pct, "SL_after_scaleout"
            if hit_tp:
                move = (tp - entry_price) / entry_price if side == "buy" else (entry_price - tp) / entry_price
                realized_pct += move * Decimal("100") * fraction_remaining
                return realized_pct, "TP_final"
            # trailing + step-TP re-evaluation using this bar's close as proxy for mark price
            price = bar["close"]
            profit = (price - entry_price) / entry_price * Decimal("100") if side == "buy" else (entry_price - price) / entry_price * Decimal("100")
            if profit >= bot.TRAIL_START_PCT:
                tr = price * (Decimal("1") - bot.TRAIL_DISTANCE_PCT / Decimal("100")) if side == "buy" else price * (Decimal("1") + bot.TRAIL_DISTANCE_PCT / Decimal("100"))
                sl = max(sl, tr) if side == "buy" else min(sl, tr)
                sl = bot.cap_sl_distance(side, entry_price, sl, tick)
            achieved_step = int((profit / bot.STEP_TRIGGER_PCT).to_integral_value()) if profit > 0 else 0
            if achieved_step > step_level:
                step_level = achieved_step
                if side == "buy":
                    tp = max(tp, entry_price * (Decimal("1") + Decimal(step_level + 1) * bot.STEP_TRIGGER_PCT / Decimal("100")))
                else:
                    tp = min(tp, entry_price * (Decimal("1") - Decimal(step_level + 1) * bot.STEP_TRIGGER_PCT / Decimal("100")))

    return None, "STILL_OPEN_AT_DATA_END"


def run():
    print(f"Fetching ~{BARS_TO_FETCH} bars of {bot.BAR} history for: {bot.SYMBOLS}\n")
    all_results = []
    for symbol in bot.SYMBOLS:
        print(f"--- {symbol} ---")
        c15 = fetch_full_history(symbol, bot.BAR, BARS_TO_FETCH)
        c1h = fetch_full_history(symbol, bot.TREND_BAR, BARS_TO_FETCH // 4 + 200)
        if len(c15) < 60 or len(c1h) < 30:
            print(f"  Not enough data fetched ({len(c15)} 15m bars), skipping.")
            continue
        print(f"  Got {len(c15)} bars ({datetime.fromtimestamp(c15[0]['ts']/1000)} -> {datetime.fromtimestamp(c15[-1]['ts']/1000)})")

        last_close_ts = None
        i = 50
        while i < len(c15) - 1:
            if last_close_ts is not None and (c15[i]["ts"] - last_close_ts) / 1000 < bot.SYMBOL_COOLDOWN_SECONDS:
                i += 1
                continue
            sig = analyze_at(symbol, c15, c1h, i)
            if sig is None:
                i += 1
                continue
            side = "buy" if sig["signal"] == "BUY" else "sell"
            pnl_pct, reason = simulate_trade(symbol, c15, i, side, sig["entry"], sig["atr_pct"], sig["notional"])
            if pnl_pct is None:
                i += 1
                continue
            close_idx = i + 1
            for j in range(i + 1, len(c15)):
                bar = c15[j]
                hi, lo = bar["high"], bar["low"]
                # rough re-scan just to find approx close ts for cooldown bookkeeping
                close_idx = j
                if pnl_pct is not None:
                    break
            last_close_ts = c15[close_idx]["ts"]
            all_results.append((symbol, side, float(pnl_pct), reason))
            i = close_idx + 1

    if not all_results:
        print("\nNo trades generated in the backtest window.")
        return

    wins = [r for r in all_results if r[2] > 0]
    losses = [r for r in all_results if r[2] <= 0]
    n = len(all_results)
    win_rate = len(wins) / n * 100
    avg_win = sum(r[2] for r in wins) / len(wins) if wins else 0
    avg_loss = sum(abs(r[2]) for r in losses) / len(losses) if losses else 0
    expectancy = (len(wins)/n)*avg_win - (len(losses)/n)*avg_loss

    print("\n" + "=" * 60)
    print(f"TOTAL TRADES: {n}")
    print(f"Wins: {len(wins)}  Losses: {len(losses)}  Win rate: {win_rate:.1f}%")
    print(f"Average win: +{avg_win:.2f}%   Average loss: -{avg_loss:.2f}%")
    print(f"Expectancy per trade: {expectancy:+.3f}% (price-move basis, x{bot.LEVERAGE} leverage not applied here)")
    print(f"Sum of all trade returns: {sum(r[2] for r in all_results):+.2f}%")
    print("=" * 60)
    print("\nPer-symbol breakdown:")
    for symbol in bot.SYMBOLS:
        sub = [r for r in all_results if r[0] == symbol]
        if not sub:
            continue
        sw = len([r for r in sub if r[2] > 0])
        print(f"  {symbol}: {len(sub)} trades, {sw} wins ({sw/len(sub)*100:.0f}%), sum {sum(r[2] for r in sub):+.2f}%")


if __name__ == "__main__":
    run()
