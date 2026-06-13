import os
import time
import requests
from datetime import datetime, timezone, timedelta

# ─── CONFIG ───────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL   = 300  # 5 min

IST = timezone(timedelta(hours=5, minutes=30))

# ─── TELEGRAM ─────────────────────────────────────────────
def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text":    msg,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

# ─── STEP 1: KRAKEN API SE CANDLES ────────────────────────
def get_btc_candles(interval_min=5, limit=60):
    """
    Kraken free API — no restrictions, no API key needed
    interval: 5 = 5min, 15 = 15min
    """
    try:
        # Kraken interval in minutes
        url = (
            f"https://api.kraken.com/0/public/OHLC"
            f"?pair=XBTUSD&interval={interval_min}"
        )
        r    = requests.get(url, timeout=15)
        data = r.json()

        if data.get("error"):
            print(f"Kraken error: {data['error']}")
            return None, None, None, None

        # Kraken returns: [time, open, high, low, close, vwap, volume, count]
        candles = list(data["result"].values())[0]

        if not candles or len(candles) < 50:
            print(f"Kraken: Not enough candles — {len(candles)}")
            return None, None, None, None

        # Take last `limit` candles
        candles = candles[-limit:]

        closes  = [float(c[4]) for c in candles]
        volumes = [float(c[6]) for c in candles]
        highs   = [float(c[2]) for c in candles]
        lows    = [float(c[3]) for c in candles]

        print(f"Kraken ({interval_min}m): OK — {len(closes)} candles | Latest: ${closes[-1]:,.2f}")
        return closes, volumes, highs, lows

    except Exception as e:
        print(f"Kraken ({interval_min}m) error: {e}")
        return None, None, None, None

# ─── STEP 2: EMA ──────────────────────────────────────────
def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    k   = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 2)

# ─── STEP 3: TREND (15M) ──────────────────────────────────
def get_trend(closes_15m):
    ema30 = calculate_ema(closes_15m, 30)
    ema50 = calculate_ema(closes_15m, 50)
    if not ema30 or not ema50:
        return None, None, None
    trend = "BULLISH" if ema30 > ema50 else "BEARISH"
    return trend, ema30, ema50

# ─── STEP 4: ENTRY (5M) ───────────────────────────────────
def check_entry(closes_5m, trend):
    ema30 = calculate_ema(closes_5m, 30)
    ema50 = calculate_ema(closes_5m, 50)
    if not ema30 or not ema50:
        return False, None, None
    price = closes_5m[-1]
    if trend == "BULLISH":
        ok = (ema30 > ema50) and (price > ema30)
    else:
        ok = (ema30 < ema50) and (price < ema30)
    return ok, ema30, ema50

# ─── STEP 5: VOLUME ───────────────────────────────────────
def check_volume(volumes):
    if len(volumes) < 21:
        return False, 0, 0
    avg = sum(volumes[-21:-1]) / 20
    cur = volumes[-1]
    return cur > avg, round(cur, 4), round(avg, 4)

# ─── STEP 6: SESSION ──────────────────────────────────────
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

# ─── STEP 7: SL / TP ──────────────────────────────────────
def calculate_levels(trend, price, highs, lows):
    if trend == "BULLISH":
        sl      = round(min(lows[-10:]), 2)
        sl_dist = price - sl
        tp1     = round(price + sl_dist, 2)
        tp2     = round(price + sl_dist * 2, 2)
    else:
        sl      = round(max(highs[-10:]), 2)
        sl_dist = sl - price
        tp1     = round(price - sl_dist, 2)
        tp2     = round(price - sl_dist * 2, 2)

    if sl_dist <= 0:
        return None, None, None, None

    return sl, tp1, tp2, 2.0

# ─── STEP 8: SCORE ────────────────────────────────────────
def calculate_score(priority, entry_ok, vol_ok, retest):
    score   = 0
    reasons = []

    if priority == "HIGH":
        score += 3
        reasons.append("✅ High priority session (Killzone)")
    elif priority == "MEDIUM":
        score += 2
        reasons.append("⚡ Medium priority session")
    else:
        score += 1
        reasons.append("⚠️ Low priority (Asian)")

    score += 2
    reasons.append("✅ 15M Trend confirmed")

    if entry_ok:
        score += 2
        reasons.append("✅ 5M Entry conditions met")

    if vol_ok:
        score += 2
        reasons.append("✅ Volume above average")
    else:
        reasons.append("❌ Volume weak")

    if retest:
        score += 1
        reasons.append("✅ Price retesting EMA30")

    return score, reasons

