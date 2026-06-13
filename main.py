import os
import time
import requests
from datetime import datetime, timezone, timedelta

# ─── CONFIG ───────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL   = 300  # 5 min

IST = timezone(timedelta(hours=5, minutes=30))

# Sessions IST
SESSIONS = {
    "London":   (12, 30, 15, 30),
    "New York": (18, 30, 21, 30),
}

MAX_TRADES_PER_SESSION = 2
trade_count = {"London": 0, "New York": 0}
last_session = None

# ─── TELEGRAM ─────────────────────────────────────────────
def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=10)
        print("Telegram:", r.status_code)
    except Exception as e:
        print("Telegram error:", e)

# ─── SESSION CHECK ─────────────────────────────────────────
def get_active_session():
    now = datetime.now(IST)
    total = now.hour * 60 + now.minute
    for name, (sh, sm, eh, em) in SESSIONS.items():
        if sh * 60 + sm <= total <= eh * 60 + em:
            return name
    return None

# ─── EMA ──────────────────────────────────────────────────
def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return round(e, 6)

# ─── FETCH CANDLES ────────────────────────────────────────
def get_btc_candles(interval="5m", limit=60):
    """Binance free API — BTCUSDT candles"""
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}"
        r = requests.get(url, timeout=10)
        data = r.json()
        closes  = [float(c[4]) for c in data]
        volumes = [float(c[5]) for c in data]
        highs   = [float(c[2]) for c in data]
        lows    = [float(c[3]) for c in data]
        return closes, volumes, highs, lows
    except Exception as e:
        print(f"BTC candle error: {e}")
        return None, None, None, None

def get_gold_candles(interval="5m", limit=60):
    """Yahoo Finance free API — XAUUSD candles"""
    try:
        yf_interval = "5m" if interval == "5m" else "15m"
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval={yf_interval}&range=1d"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        result = r.json()["chart"]["result"][0]
        closes  = result["indicators"]["quote"][0]["close"]
        volumes = result["indicators"]["quote"][0]["volume"]
        highs   = result["indicators"]["quote"][0]["high"]
        lows    = result["indicators"]["quote"][0]["low"]
        # Remove None values
        combined = [(c, v, h, l) for c, v, h, l in zip(closes, volumes, highs, lows)
                    if c is not None and v is not None]
        if not combined:
            return None, None, None, None
        closes, volumes, highs, lows = zip(*combined)
        return list(closes)[-limit:], list(volumes)[-limit:], list(highs)[-limit:], list(lows)[-limit:]
    except Exception as e:
        print(f"Gold candle error: {e}")
        return None, None, None, None

# ─── SWING HIGH/LOW ───────────────────────────────────────
def get_swing_low(lows, lookback=10):
    return min(lows[-lookback:])

def get_swing_high(highs, lookback=10):
    return max(highs[-lookback:])

# ─── SIDEWAYS CHECK ───────────────────────────────────────
def is_sideways(closes, threshold=0.003):
    """If price range < 0.3% — sideways market"""
    recent = closes[-20:]
    rng = (max(recent) - min(recent)) / min(recent)
    return rng < threshold

# ─── VOLUME CHECK ─────────────────────────────────────────
def volume_confirmed(volumes):
    if len(volumes) < 21:
        return False
    avg_vol = sum(volumes[-21:-1]) / 20
    current_vol = volumes[-1]
    return current_vol > avg_vol

