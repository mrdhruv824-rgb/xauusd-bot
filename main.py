import os
import time
import requests
from datetime import datetime, timezone, timedelta

# ─── CONFIG ───────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL   = 300  # 5 min

IST = timezone(timedelta(hours=5, minutes=30))

# ─── PROTECTION RULES ─────────────────────────────────────
consecutive_losses = 0
session_trades     = {}
last_signal        = {"time": None, "dir": None}
bot_paused         = False
bot_pause_until    = None

# ─── TELEGRAM ─────────────────────────────────────────────
def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       msg,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

# ─── STEP 1: KRAKEN CANDLES ───────────────────────────────
def get_candles(interval_min, limit=210):
    """
    Kraken free API
    interval: 5, 15, 60 (minutes)
    Returns: closes, volumes, highs, lows
    """
    try:
        url = f"https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval={interval_min}"
        r   = requests.get(url, timeout=15)
        d   = r.json()

        if d.get("error"):
            print(f"Kraken {interval_min}m error: {d['error']}")
            return None, None, None, None

        candles = list(d["result"].values())[0]
        if len(candles) < 210:
            print(f"Kraken {interval_min}m: only {len(candles)} candles")

        candles = candles[-limit:]

        closes  = [float(c[4]) for c in candles]
        volumes = [float(c[6]) for c in candles]
        highs   = [float(c[2]) for c in candles]
        lows    = [float(c[3]) for c in candles]

        print(f"Kraken {interval_min}m: {len(closes)} candles | Latest: ${closes[-1]:,.2f}")
        return closes, volumes, highs, lows

    except Exception as e:
        print(f"Kraken {interval_min}m fetch error: {e}")
        return None, None, None, None

# ─── STEP 2: EMA ──────────────────────────────────────────
def ema(prices, period):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    e = sum(prices[:period]) / period
    for p in prices[period:]:
        e = p * k + e * (1 - k)
    return round(e, 2)