# ─── STEP 9: FORMAT MESSAGE ───────────────────────────────
def format_signal(direction, price, sl, tp1, tp2, rr, score,
                  session, priority, ema30_5m, ema50_5m,
                  ema30_15m, ema50_15m, vol_status, reasons):

    now_ist = datetime.now(IST).strftime("%d %b %Y | %I:%M %p IST")
    emoji   = "🟢" if direction == "BUY" else "🔴"
    quality = "A+ Strong" if score >= 8 else "B Good" if score >= 6 else "C Weak"

    lines = [
        f"{emoji} <b>BTCUSD — {direction} SIGNAL</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"📍 <b>Entry  :</b> ${price:,.2f}",
        f"🛑 <b>SL     :</b> ${sl:,.2f}",
        f"🎯 <b>TP1    :</b> ${tp1:,.2f}",
        f"🎯 <b>TP2    :</b> ${tp2:,.2f}",
        f"⚖️ <b>R:R    :</b> 1:{rr}",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"📊 <b>Score  :</b> {score}/10",
        f"⭐ <b>Quality:</b> {quality}",
        f"📦 <b>Volume :</b> {vol_status}",
        f"🕐 <b>Session:</b> {session} [{priority}]",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"5M  → EMA30: {ema30_5m} | EMA50: {ema50_5m}",
        f"15M → EMA30: {ema30_15m} | EMA50: {ema50_15m}",
        "━━━━━━━━━━━━━━━━━━━━━",
        "<b>Reasons:</b>",
    ]
    for r in reasons:
        lines.append(f"  {r}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🕐 {now_ist}")
    lines.append("⚠️ <i>Risk 1% only. Confirm on chart.</i>")
    return "\n".join(lines)

# ─── MAIN LOOP ────────────────────────────────────────────
def main():
    print("🚀 BTCUSD EMA 30/50 Bot Started — Kraken API")
    send_telegram(
        "🚀 <b>BTCUSD Signal Bot Online!</b>\n"
        "📊 EMA 30/50 | 15M + 5M\n"
        "📡 Data: Kraken API\n"
        "⏰ All day signals (except 12AM-6AM IST)\n"
        "Checking every 5 minutes..."
    )

    last_signal    = {"time": None, "dir": None}
    session_trades = {}

    while True:
        try:
            session, priority = get_session()
            now     = datetime.now(IST)
            now_str = now.strftime("%H:%M")

            if session is None:
                print(f"[{now_str}] Dead zone — sleeping")
                time.sleep(CHECK_INTERVAL)
                continue

            # Fetch candles from Kraken
            closes_5m,  vols_5m,  highs_5m,  lows_5m  = get_btc_candles(5,  60)
            closes_15m, vols_15m, highs_15m, lows_15m  = get_btc_candles(15, 60)

            if closes_5m is None or closes_15m is None:
                print(f"[{now_str}] Candle fetch failed — retry in 60s")
                time.sleep(60)
                continue

            price = closes_5m[-1]

            # Trend
            trend, ema30_15m, ema50_15m = get_trend(closes_15m)
            if not trend:
                print(f"[{now_str}] Trend unclear — WAIT")
                time.sleep(CHECK_INTERVAL)
                continue

            direction = "BUY" if trend == "BULLISH" else "SELL"

            # Entry
            entry_ok, ema30_5m, ema50_5m = check_entry(closes_5m, trend)

            # Volume
            vol_ok, cur_vol, avg_vol = check_volume(vols_5m)
            vol_status = "Strong ✅" if vol_ok else "Weak ❌"

            # Retest
            retest = (abs(price - ema30_5m) / price * 100 < 0.2) if ema30_5m else False

            # Score
            score, reasons = calculate_score(priority, entry_ok, vol_ok, retest)

            print(f"[{now_str}] BTC: ${price:,.0f} | {direction} | Score: {score}/10 | Entry: {entry_ok} | Vol: {vol_ok} | {session}")

            # Min score
            min_score = 6 if priority in ["HIGH", "MEDIUM"] else 8
            if score < min_score or not entry_ok:
                print(f"[{now_str}] Score {score} < {min_score} or entry not met — WAIT")
                time.sleep(CHECK_INTERVAL)
                continue

            # Max 2 trades per session per day
            key = f"{session}_{now.strftime('%Y%m%d')}"
            if session_trades.get(key, 0) >= 2:
                print(f"[{now_str}] Max trades reached for {session}")
                time.sleep(CHECK_INTERVAL)
                continue

            # Duplicate check 30 min
            if (last_signal["time"] and
                (now - last_signal["time"]).seconds < 1800 and
                last_signal["dir"] == direction):
                print(f"[{now_str}] Duplicate — skipped")
                time.sleep(CHECK_INTERVAL)
                continue

            # SL/TP
            sl, tp1, tp2, rr = calculate_levels(trend, price, highs_5m, lows_5m)
            if not sl:
                print(f"[{now_str}] Invalid SL — skip")
                time.sleep(CHECK_INTERVAL)
                continue

            # Send signal
            msg = format_signal(
                direction, price, sl, tp1, tp2, rr, score,
                session, priority, ema30_5m, ema50_5m,
                ema30_15m, ema50_15m, vol_status, reasons
            )
            send_telegram(msg)

            last_signal["time"] = now
            last_signal["dir"]  = direction
            session_trades[key] = session_trades.get(key, 0) + 1
            print(f"[{now_str}] ✅ Signal sent: {direction} @ ${price:,.0f}")

        except Exception as e:
            print(f"Loop error: {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