# ─── MAIN ANALYSIS ────────────────────────────────────────
def analyze(symbol, session):
    # Fetch 5M and 15M candles
    if symbol == "BTCUSD":
        closes_5m,  vols_5m,  highs_5m,  lows_5m  = get_btc_candles("5m",  60)
        closes_15m, vols_15m, highs_15m, lows_15m  = get_btc_candles("15m", 60)
    else:
        closes_5m,  vols_5m,  highs_5m,  lows_5m  = get_gold_candles("5m",  60)
        closes_15m, vols_15m, highs_15m, lows_15m  = get_gold_candles("15m", 60)

    if closes_5m is None or closes_15m is None:
        print(f"{symbol}: Failed to fetch candles")
        return None

    if len(closes_5m) < 55 or len(closes_15m) < 55:
        print(f"{symbol}: Not enough candle data")
        return None

    # ── Current price ──
    price = closes_5m[-1]

    # ── Sideways check ──
    if is_sideways(closes_5m):
        print(f"{symbol}: Sideways market — skipping")
        return None

    # ── 15M EMAs (Trend Filter) ──
    ema30_15m = ema(closes_15m, 30)
    ema50_15m = ema(closes_15m, 50)
    if not ema30_15m or not ema50_15m:
        return None

    if ema30_15m > ema50_15m:
        trend_15m = "BULLISH"
    elif ema30_15m < ema50_15m:
        trend_15m = "BEARISH"
    else:
        print(f"{symbol}: 15M trend unclear")
        return None

    # ── 5M EMAs (Entry) ──
    ema30_5m = ema(closes_5m, 30)
    ema50_5m = ema(closes_5m, 50)
    if not ema30_5m or not ema50_5m:
        return None

    # ── Volume check ──
    vol_ok = volume_confirmed(vols_5m)
    vol_status = "Strong ✅" if vol_ok else "Weak ❌"

    # ── Score system ──
    score = 0
    reasons = []

    # Killzone
    score += 1
    reasons.append(f"✅ {session} Killzone active")

    # 15M Trend
    score += 2
    reasons.append(f"✅ 15M Trend: {trend_15m} (EMA30 {'>' if trend_15m=='BULLISH' else '<'} EMA50)")

    # Volume
    if vol_ok:
        score += 2
        reasons.append("✅ Volume above 20-candle average")
    else:
        reasons.append("❌ Volume weak — signal quality lower")

    # ── BUY Setup ──
    if trend_15m == "BULLISH":
        if (ema30_5m > ema50_5m and        # 5M EMA aligned
            price > ema30_5m):              # Price above 30 EMA
            score += 3
            reasons.append("✅ 5M: EMA30 > EMA50, price above EMA30")
            direction = "BUY"

            # EMA retest bonus
            retest_dist = abs(price - ema30_5m) / price * 100
            if retest_dist < 0.2:
                score += 1
                reasons.append("✅ Price retesting EMA30 — ideal entry")
        else:
            print(f"{symbol}: BUY conditions not met on 5M")
            return None

    # ── SELL Setup ──
    elif trend_15m == "BEARISH":
        if (ema30_5m < ema50_5m and        # 5M EMA aligned
            price < ema30_5m):              # Price below 30 EMA
            score += 3
            reasons.append("✅ 5M: EMA30 < EMA50, price below EMA30")
            direction = "SELL"

            # EMA retest bonus
            retest_dist = abs(price - ema30_5m) / price * 100
            if retest_dist < 0.2:
                score += 1
                reasons.append("✅ Price retesting EMA30 — ideal entry")
        else:
            print(f"{symbol}: SELL conditions not met on 5M")
            return None

    # Minimum score
    if score < 6:
        print(f"{symbol}: Score too low ({score}/10)")
        return None

    # ── SL / TP ──
    if direction == "BUY":
        sl    = round(get_swing_low(lows_5m, 10), 2)
        sl_dist = price - sl
        tp1   = round(price + sl_dist, 2)        # 1:1
        tp2   = round(price + sl_dist * 2, 2)    # 1:2
        emoji = "🟢"
    else:
        sl    = round(get_swing_high(highs_5m, 10), 2)
        sl_dist = sl - price
        tp1   = round(price - sl_dist, 2)
        tp2   = round(price - sl_dist * 2, 2)
        emoji = "🔴"

    # Min RR check
    if sl_dist <= 0:
        return None
    rr = round(sl_dist * 2 / sl_dist, 1)  # Always 1:2

    # Trade quality
    if score >= 8:
        quality = "A+ — High Quality"
    elif score >= 6:
        quality = "B — Good Setup"
    else:
        quality = "C — Weak"

    return {
        "symbol":    symbol,
        "direction": direction,
        "emoji":     emoji,
        "trend":     trend_15m,
        "entry":     round(price, 2),
        "sl":        sl,
        "tp1":       tp1,
        "tp2":       tp2,
        "rr":        f"1:{rr}",
        "score":     score,
        "quality":   quality,
        "vol_status": vol_status,
        "session":   session,
        "ema30_5m":  round(ema30_5m, 2),
        "ema50_5m":  round(ema50_5m, 2),
        "ema30_15m": round(ema30_15m, 2),
        "ema50_15m": round(ema50_15m, 2),
        "reasons":   reasons,
    }

