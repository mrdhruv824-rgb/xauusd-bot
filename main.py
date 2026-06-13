import os
import time
import requests
from datetime import datetime, timezone, timedelta

# ─── CONFIG ───────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL   = 300  # 5 min

IST = timezone(timedelta(hours=5, minutes=30))

# ─── STEP 1: BINANCE SE DATA LANA ─────────────────────────
def get_btc_candles(interval="5m", limit=60):
    """
    Binance free API se BTCUSDT candles lo
    interval = "5m" ya "15m"
    limit    = kitni candles chahiye
    """
    try:
        url = (
            f"https://api.binance.com/api/v3/klines"
            f"?symbol=BTCUSDT&interval={interval}&limit={limit}"
        )
        r = requests.get(url, timeout=10)
        data = r.json()

        closes  = [float(c[4]) for c in data]   # Close price
        volumes = [float(c[5]) for c in data]   # Volume
        highs   = [float(c[2]) for c in data]   # High
        lows    = [float(c[3]) for c in data]   # Low

        print(f"[Binance] {interval} candles fetched: {len(closes)}")
        return closes, volumes, highs, lows

    except Exception as e:
        print(f"Binance error ({interval}): {e}")
        return None, None, None, None

# ─── STEP 2: EMA CALCULATION ──────────────────────────────
def calculate_ema(prices, period):
    """
    EMA formula:
    EMA = Price * (2/period+1) + Previous EMA * (1 - 2/period+1)
    """
    if len(prices) < period:
        return None
    k   = 2 / (period + 1)
    ema = sum(prices[:period]) / period   # First EMA = simple average
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 2)

# ─── STEP 3: TREND CHECK (15M) ────────────────────────────
def get_trend(closes_15m):
    """
    15M EMA30 vs EMA50:
    EMA30 > EMA50 = BULLISH
    EMA30 < EMA50 = BEARISH
    """
    ema30 = calculate_ema(closes_15m, 30)
    ema50 = calculate_ema(closes_15m, 50)

    if not ema30 or not ema50:
        return None, None, None

    if ema30 > ema50:
        trend = "BULLISH"
    elif ema30 < ema50:
        trend = "BEARISH"
    else:
        trend = None

    return trend, round(ema30, 2), round(ema50, 2)

# ─── STEP 4: ENTRY CHECK (5M) ─────────────────────────────
def check_entry(closes_5m, trend):
    """
    5M conditions:
    BUY:  EMA30 > EMA50 AND price > EMA30
    SELL: EMA30 < EMA50 AND price < EMA30
    """
    ema30 = calculate_ema(closes_5m, 30)
    ema50 = calculate_ema(closes_5m, 50)

    if not ema30 or not ema50:
        return False, None, None

    price = closes_5m[-1]   # Latest close price

    if trend == "BULLISH":
        entry_ok = (ema30 > ema50) and (price > ema30)
    elif trend == "BEARISH":
        entry_ok = (ema30 < ema50) and (price < ema30)
    else:
        entry_ok = False

    return entry_ok, round(ema30, 2), round(ema50, 2)

# ─── STEP 5: VOLUME CHECK ─────────────────────────────────
def check_volume(volumes):
    """
    Current volume > Average of last 20 candles?
    """
    if len(volumes) < 21:
        return False, 0, 0
    avg_vol     = sum(volumes[-21:-1]) / 20
    current_vol = volumes[-1]
    return current_vol > avg_vol, round(current_vol, 2), round(avg_vol, 2)

# ─── STEP 6: SESSION (Dead hours avoid) ───────────────────
def get_session():
    """
    Sirf dead hours (12 AM - 6 AM IST) mein signal nahi
    Baaki poore din signal milega
    Priority bhi assign karo:
    """
    now   = datetime.now(IST)
    hour  = now.hour
    total = now.hour * 60 + now.minute

    # Dead zone — no signal
    if 0 <= hour < 6:
        return None, None

    # London Killzone — High priority
    if 12*60+30 <= total <= 15*60+30:
        return "London Killzone", "HIGH"

    # NY Killzone — High priority
    if 18*60+30 <= total <= 21*60+30:
        return "New York Killzone", "HIGH"

    # London Open — Medium
    if 10*60 <= total < 12*60+30:
        return "London Open", "MEDIUM"

    # NY Open — Medium
    if 16*60 <= total < 18*60+30:
        return "NY Open", "MEDIUM"

    # Asian — Low priority
    return "Asian Session", "LOW"

# ─── STEP 7: SL / TP CALCULATION ──────────────────────────
def calculate_levels(trend, price, highs, lows):
    """
    BUY:  SL = recent swing low,  TP = 1:1 and 1:2
    SELL: SL = recent swing high, TP = 1:1 and 1:2
    """
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

    rr = round(sl_dist * 2 / sl_dist, 1)
    return sl, tp1, tp2, rr