# ─── STEP 3: RSI ──────────────────────────────────────────
def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-period + i] - closes[-period + i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    rs  = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

# ─── STEP 4: SIDEWAYS CHECK ───────────────────────────────
def is_sideways(closes, lookback=20, threshold=0.004):
    recent = closes[-lookback:]
    rng    = (max(recent) - min(recent)) / min(recent)
    return rng < threshold

# ─── STEP 5: SESSION ──────────────────────────────────────
def get_session():
    now   = datetime.now(IST)
    total = now.hour * 60 + now.minute

    # Dead zone 12AM - 6AM IST
    if 0 <= now.hour < 6:
        return None, None

    if 12*60+30 <= total <= 15*60+30:
        return "London Killzone", "HIGH"
    if 18*60+30 <= total <= 21*60+30:
        return "New York Killzone", "HIGH"
    if 10*60 <= total < 12*60+30:
        return "London Open", "MEDIUM"
    if 16*60 <= total < 18*60+30:
        return "NY Open", "MEDIUM"

    return "Asian Session", "LOW"

# ─── STEP 6: ENGULFING CANDLE ─────────────────────────────
def is_engulfing(closes, opens=None):
    """
    Simple bullish/bearish engulfing check
    Using close prices as proxy
    """
    if len(closes) < 3:
        return None

    prev_close = closes[-2]
    prev_open  = closes[-3]
    curr_close = closes[-1]
    curr_open  = closes[-2]

    prev_body = abs(prev_close - prev_open)
    curr_body = abs(curr_close - curr_open)

    # Bullish engulfing
    if curr_close > curr_open and curr_body > prev_body and curr_close > prev_close:
        return "BULLISH"

    # Bearish engulfing
    if curr_close < curr_open and curr_body > prev_body and curr_close < prev_close:
        return "BEARISH"

    return None

# ─── MAIN ANALYSIS ────────────────────────────────────────
def analyze():
    # Fetch all timeframes
    closes_1h,  vols_1h,  highs_1h,  lows_1h  = get_candles(60,  210)
    closes_15m, vols_15m, highs_15m, lows_15m  = get_candles(15,  210)
    closes_5m,  vols_5m,  highs_5m,  lows_5m   = get_candles(5,   60)

    if closes_1h is None or closes_15m is None or closes_5m is None:
        return None

    price = closes_5m[-1]

    # ── STEP 2: Master Trend (1H 200 EMA) ──
    ema200_1h = ema(closes_1h, 200)
    if not ema200_1h:
        print("1H 200 EMA: Not enough data")
        return None

    dist_pct = abs(price - ema200_1h) / price * 100
    if dist_pct < 0.5:
        print(f"Price at 200 EMA — WAIT ({dist_pct:.2f}%)")
        return None

    if price > ema200_1h:
        master_trend = "BULLISH"
    else:
        master_trend = "BEARISH"

    # ── STEP 3: EMA Stack (15M) ──
    ema20_15m  = ema(closes_15m, 20)
    ema50_15m  = ema(closes_15m, 50)
    ema200_15m = ema(closes_15m, 200)

    if not all([ema20_15m, ema50_15m, ema200_15m]):
        print("15M EMAs: Not enough data")
        return None

    if master_trend == "BULLISH":
        stack_ok = ema20_15m > ema50_15m > ema200_15m
    else:
        stack_ok = ema20_15m < ema50_15m < ema200_15m

    if not stack_ok:
        print(f"15M EMA Stack not aligned — WAIT")
        return None

    direction = "BUY" if master_trend == "BULLISH" else "SELL"

    # ── STEP 4: RSI Filter (5M) ──
    rsi_val = rsi(closes_5m, 14)
    if rsi_val is None:
        return None

    if direction == "BUY":
        rsi_ok = 45 <= rsi_val <= 65
    else:
        rsi_ok = 35 <= rsi_val <= 55

    # ── STEP 5: Pullback to EMA (5M) ──
    ema20_5m = ema(closes_5m, 20)
    ema50_5m = ema(closes_5m, 50)

    if not ema20_5m or not ema50_5m:
        return None

    dist_ema20 = abs(price - ema20_5m) / price * 100
    dist_ema50 = abs(price - ema50_5m) / price * 100
    pullback_ok = dist_ema20 < 0.3 or dist_ema50 < 0.5

    # ── Candle direction confirm ──
    if direction == "BUY":
        candle_ok = closes_5m[-1] > closes_5m[-2]
    else:
        candle_ok = closes_5m[-1] < closes_5m[-2]

    # ── STEP 6: Volume (1.5x average) ──
    if len(vols_5m) >= 21:
        avg_vol = sum(vols_5m[-21:-1]) / 20
        cur_vol = vols_5m[-1]
        vol_ok  = cur_vol > avg_vol * 1.5
        vol_status = f"Strong ✅ ({cur_vol:.2f} vs avg {avg_vol:.2f})" if vol_ok else f"Weak ❌"
    else:
        vol_ok, vol_status = False, "Insufficient data"

    # ── Sideways check ──
    if is_sideways(closes_5m):
        print("Sideways market — WAIT")
        return None

    # ── SCORE SYSTEM ──
    score   = 0
    reasons = []

    # 1H trend (2 pts)
    score += 2
    reasons.append(f"✅ 1H Master Trend: {master_trend} (200 EMA: {ema200_1h:,.2f})")

    # 15M EMA Stack (2 pts)
    score += 2
    reasons.append(f"✅ 15M EMA Stack aligned: EMA20({ema20_15m:,.0f}) {'>' if direction=='BUY' else '<'} EMA50({ema50_15m:,.0f}) {'>' if direction=='BUY' else '<'} EMA200({ema200_15m:,.0f})")

    # RSI (2 pts)
    if rsi_ok:
        score += 2
        reasons.append(f"✅ RSI: {rsi_val} (valid zone)")
    else:
        reasons.append(f"❌ RSI: {rsi_val} (out of zone)")

    # Pullback + Candle (2 pts)
    if pullback_ok and candle_ok:
        score += 2
        reasons.append(f"✅ Pullback to EMA + candle confirmed")
    elif pullback_ok:
        score += 1
        reasons.append(f"⚡ Pullback to EMA (candle weak)")

    # Volume (1 pt)
    if vol_ok:
        score += 1
        reasons.append(f"✅ Volume 1.5x above average")
    else:
        reasons.append(f"❌ Volume weak")

    # Session (1 pt)
    session, priority = get_session()
    if priority == "HIGH":
        score += 1
        reasons.append(f"✅ {session} — High priority")
    elif priority == "MEDIUM":
        score += 0.5
        reasons.append(f"⚡ {session} — Medium priority")

    print(f"Score: {score}/10 | Direction: {direction} | RSI: {rsi_val} | Vol: {vol_ok} | Pullback: {pullback_ok}")

    # ── Min score ──
    if score < 7:
        print(f"Score {score} too low — WAIT")
        return None

    # ── SL / TP ──
    if direction == "BUY":
        sl      = round(min(lows_5m[-10:]), 2)
        sl_dist = price - sl
        tp1     = round(price + sl_dist, 2)
        tp2     = round(price + sl_dist * 2, 2)
        tp3     = round(price + sl_dist * 3, 2)
    else:
        sl      = round(max(highs_5m[-10:]), 2)
        sl_dist = sl - price
        tp1     = round(price - sl_dist, 2)
        tp2     = round(price - sl_dist * 2, 2)
        tp3     = round(price - sl_dist * 3, 2)

    if sl_dist <= 0:
        print("Invalid SL — skip")
        return None

    # Quality grade
    if score >= 9:
        quality  = "A+ — Strong Signal 🔥"
        risk_pct = "1%"
    elif score >= 7:
        quality  = "A — Good Signal ✅"
        risk_pct = "0.5%"
    else:
        quality  = "B — Weak"
        risk_pct = "Skip"

    return {
        "direction":   direction,
        "price":       price,
        "sl":          sl,
        "tp1":         tp1,
        "tp2":         tp2,
        "tp3":         tp3,
        "score":       score,
        "quality":     quality,
        "risk_pct":    risk_pct,
        "rsi":         rsi_val,
        "vol_status":  vol_status,
        "session":     session,
        "priority":    priority,
        "ema200_1h":   ema200_1h,
        "ema20_15m":   ema20_15m,
        "ema50_15m":   ema50_15m,
        "ema200_15m":  ema200_15m,
        "ema20_5m":    ema20_5m,
        "ema50_5m":    ema50_5m,
        "master_trend":master_trend,
        "reasons":     reasons,
    }

# ─── FORMAT MESSAGE ───────────────────────────────────────
def format_signal(sig):
    now_ist = datetime.now(IST).strftime("%d %b %Y | %I:%M %p IST")
    emoji   = "🟢" if sig["direction"] == "BUY" else "🔴"

    lines = [
        f"{emoji} <b>BTCUSD — {sig['direction']} SIGNAL</b>",
        f"📊 <b>Strategy: Paul Tudor Jones Method</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📍 <b>Entry   :</b> ${sig['price']:,.2f}",
        f"🛑 <b>SL      :</b> ${sig['sl']:,.2f}",
        f"🎯 <b>TP1(1:1):</b> ${sig['tp1']:,.2f}",
        f"🎯 <b>TP2(1:2):</b> ${sig['tp2']:,.2f}",
        f"🎯 <b>TP3(1:3):</b> ${sig['tp3']:,.2f}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"⭐ <b>Quality :</b> {sig['quality']}",
        f"📊 <b>Score   :</b> {sig['score']}/10",
        f"💰 <b>Risk    :</b> {sig['risk_pct']} of account",
        f"📈 <b>RSI     :</b> {sig['rsi']}",
        f"📦 <b>Volume  :</b> {sig['vol_status']}",
        f"🕐 <b>Session :</b> {sig['session']} [{sig['priority']}]",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📉 <b>1H  200 EMA:</b> ${sig['ema200_1h']:,.2f}",
        f"📉 <b>15M 200 EMA:</b> ${sig['ema200_15m']:,.2f}",
        f"📉 <b>15M  50 EMA:</b> ${sig['ema50_15m']:,.2f}",
        f"📉 <b>15M  20 EMA:</b> ${sig['ema20_15m']:,.2f}",
        f"📉 <b>5M   20 EMA:</b> ${sig['ema20_5m']:,.2f}",
        f"📉 <b>5M   50 EMA:</b> ${sig['ema50_5m']:,.2f}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🌊 <b>Master Trend:</b> {sig['master_trend']} (1H)",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "<b>📋 Reasons:</b>",
    ]
    for r in sig["reasons"]:
        lines.append(f"  {r}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("<b>📌 Trade Plan:</b>")
    lines.append(f"  • TP1 hit → Close 50%, Move SL to BE")
    lines.append(f"  • TP2 hit → Close 30%")
    lines.append(f"  • TP3 hit → Close remaining 20%")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🕐 {now_ist}")
    lines.append(f"⚠️ <i>Risk {sig['risk_pct']} only. Always confirm on chart.</i>")
    return "\n".join(lines)

# ─── MAIN LOOP ────────────────────────────────────────────
def main():
    global consecutive_losses, bot_paused, bot_pause_until

    print("🚀 Paul Tudor Jones BTCUSD Bot Started")
    send_telegram(
        "🚀 <b>BTCUSD Signal Bot Online!</b>\n"
        "📊 <b>Strategy: Paul Tudor Jones Method</b>\n"
        "📡 EMA 20/50/200 | RSI | Volume | Pullback\n"
        "⏰ Signals all day (except 12AM-6AM IST)\n"
        "🔔 Checking every 5 minutes..."
    )

    while True:
        try:
            now     = datetime.now(IST)
            now_str = now.strftime("%H:%M")

            # ── Bot paused check ──
            if bot_paused and bot_pause_until:
                if now < bot_pause_until:
                    remaining = int((bot_pause_until - now).seconds / 60)
                    print(f"[{now_str}] Bot paused — {remaining} min remaining")
                    time.sleep(CHECK_INTERVAL)
                    continue
                else:
                    bot_paused         = False
                    bot_pause_until    = None
                    consecutive_losses = 0
                    send_telegram("✅ <b>Bot resumed after pause.</b>\nWatching for setups...")

            # ── Session check ──
            session, priority = get_session()
            if session is None:
                print(f"[{now_str}] Dead zone — sleeping")
                time.sleep(CHECK_INTERVAL)
                continue

            # ── Max trades check ──
            key = f"{session}_{now.strftime('%Y%m%d')}"
            if session_trades.get(key, 0) >= 2:
                print(f"[{now_str}] Max 2 trades reached for {session}")
                time.sleep(CHECK_INTERVAL)
                continue

            # ── Analyze ──
            print(f"\n[{now_str}] Analyzing BTCUSD...")
            signal = analyze()

            if not signal:
                print(f"[{now_str}] No valid setup — WAIT")
                time.sleep(CHECK_INTERVAL)
                continue

            # ── Duplicate check (30 min) ──
            if (last_signal["time"] and
                (now - last_signal["time"]).seconds < 1800 and
                last_signal["dir"] == signal["direction"]):
                print(f"[{now_str}] Duplicate signal — skipped")
                time.sleep(CHECK_INTERVAL)
                continue

            # ── Send signal ──
            msg = format_signal(signal)
            send_telegram(msg)

            last_signal["time"] = now
            last_signal["dir"]  = signal["direction"]
            session_trades[key] = session_trades.get(key, 0) + 1

            print(f"[{now_str}] ✅ Signal sent: {signal['direction']} @ ${signal['price']:,.0f} | Score: {signal['score']}/10")

        except Exception as e:
            print(f"Loop error: {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