# ─── FORMAT MESSAGE ───────────────────────────────────────
def format_message(sig: dict) -> str:
    now_ist = datetime.now(IST).strftime("%d %b %Y | %I:%M %p IST")
    lines = [
        f"{sig['emoji']} <b>{sig['symbol']} — {sig['direction']} SIGNAL</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"💹 <b>Pair      :</b> {sig['symbol']}",
        f"📊 <b>Direction :</b> {sig['direction']}",
        f"📈 <b>Trend     :</b> {sig['trend']} (15M)",
        f"⏱ <b>Timeframe :</b> 15M Trend | 5M Entry",
        f"🕐 <b>Session   :</b> {sig['session']} Killzone",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"📍 <b>Entry     :</b> {sig['entry']}",
        f"🛑 <b>Stop Loss :</b> {sig['sl']}",
        f"🎯 <b>TP1 (1:1) :</b> {sig['tp1']}",
        f"🎯 <b>TP2 (1:2) :</b> {sig['tp2']}",
        f"⚖️ <b>R:R       :</b> {sig['rr']}",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"📦 <b>Volume    :</b> {sig['vol_status']}",
        f"⭐ <b>Quality   :</b> {sig['quality']}",
        f"🔢 <b>Score     :</b> {sig['score']}/10",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"EMA30 5M : {sig['ema30_5m']}",
        f"EMA50 5M : {sig['ema50_5m']}",
        f"EMA30 15M: {sig['ema30_15m']}",
        f"EMA50 15M: {sig['ema50_15m']}",
        "━━━━━━━━━━━━━━━━━━━━━",
        "<b>Reason for Entry:</b>",
    ]
    for r in sig["reasons"]:
        lines.append(f"  {r}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🕐 {now_ist}")
    lines.append("⚠️ <i>Risk 1% only. Confirm on chart before entry.</i>")
    return "\n".join(lines)

# ─── MAIN LOOP ────────────────────────────────────────────
def main():
    global last_session
    print("🚀 EMA 30/50 Signal Bot Started — BTCUSD + XAUUSD")
    send_telegram(
        "🚀 <b>Signal Bot Online!</b>\n"
        "📊 Strategy: EMA 30/50 | 15M+5M\n"
        "💰 BTCUSD + XAUUSD\n"
        "⏰ London + New York sessions only\n"
        "Waiting for next killzone..."
    )

    last_signal = {
        "BTCUSD": {"time": None, "dir": None},
        "XAUUSD": {"time": None, "dir": None},
    }

    while True:
        try:
            session = get_active_session()
            now = datetime.now(IST)

            # Reset trade count when new session starts
            if session and session != last_session:
                trade_count[session] = 0
                last_session = session
                send_telegram(f"🔔 <b>{session} Killzone Started!</b>\nWatching for setups...")

            if session is None:
                now_str = now.strftime("%H:%M")
                print(f"[{now_str} IST] No active session")
                time.sleep(CHECK_INTERVAL)
                continue

            # Max trades check
            if trade_count[session] >= MAX_TRADES_PER_SESSION:
                print(f"Max trades reached for {session} session")
                time.sleep(CHECK_INTERVAL)
                continue

            for symbol in ["XAUUSD", "BTCUSD"]:
                print(f"\nAnalyzing {symbol}...")
                signal = analyze(symbol, session)

                if signal:
                    last = last_signal[symbol]
                    # 30 min gap between same direction signals
                    if (last["time"] is None or
                        (now - last["time"]).seconds > 1800 or
                        last["dir"] != signal["direction"]):

                        msg = format_message(signal)
                        send_telegram(msg)
                        trade_count[session] += 1
                        last_signal[symbol]["time"] = now
                        last_signal[symbol]["dir"]  = signal["direction"]
                        print(f"✅ Signal: {symbol} {signal['direction']} @ {signal['entry']}")

                        if trade_count[session] >= MAX_TRADES_PER_SESSION:
                            send_telegram(f"⛔ <b>Max 2 trades reached for {session} session.</b>\nNo more signals until next session.")
                            break
                    else:
                        print(f"⏭ Duplicate skipped: {symbol}")
                else:
                    print(f"⏳ No valid setup: {symbol}")

        except Exception as e:
            print("Loop error:", e)

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