# ─── STEP 8: SCORE SYSTEM ─────────────────────────────────
def calculate_score(session_priority, trend_ok, entry_ok, vol_ok, retest):
    score   = 0
    reasons = []

    # Session priority
    if session_priority == "HIGH":
        score += 3
        reasons.append("✅ High priority session (Killzone)")
    elif session_priority == "MEDIUM":
        score += 2
        reasons.append("⚡ Medium priority session")
    else:
        score += 1
        reasons.append("⚠️ Low priority session")

    # 15M trend
    if trend_ok:
        score += 2
        reasons.append("✅ 15M Trend confirmed")

    # 5M entry
    if entry_ok:
        score += 2
        reasons.append("✅ 5M Entry conditions met")

    # Volume
    if vol_ok:
        score += 2
        reasons.append("✅ Volume above average")
    else:
        reasons.append("❌ Volume weak")

    # EMA retest bonus
    if retest:
        score += 1
        reasons.append("✅ Price retesting EMA30")

    return score, reasons

# ─── STEP 9: TELEGRAM ─────────────────────────────────────
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

def format_signal(direction, price, sl, tp1, tp2, rr, score,
                  session, priority, ema30_5m, ema50_5m,
                  ema30_15m, ema50_15m, vol_status, reasons):

    now_ist = datetime.now(IST).strftime("%d %b %Y | %I:%M %p IST")
    emoji   = "🟢" if direction == "BUY" else "🔴"

    if score >= 8:
        quality = "A+ — Strong Signal"
    elif score >= 6:
        quality = "B  — Good Setup"
    else:
        quality = "C  — Weak"

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
        f"5M  EMA30: {ema30_5m}  | EMA50: {ema50_5m}",
        f"15M EMA30: {ema30_15m} | EMA50: {ema50_15m}",
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
    print("🚀 BTCUSD EMA 30/50 Bot Started")
    send_telegram(
        "🚀 <b>BTCUSD Signal Bot Online!</b>\n"
        "📊 EMA 30/50 | 15M Trend + 5M Entry\n"
        "⏰ Signals all day (except 12AM-6AM IST)\n"
        "🔔 Priority: Killzone > NY/London Open > Asian"
    )

    last_signal = {"time": None, "dir": None}
    session_trade_count = {}

    while True:
        try:
            session, priority = get_session()
            now = datetime.now(IST)

            if session is None:
                print(f"[{now.strftime('%H:%M')} IST] Dead zone — sleeping")
                time.sleep(CHECK_INTERVAL)
                continue

            # ── Fetch candles ──
            closes_5m,  vols_5m,  highs_5m,  lows_5m  = get_btc_candles("5m",  60)
            closes_15m, vols_15m, highs_15m, lows_15m  = get_btc_candles("15m", 60)

            if closes_5m is None or closes_15m is None:
                time.sleep(60)
                continue

            price = closes_5m[-1]

            # ── Step 2: Trend ──
            trend, ema30_15m, ema50_15m = get_trend(closes_15m)
            if not trend:
                print("Trend unclear — WAIT")
                time.sleep(CHECK_INTERVAL)
                continue

            direction = "BUY" if trend == "BULLISH" else "SELL"

            # ── Step 3: Entry ──
            entry_ok, ema30_5m, ema50_5m = check_entry(closes_5m, trend)

            # ── Step 4: Volume ──
            vol_ok, cur_vol, avg_vol = check_volume(vols_5m)
            vol_status = f"Strong ✅ ({cur_vol:.1f} > avg {avg_vol:.1f})" if vol_ok else f"Weak ❌ ({cur_vol:.1f} < avg {avg_vol:.1f})"

            # ── EMA Retest check ──
            retest = abs(price - ema30_5m) / price * 100 < 0.2 if ema30_5m else False

            # ── Score ──
            score, reasons = calculate_score(priority, bool(trend), entry_ok, vol_ok, retest)

            print(f"[{now.strftime('%H:%M')} IST] BTC: ${price:,.0f} | {direction} | Score: {score}/10 | Session: {session}")

            # ── Min score check ──
            min_score = 6 if priority in ["HIGH", "MEDIUM"] else 8
            if score < min_score:
                print(f"Score {score} < {min_score} — WAIT")
                time.sleep(CHECK_INTERVAL)
                continue

            if not entry_ok:
                print("Entry conditions not met — WAIT")
                time.sleep(CHECK_INTERVAL)
                continue

            # ── Max 2 trades per session ──
            session_key = f"{session}_{now.strftime('%Y%m%d')}"
            if session_trade_count.get(session_key, 0) >= 2:
                print(f"Max 2 trades reached for {session}")
                time.sleep(CHECK_INTERVAL)
                continue

            # ── Duplicate check (30 min gap) ──
            if (last_signal["time"] and
                (now - last_signal["time"]).seconds < 1800 and
                last_signal["dir"] == direction):
                print("Duplicate signal — skipped")
                time.sleep(CHECK_INTERVAL)
                continue

            # ── SL / TP ──
            sl, tp1, tp2, rr = calculate_levels(trend, price, highs_5m, lows_5m)
            if not sl:
                print("Invalid SL — skip")
                time.sleep(CHECK_INTERVAL)
                continue

            # ── Send signal ──
            msg = format_signal(
                direction, price, sl, tp1, tp2, rr, score,
                session, priority, ema30_5m, ema50_5m,
                ema30_15m, ema50_15m, vol_status, reasons
            )
            send_telegram(msg)

            last_signal["time"] = now
            last_signal["dir"]  = direction
            session_trade_count[session_key] = session_trade_count.get(session_key, 0) + 1

            print(f"✅ Signal sent: {direction} @ ${price:,.0f}")

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
