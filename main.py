import os
import time
import requests
from datetime import datetime, timezone, timedelta

# ─── CONFIG ───────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL  = 300  # seconds (5 min)

# Gold price API (free)
GOLD_API_URL = "https://api.gold-api.com/price/XAU"

# IST = UTC+5:30
IST = timezone(timedelta(hours=5, minutes=30))

# ─── SESSIONS (IST) ───────────────────────────────────────
SESSIONS = {
    "London":   (12, 30, 15, 30),   # 12:30 PM – 3:30 PM IST
    "New York": (18, 30, 21, 30),   # 6:30 PM  – 9:30 PM  IST
}

# ─── TELEGRAM ─────────────────────────────────────────────
def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        print("Telegram sent:", r.status_code)
    except Exception as e:
        print("Telegram error:", e)

# ─── GET PRICE ────────────────────────────────────────────
def get_gold_price():
    try:
        r = requests.get(GOLD_API_URL, timeout=10)
        data = r.json()
        price = float(data.get("price", 0))
        return price
    except Exception as e:
        print("Price fetch error:", e)
        return None

# ─── SESSION CHECK ────────────────────────────────────────
def get_active_session():
    now = datetime.now(IST)
    h, m = now.hour, now.minute
    total = h * 60 + m
    for name, (sh, sm, eh, em) in SESSIONS.items():
        start = sh * 60 + sm
        end   = eh * 60 + em
        if start <= total <= end:
            return name
    return None

# ─── ICT ANALYSIS ─────────────────────────────────────────
def analyze(price: float, session: str):
    """
    Simple ICT rules based on price levels.
    Returns: signal dict or None
    """
    # Round numbers (psychological levels) — key ICT concept
    round_level = round(price / 50) * 50  # nearest $50 level
    dist_to_round = abs(price - round_level)

    # Premium / Discount zones (relative to round level)
    # Discount = price below round level → BUY bias
    # Premium  = price above round level → SELL bias
    is_discount = price < round_level
    is_premium  = price > round_level

    score = 0
    reasons = []

    # ── Killzone active ──
    score += 2
    reasons.append(f"✅ {session} Killzone active")

    # ── Near round number (liquidity zone) ──
    if dist_to_round < 3.0:
        score += 2
        reasons.append(f"✅ Near round level ${round_level:.0f} (liquidity zone)")
    elif dist_to_round < 7.0:
        score += 1
        reasons.append(f"⚡ Approaching round level ${round_level:.0f}")

    # ── Premium / Discount ──
    if is_discount:
        score += 2
        direction = "BUY"
        reasons.append("✅ Price in Discount zone → BUY bias")
    else:
        score += 2
        direction = "SELL"
        reasons.append("✅ Price in Premium zone → SELL bias")

    # ── Time-based manipulation window ──
    now = datetime.now(IST)
    # First 30 min of session = manipulation phase
    if session == "London":
        manip_end = now.replace(hour=13, minute=0, second=0)
    else:
        manip_end = now.replace(hour=19, minute=0, second=0)

    if now < manip_end:
        score += 1
        reasons.append("⚡ Manipulation phase — watch for sweep")
    else:
        score += 2
        reasons.append("✅ Distribution phase — expansion likely")

    # ── Minimum score to signal ──
    if score < 6:
        return None

    # ── Calculate Entry / SL / TP ──
    spread = 0.30  # typical gold spread

    if direction == "BUY":
        entry = round(price + spread, 2)
        sl    = round(entry - 8.0, 2)
        tp1   = round(entry + 8.0, 2)
        tp2   = round(entry + 15.0, 2)
        tp3   = round(entry + 25.0, 2)
        emoji = "🟢"
    else:
        entry = round(price - spread, 2)
        sl    = round(entry + 8.0, 2)
        tp1   = round(entry - 8.0, 2)
        tp2   = round(entry - 15.0, 2)
        tp3   = round(entry - 25.0, 2)
        emoji = "🔴"

    rr = round((tp2 - entry) / (entry - sl), 2) if direction == "BUY" else round((entry - tp2) / (sl - entry), 2)

    return {
        "direction": direction,
        "emoji": emoji,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "rr": abs(rr),
        "score": score,
        "session": session,
        "reasons": reasons,
        "price": price,
    }

# ─── FORMAT MESSAGE ───────────────────────────────────────
def format_message(sig: dict) -> str:
    now_ist = datetime.now(IST).strftime("%d %b %Y | %I:%M %p IST")
    lines = [
        f"{sig['emoji']} <b>XAUUSD {sig['direction']} SIGNAL</b>",
        "━━━━━━━━━━━━━━━━━━━",
        f"📍 <b>Entry :</b> {sig['entry']}",
        f"🛑 <b>SL    :</b> {sig['sl']}",
        f"🎯 <b>TP1   :</b> {sig['tp1']}",
        f"🎯 <b>TP2   :</b> {sig['tp2']}",
        f"🎯 <b>TP3   :</b> {sig['tp3']}",
        "━━━━━━━━━━━━━━━━━━━",
        f"📊 <b>Score :</b> {sig['score']}/10",
        f"⚖️ <b>RR    :</b> 1:{sig['rr']}",
        f"🕐 <b>Session:</b> {sig['session']} KZ",
        "━━━━━━━━━━━━━━━━━━━",
        "<b>ICT Reasons:</b>",
    ]
    for r in sig["reasons"]:
        lines.append(f"  {r}")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🕐 {now_ist}")
    lines.append("⚠️ <i>Always confirm on chart before entry</i>")
    return "\n".join(lines)

# ─── MAIN LOOP ────────────────────────────────────────────
def main():
    print("🚀 XAUUSD ICT Signal Bot Started")
    send_telegram("🚀 <b>XAUUSD Signal Bot Online!</b>\nWaiting for London/NY killzone...")

    last_signal_time = None
    last_direction   = None

    while True:
        try:
            session = get_active_session()

            if session is None:
                now_ist = datetime.now(IST).strftime("%H:%M")
                print(f"[{now_ist} IST] No active session — waiting...")
                time.sleep(CHECK_INTERVAL)
                continue

            price = get_gold_price()
            if price is None:
                time.sleep(60)
                continue

            print(f"[{session}] XAUUSD price: {price}")

            signal = analyze(price, session)

            if signal:
                now = datetime.now(IST)
                # Avoid duplicate signals — wait 30 min between same direction
                if (last_signal_time is None or
                    (now - last_signal_time).seconds > 1800 or
                    last_direction != signal["direction"]):

                    msg = format_message(signal)
                    send_telegram(msg)
                    last_signal_time = now
                    last_direction   = signal["direction"]
                    print(f"Signal sent: {signal['direction']} @ {signal['entry']}")
                else:
                    print("Duplicate signal — skipped")
            else:
                print("No valid ICT setup — WAIT")

        except Exception as e:
            print("Loop error:", e)

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
