from flask import Flask, render_template, request, jsonify, session, redirect
import random
import string
import json
import os
import time
import requests
import smtplib
import ssl
import certifi
from email.mime.text import MIMEText
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()  # reads .env file if present (local development)

app = Flask(__name__, template_folder=".")
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

DB_FILE = "users.json"

# --- MongoDB (permanent storage - Render's local disk resets on restart/sleep) ---
MONGODB_URI = os.environ.get("MONGODB_URI", "")
mongo_collection = None
if MONGODB_URI:
    try:
        _mongo_client = MongoClient(MONGODB_URI, tls=True, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=30000)
        mongo_collection = _mongo_client.get_database("profitix").get_collection("app_data")
    except Exception as e:
        print("Mongo connection error:", repr(e))

DEFAULT_DATA = {"users": {}, "withdrawals": [], "support_tickets": [], "notifications": [], "promo_codes": {}, "telegram_points": {}}

def load_data():
    if mongo_collection is not None:
        try:
            doc = mongo_collection.find_one({"_id": "main"})
            if doc:
                doc.pop("_id", None)
                for key, val in DEFAULT_DATA.items():
                    if key not in doc:
                        doc[key] = val
                return doc
            return dict(DEFAULT_DATA)
        except Exception as e:
            print("Mongo load error:", repr(e))

    # Fallback: local file (works for local testing; NOT persistent on Render free tier)
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    for key, val in DEFAULT_DATA.items():
                        if key not in data:
                            data[key] = val
                    return data
        except:
            pass
    return dict(DEFAULT_DATA)

def save_data(data):
    if mongo_collection is not None:
        try:
            doc = dict(data)
            doc["_id"] = "main"
            mongo_collection.replace_one({"_id": "main"}, doc, upsert=True)
            return
        except Exception as e:
            print("Mongo save error:", repr(e))

    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

db = load_data()
users_db = db.get("users", {})
withdrawals_db = db.get("withdrawals", [])
support_tickets_db = db.get("support_tickets", [])
notifications_db = db.get("notifications", [])
promo_codes_db = db.get("promo_codes", {})
telegram_points_db = db.get("telegram_points", {})
otp_db = {}
broadcast_sessions = {}
redeem_sessions = {}
top_command_cooldowns = {}  # user_id -> last-used timestamp, for the /top rate limit

def save_all():
    """Persist every in-memory collection together so nothing gets dropped on save."""
    save_data({
        "users": users_db,
        "withdrawals": withdrawals_db,
        "support_tickets": support_tickets_db,
        "notifications": notifications_db,
        "promo_codes": promo_codes_db,
        "telegram_points": telegram_points_db
    })

# --- Migrate old notifications (no id/likes/comments) so like & comment features work on old data ---
_notif_migrated = False
for _i, _n in enumerate(notifications_db):
    if "id" not in _n:
        _n["id"] = _i + 1
        _notif_migrated = True
    if "likes" not in _n:
        _n["likes"] = []
        _notif_migrated = True
    if "comments" not in _n:
        _n["comments"] = []
        _notif_migrated = True
if _notif_migrated:
    save_all()

def next_notif_id():
    if not notifications_db:
        return 1
    return max(n.get("id", 0) for n in notifications_db) + 1

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
ADMIN_SUPPORT_GC = os.environ.get("ADMIN_SUPPORT_GC", "")
ADMIN_PAYMENT_CHANNEL = os.environ.get("ADMIN_PAYMENT_CHANNEL", "@PROFITIX77")

# --- Group-chat "message = point" earning system ---
# Set POINTS_GC_CHAT_ID to the Telegram chat_id of the group where messages should count as points
# (this is usually a negative number for groups, e.g. -1001234567890).
POINTS_GC_CHAT_ID = os.environ.get("POINTS_GC_CHAT_ID", "")
POINT_VALUE = float(os.environ.get("POINT_VALUE", "0.001"))         # ₹ credited per point on redeem (10000 pts = ₹10)
POINTS_REDEEM_THRESHOLD = int(os.environ.get("POINTS_REDEEM_THRESHOLD", "50"))

def get_point_entry(tg_user_id):
    if tg_user_id not in telegram_points_db:
        telegram_points_db[tg_user_id] = {"points": 0, "email": None, "username": None}
    return telegram_points_db[tg_user_id]

def get_display_name(entry):
    """Used only for /top and the cheating alert: @username if set, else
    'First Last' (or just 'First' if no last name), else 'unknown'."""
    if entry.get("tg_username"):
        return f"@{entry['tg_username']}"
    first = (entry.get("first_name") or "").strip()
    last = (entry.get("last_name") or "").strip()
    full = f"{first} {last}".strip()
    return full if full else "unknown"

def send_tg_message(chat_id, text, reply_to=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print("Group message send error:", e)

def find_username_by_email(email):
    email = email.strip().lower()
    for uname, udata in users_db.items():
        if udata.get("email", "").strip().lower() == email:
            return uname
    return None

def find_point_entry_by_website_username(query):
    """Looks up a point entry by the linked PROFITIX website username (set when the user
    used /link or /redeem with their registered Gmail)."""
    query = query.strip().lstrip('@').lower()
    for uid, entry in telegram_points_db.items():
        if (entry.get("username") or "").lower() == query:
            return uid, entry
    return None, None

def refresh_all_names():
    """Fetches fresh username/first_name/last_name for every tracked user directly from
    Telegram (via getChatMember) — fixes old entries that never got a name saved,
    without needing that person to send a new message."""
    if not POINTS_GC_CHAT_ID:
        return 0, 0
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getChatMember"
    updated = 0
    failed = 0
    for uid, entry in telegram_points_db.items():
        try:
            resp = requests.get(url, params={"chat_id": POINTS_GC_CHAT_ID, "user_id": uid}, timeout=10)
            data = resp.json()
            if data.get("ok"):
                user = data["result"]["user"]
                if user.get("username"):
                    entry["tg_username"] = user["username"]
                if user.get("first_name"):
                    entry["first_name"] = user["first_name"]
                if user.get("last_name"):
                    entry["last_name"] = user["last_name"]
                updated += 1
            else:
                failed += 1
        except Exception as e:
            print("refresh_all_names error for", uid, repr(e))
            failed += 1
    save_all()
    return updated, failed

def analyze_cheating(entry):
    """Spam/farming heuristics based on the user's logged group messages (kept since their
    last redeem). Uses proportional/episode-based checks — occasional short messages or one
    fast burst in a large sample is normal human behaviour and stays green; only a repeated
    pattern (scaled to how many messages were sent) gets flagged."""
    log = entry.get("message_log", [])
    total = len(log)
    if total == 0:
        return False, ["Is user ka koi message log nahi mila."], total

    texts = [(m.get("text") or "").strip().lower() for m in log]
    times = []
    for m in log:
        try:
            times.append(float(m.get("ts", 0) or 0))
        except (TypeError, ValueError):
            times.append(0)

    flags = []

    # 1) Duplicate-burst episodes: 3+ identical messages sent within 10 seconds of each other.
    # One such episode in a decent-sized sample is normal (people repeat "hi" etc. sometimes) —
    # only flag if it happens more often than roughly once per 50 messages sent.
    episodes = 0
    i = 0
    while i < total - 2:
        if texts[i] and texts[i] == texts[i + 1] == texts[i + 2] and (times[i + 2] - times[i]) <= 10:
            episodes += 1
            i += 3  # move past this episode instead of re-counting overlapping windows
        else:
            i += 1
    allowed_episodes = max(1, total // 50)
    if episodes > allowed_episodes:
        flags.append(f"⚠️ {episodes} baar 3+ same messages 10 second ke andar bheje gaye (normal range: ~{allowed_episodes} tak {total} messages me) — repeat-spam pattern")

    # 2) Absolute safety net: 5+ identical messages in a row is never normal, regardless of sample size.
    max_streak = 1
    cur_streak = 1
    for i in range(1, total):
        if texts[i] and texts[i] == texts[i - 1]:
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)
        else:
            cur_streak = 1
    if max_streak >= 5:
        flags.append(f"⚠️ Lagatar {max_streak} baar bilkul wahi same message bheja gaya — bahut clear spam pattern")

    # 3) Short/junk messages: a chunk of short replies (ok, hi, etc.) is normal chatting —
    # only flag once they make up more than ~35% of everything sent.
    short_count = sum(1 for t in texts if len(t) <= 2)
    short_ratio = short_count / total
    if total >= 15 and short_ratio >= 0.35:
        flags.append(f"⚠️ {round(short_ratio * 100)}% ({short_count}/{total}) messages bahut chhote/junk (1-2 characters) hain")

    # 4) Variety check: relaxed — only flag when the majority of messages are repeats.
    unique_ratio = len(set(texts)) / total
    if total >= 20 and unique_ratio <= 0.45:
        flags.append(f"⚠️ Sirf {round(unique_ratio * 100)}% messages unique hain — kaafi kam variety, farming jaisa lagta hai")

    # 5) Overall duplicate share of a single exact message — allow a fair chunk of repeats,
    # only flag when one message dominates the sample.
    counts = {}
    for t in texts:
        counts[t] = counts.get(t, 0) + 1
    most_common_text, most_common_count = max(counts.items(), key=lambda kv: kv[1])
    duplicate_ratio = most_common_count / total
    if total >= 15 and duplicate_ratio >= 0.3:
        preview = most_common_text[:25] if most_common_text else "(khaali)"
        flags.append(f"⚠️ {round(duplicate_ratio * 100)}% ({most_common_count}/{total}) messages ek hi text hain — '{preview}'")

    suspicious = len(flags) > 0
    if not flags:
        flags.append(f"✅ {total} messages check kiye, koi unusual spam/duplicate pattern nahi mila — normal lag raha hai.")

    return suspicious, flags, total

def handle_admin_reply_command(chat_id, text, reply_from):
    """Lets the admin reply to a user's message in the points group with /block or /unblock
    to act on that exact user directly — no need to type their username.
    (/check stays DM-only, handled separately.)"""
    target_uid = str(reply_from.get("id"))
    target_tgname = reply_from.get("username", "")
    entry = get_point_entry(target_uid)
    if target_tgname:
        entry["tg_username"] = target_tgname

    parts = text.strip().split()
    cmd = parts[0].lower()

    if cmd == "/block":
        hours = 24
        if len(parts) >= 2:
            try:
                hours = float(parts[1])
            except ValueError:
                hours = 24
        entry["blocked_until"] = time.time() + hours * 3600
        save_all()
        send_tg_message(chat_id, f"🚫 @{target_tgname or target_uid} ko {hours} hours ke liye block kar diya — is dauran points count nahi honge.")
        return

    elif cmd == "/unblock":
        entry["blocked_until"] = 0
        save_all()
        send_tg_message(chat_id, f"✅ @{target_tgname or target_uid} ka block hata diya — points ab count honge.")
        return

    save_all()

def check_realtime_spam(entry):
    """Runs after every earned point on the last few messages, for immediate spam detection.
    Returns a short reason string if something looks suspicious right now, else None."""
    log = entry.get("message_log", [])
    if len(log) < 3:
        return None

    recent_texts = [(m.get("text") or "").strip().lower() for m in log[-5:]]

    # 3+ identical messages in a row = repeat-spam
    if len(recent_texts) >= 3 and recent_texts[-1] and recent_texts[-1] == recent_texts[-2] == recent_texts[-3]:
        return f"Lagatar same message bhej raha hai: '{recent_texts[-1][:30]}'"

    # 4 messages fired within a 5 second window = flooding
    times = []
    for m in log[-4:]:
        try:
            times.append(float(m.get("ts", 0)))
        except (TypeError, ValueError):
            pass
    if len(times) >= 4 and (times[-1] - times[0]) < 5:
        return "Bahut fast/lagatar messages bhej raha hai (spam ya bot jaisa)"

    # 3+ back-to-back junk/short messages
    if len(recent_texts) >= 3 and all(len(t) <= 2 for t in recent_texts[-3:]):
        return "Lagatar chhote/faltu (1-2 character) messages bhej raha hai"

    return None

def maybe_alert_admin(user_id, entry, reason):
    if not ADMIN_CHAT_ID or not reason:
        return
    uname = entry.get("username") or "Not linked"
    display_name = get_display_name(entry)
    alert_text = (
        f"🚨 Cheating alert!\n"
        f"Telegram: {display_name}\n"
        f"Website username: {uname}\n"
        f"Points: {entry.get('points', 0)}\n"
        f"Reason: {reason}"
    )
    send_tg_message(ADMIN_CHAT_ID, alert_text)

def record_cheat_strike(entry):
    """Logs a cheating strike with a 24h rolling window. If the user hits 5 strikes within
    24h, blocks them from earning points for the next 24 hours. Returns the current strike count."""
    now = time.time()
    strikes = entry.setdefault("cheat_strikes", [])
    strikes.append(now)
    strikes[:] = [t for t in strikes if now - t < 86400]
    if len(strikes) >= 5:
        entry["blocked_until"] = now + 86400
    return len(strikes)

def handle_group_points_message(user_id, chat_id, msg):
    """Handles the 'message = point' group-chat earning system: point counting,
    'point' / 'redeem' / 'link' commands, and the redeem-email reply flow."""
    text = msg.get("text", "")
    msg_id = msg.get("message_id")
    from_obj = msg.get("from", {})
    tg_username = from_obj.get("username", "")

    entry = get_point_entry(user_id)
    if tg_username:
        entry["tg_username"] = tg_username
    if from_obj.get("first_name"):
        entry["first_name"] = from_obj.get("first_name", "")
    if from_obj.get("last_name"):
        entry["last_name"] = from_obj.get("last_name", "")

    stripped = (text or "").strip()
    lower = stripped.lower()
    handled = False

    # Step 2 of redeem flow: user is replying with their Gmail
    if user_id in redeem_sessions and redeem_sessions[user_id].get("step") == "AWAITING_EMAIL":
        handled = True
        email = stripped.lower()
        if "@" in email and "." in email:
            matched_username = find_username_by_email(email)
            if matched_username:
                pts = entry["points"]
                if pts >= POINTS_REDEEM_THRESHOLD:
                    credit = round(pts * POINT_VALUE, 2)
                    users_db[matched_username]["balance"] += credit
                    entry["points"] = 0
                    entry["message_log"] = []
                    entry["email"] = email
                    entry["username"] = matched_username
                    save_all()
                    send_tg_message(chat_id, f"✅ Redeem successful! {pts} points = ₹{credit} aapke PROFITIX account ({matched_username}) me add ho gaya hai. Website ke Withdraw tab se ab withdraw kar sakte ho!", msg_id)
                else:
                    send_tg_message(chat_id, f"⚠️ Aapke paas sirf {pts} points hain. Redeem ke liye kam se kam {POINTS_REDEEM_THRESHOLD} points chahiye.", msg_id)
            else:
                send_tg_message(chat_id, "❌ Account nahi mila. Pehle register karein: https://profitix.onrender.com\nPhir yahan bhejein: /link sarimkhan@gmail.com", msg_id)
        else:
            send_tg_message(chat_id, "⚠️ Ye valid Gmail nahi lagi. Kripya apni registered Gmail address reply karein.", msg_id)
        del redeem_sessions[user_id]

    if not handled and lower in ("/point", "point", "/points", "points"):
        handled = True
        pts = entry["points"]
        send_tg_message(chat_id, f"Point- {pts}", msg_id)

    elif not handled and lower in ("/top", "/leaderboard", "top"):
        handled = True
        now = time.time()
        if user_id != ADMIN_CHAT_ID and now - top_command_cooldowns.get(user_id, 0) < 3600:
            pass  # rate-limited: 1 use per hour per user (owner is exempt) — stay silent
        else:
            top_command_cooldowns[user_id] = now
            ranked = sorted(telegram_points_db.items(), key=lambda kv: kv[1].get("points", 0), reverse=True)
            ranked = [(uid, e) for uid, e in ranked if e.get("points", 0) > 0][:5]
            if not ranked:
                send_tg_message(chat_id, "Abhi tak koi points nahi hain.", msg_id)
            else:
                medals = ["🥇", "🥈", "🥉", "4.", "5."]
                lines = ["🏆 Top 5 Point Earners:"]
                for idx, (uid, e) in enumerate(ranked):
                    name = get_display_name(e)
                    lines.append(f"{medals[idx]} {name} — {e.get('points', 0)} points")
                send_tg_message(chat_id, "\n".join(lines), msg_id)

    elif not handled and lower.startswith("/link"):
        handled = True
        parts = stripped.split(maxsplit=1)
        email = parts[1].strip().lower() if len(parts) > 1 else ""
        if "@" in email and "." in email:
            matched_username = find_username_by_email(email)
            if matched_username:
                entry["email"] = email
                entry["username"] = matched_username
                save_all()
                send_tg_message(chat_id, f"✅ Aapka account ({matched_username}) is Telegram se link ho gaya! Points ginte rahenge.", msg_id)
            else:
                send_tg_message(chat_id, "❌ Account nahi mila. Pehle register karein: https://profitix.onrender.com\nPhir yahan bhejein: /link sarimkhan@gmail.com", msg_id)
        else:
            send_tg_message(chat_id, "Sahi format: /link yourgmail@gmail.com", msg_id)

    elif not handled and lower in ("/redeem", "redeem"):
        handled = True
        pts = entry["points"]
        if pts < POINTS_REDEEM_THRESHOLD:
            send_tg_message(chat_id, f"⚠️ Redeem ke liye kam se kam {POINTS_REDEEM_THRESHOLD} points chahiye. Aapke paas abhi {pts} points hain.", msg_id)
        elif entry.get("username"):
            credit = round(pts * POINT_VALUE, 2)
            users_db[entry["username"]]["balance"] += credit
            entry["points"] = 0
            entry["message_log"] = []
            save_all()
            send_tg_message(chat_id, f"✅ Redeem successful! {pts} points = ₹{credit} aapke account ({entry['username']}) me add ho gaya. Website se withdraw kar sakte ho!", msg_id)
        else:
            redeem_sessions[user_id] = {"step": "AWAITING_EMAIL"}
            send_tg_message(chat_id, "🎉 Redeem karne ke liye apni PROFITIX website wali registered Gmail reply karke bhejein.", msg_id)

    # Every other normal message earns 1 point — log it for later cheating checks.
    # This log stays until the user redeems (then it resets), so /check only ever looks
    # at messages since their last redeem.
    # Skipped entirely if the user is currently blocked (5 cheat strikes within 24h).
    if not handled and stripped:
        if time.time() < entry.get("blocked_until", 0):
            pass  # earning suspended for 24h after repeated cheating — stay silent
        else:
            entry["points"] += 1
            log = entry.setdefault("message_log", [])
            log.append({"text": stripped[:200], "ts": msg.get("date", 0)})
            # Every message stays logged until the user redeems (so /check sees full history
            # since their last redeem) — capped at 500 as a safety net so one very active user
            # can't bloat the shared database document for everyone.
            if len(log) > 500:
                del log[0]
            save_all()

            spam_reason = check_realtime_spam(entry)
            if spam_reason:
                entry["points"] = max(0, entry["points"] - 20)
                del log[-20:]  # remove the last 20 logged messages along with the 20 cut points
                strike_count = record_cheat_strike(entry)
                save_all()
                send_tg_message(chat_id, "⚠️ Aapne cheating ki hai — spam/duplicate messages detect hue, isliye 20 points kaat diye gaye hain. Aage se normal messages bhejein.", msg_id)
                if strike_count >= 5:
                    send_tg_message(chat_id, "🚫 5 baar cheating pakdi gayi — agle 24 hours ke liye aapke points count nahi honge.", msg_id)
                maybe_alert_admin(user_id, entry, spam_reason)

# --- Email OTP via Brevo API (HTTPS-based — works on Render, unlike raw SMTP which Render blocks) ---
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
SENDER_EMAIL = os.environ.get("GMAIL_ADDRESS", "no-reply@profitix.com")
OTP_EXPIRY_MINUTES = 5


def send_otp_email(to_email, otp):
    """Sends a real OTP using Brevo's HTTPS email API. Returns True/False."""
    if not BREVO_API_KEY:
        print("BREVO_API_KEY not set - cannot send real email.")
        return False

    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY,
        "content-type": "application/json"
    }
    payload = {
        "sender": {"name": "PROFITIX", "email": SENDER_EMAIL},
        "to": [{"email": to_email}],
        "subject": "Your PROFITIX Verification Code",
        "textContent": f"Your PROFITIX OTP is: {otp}\n\nThis code will expire in {OTP_EXPIRY_MINUTES} minutes. Do not share it with anyone.\n\n- Team PROFITIX"
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code in (200, 201):
            return True
        print("Brevo API error:", resp.status_code, resp.text)
        return False
    except Exception as e:
        print("Email send error:", repr(e))
        return False

def generate_ref_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/join/group')
def join_group():
    return redirect("https://t.me/PROFITIX11")

@app.route('/join/earning')
def join_earning():
    return redirect("https://t.me/PROFITIX00")

@app.route('/join/payment')
def join_payment():
    return redirect("https://t.me/PROFITIX77")

@app.route('/api/send_otp', methods=['POST'])
def send_otp():
    data = request.json
    email = data.get('email', '').strip().lower()

    if not email or '@' not in email or '.' not in email:
        return jsonify({"status": "error", "message": "Kripya ek valid Gmail address daalein!"})

    for u, udata in users_db.items():
        if udata.get('email') == email:
            return jsonify({"status": "error", "message": "Yeh email address pehle se registered hai!"})

    otp = str(random.randint(100000, 999999))
    otp_db[email] = {
        "otp": otp,
        "expires_at": (datetime.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)).isoformat()
    }

    sent = send_otp_email(email, otp)

    if sent:
        return jsonify({
            "status": "success",
            "message": f"OTP aapke email ({email}) par bhej diya gaya hai! Inbox/Spam check karein."
        })
    else:
        # Email server configured nahi hai (local testing) - fallback so dev flow doesn't break
        return jsonify({
            "status": "success",
            "message": f"[DEV MODE - email not configured] OTP: {otp}"
        })

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username', '').strip()
    email = data.get('email', '').strip().lower()
    password = data.get('password')
    user_otp = data.get('otp', '').strip()
    ref_code = data.get('referral_code', '').strip()
    device_id = data.get('device_id', '').strip()

    if device_id:
        for u, udata in users_db.items():
            if udata.get('device_id') == device_id:
                return jsonify({"status": "error", "message": "Is device par pehle hi account ban chuka hai!"})

    if not username or not password or not email:
        return jsonify({"status": "error", "message": "Sabhi details bharna zaroori hai!"})

    if username in users_db:
        return jsonify({"status": "error", "message": "Username pehle se exist karta hai!"})

    for u, udata in users_db.items():
        if udata.get('email', '').strip().lower() == email:
            return jsonify({"status": "error", "message": "Ye Gmail pehle se ek account me use ho chuka hai!"})

    stored = otp_db.get(email)
    if not stored:
        return jsonify({"status": "error", "message": "Pehle OTP bhejein!"})

    if datetime.now() > datetime.fromisoformat(stored["expires_at"]):
        otp_db.pop(email, None)
        return jsonify({"status": "error", "message": "OTP expire ho chuka hai, dobara bhejein!"})

    if stored["otp"] != user_otp:
        return jsonify({"status": "error", "message": "Galat OTP!"})

    otp_db.pop(email, None)
    new_ref_code = generate_ref_code()

    referred_by = None
    if ref_code:
        for u, udata in users_db.items():
            if udata.get('referral_code', '').strip().upper() == ref_code.upper():
                referred_by = u
                break

    users_db[username] = {
        "email": email,
        "password": password,
        "balance": 0.0,
        "referral_code": new_ref_code,
        "referrals": [],
        "referred_by": referred_by,
        "device_id": device_id,
        "support_tickets": [],
        "checkin_day": 0,
        "last_checkin_date": "",
        "used_promos": []
    }

    if referred_by and referred_by in users_db:
        if "referrals" not in users_db[referred_by]:
            users_db[referred_by]["referrals"] = []
        if username not in users_db[referred_by]['referrals']:
            users_db[referred_by]['referrals'].append(username)

    save_all()

    session.permanent = True
    session['user'] = username

    return jsonify({"status": "success", "message": "Account successfully create ho gaya!"})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password')

    user = users_db.get(username)
    if user and user['password'] == password:
        session.permanent = True
        session['user'] = username
        return jsonify({"status": "success", "message": "Logged in successfully!"})

    return jsonify({"status": "error", "message": "Invalid username or password"})

@app.route('/api/forgot_password_send_otp', methods=['POST'])
def forgot_password_send_otp():
    data = request.json
    email = data.get('email', '').strip().lower()

    matched_username = None
    for u, udata in users_db.items():
        if udata.get('email', '').strip().lower() == email:
            matched_username = u
            break

    if not matched_username:
        return jsonify({"status": "error", "message": "Is Gmail se koi account register nahi hai!"})

    otp = str(random.randint(100000, 999999))
    otp_db[email] = {
        "otp": otp,
        "expires_at": (datetime.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)).isoformat(),
        "purpose": "reset_password"
    }

    sent = send_otp_email(email, otp)
    if sent:
        return jsonify({"status": "success", "message": f"Password reset OTP aapke email ({email}) par bhej diya gaya hai!"})
    else:
        return jsonify({"status": "success", "message": f"[DEV MODE - email not configured] OTP: {otp}"})

@app.route('/api/forgot_password_reset', methods=['POST'])
def forgot_password_reset():
    data = request.json
    email = data.get('email', '').strip().lower()
    user_otp = data.get('otp', '').strip()
    new_password = data.get('new_password', '')

    if not new_password or len(new_password) < 4:
        return jsonify({"status": "error", "message": "Password kam se kam 4 characters ka hona chahiye!"})

    stored = otp_db.get(email)
    if not stored or stored.get("purpose") != "reset_password":
        return jsonify({"status": "error", "message": "Pehle OTP bhejein!"})

    if datetime.now() > datetime.fromisoformat(stored["expires_at"]):
        otp_db.pop(email, None)
        return jsonify({"status": "error", "message": "OTP expire ho chuka hai, dobara bhejein!"})

    if stored["otp"] != user_otp:
        return jsonify({"status": "error", "message": "Galat OTP!"})

    matched_username = None
    for u, udata in users_db.items():
        if udata.get('email', '').strip().lower() == email:
            matched_username = u
            break

    if not matched_username:
        return jsonify({"status": "error", "message": "Account nahi mila!"})

    otp_db.pop(email, None)
    users_db[matched_username]['password'] = new_password
    save_all()

    return jsonify({"status": "success", "message": "Password successfully reset ho gaya! Ab login karein."})

@app.route('/api/logout', methods=['POST'])
def logout():
    session.pop('user', None)
    return jsonify({"status": "success"})

@app.route('/api/user_data', methods=['GET'])
def user_data():
    username = session.get('user')
    if not username or username not in users_db:
        return jsonify({"status": "error", "message": "Not logged in"})

    user = users_db[username]
    if "referrals" not in user:
        user["referrals"] = []
    if "support_tickets" not in user:
        user["support_tickets"] = []
    if "checkin_day" not in user:
        user["checkin_day"] = 0
    if "last_checkin_date" not in user:
        user["last_checkin_date"] = ""
    if "used_promos" not in user:
        user["used_promos"] = []
    if "referral_code" not in user:
        user["referral_code"] = generate_ref_code()
        save_all()

    referral_details = []
    for ref_user in user['referrals']:
        ref_status = "Pending"
        for tx in withdrawals_db:
            if tx['username'] == ref_user and tx['status'] == 'Success':
                ref_status = "Succeed"
                break
        referral_details.append({
            "username": ref_user,
            "status": ref_status
        })

    # Monthly Leaderboard Calculation (Based on successful referrals or total earnings, here based on successful referrals count in current month)
    current_month_str = datetime.now().strftime("%Y-%m")
    leaderboard_data = []
    for uname, uinfo in users_db.items():
        succ_refs = 0
        for ref_u in uinfo.get("referrals", []):
            for tx in withdrawals_db:
                if tx['username'] == ref_u and tx['status'] == 'Success':
                    succ_refs += 1
                    break
        leaderboard_data.append({"username": uname, "score": succ_refs})

    leaderboard_data = sorted(leaderboard_data, key=lambda x: x['score'], reverse=True)[:10]

    notifications_out = []
    for n in notifications_db:
        likes = n.get('likes', [])
        notifications_out.append({
            "id": n.get('id'),
            "title": n.get('title'),
            "message": n.get('message'),
            "image": n.get('image', ''),
            "like_count": len(likes),
            "liked_by_me": username in likes,
            "comments": n.get('comments', [])
        })

    return jsonify({
        "status": "success",
        "username": username,
        "email": user.get('email', ''),
        "balance": user['balance'],
        "referral_code": user['referral_code'],
        "total_referrals": len(user['referrals']),
        "referral_history": referral_details,
        "support_tickets": [t for t in support_tickets_db if t.get('username') == username],
        "notifications": notifications_out,
        "leaderboard": leaderboard_data,
        "checkin_day": user['checkin_day'],
        "last_checkin_date": user['last_checkin_date']
    })

@app.route('/api/claim_checkin', methods=['POST'])
def claim_checkin():
    username = session.get('user')
    if not username or username not in users_db:
        return jsonify({"status": "error", "message": "Not logged in"})

    user = users_db[username]
    today_str = datetime.now().strftime("%Y-%m-%d")

    if user.get("last_checkin_date") == today_str:
        return jsonify({"status": "error", "message": "Aapne aaj ka check-in pehle hi kar liya hai!"})

    last_date = user.get("last_checkin_date", "")
    current_day = user.get("checkin_day", 0)

    if last_date:
        try:
            d1 = datetime.strptime(last_date, "%Y-%m-%d").date()
            d2 = datetime.strptime(today_str, "%Y-%m-%d").date()
            diff = (d2 - d1).days
            if diff == 1:
                current_day += 1
                if current_day > 7:
                    current_day = 1
            elif diff > 1:
                current_day = 1
            else:
                current_day = 1
        except:
            current_day = 1
    else:
        current_day = 1

    rewards_map = {
        1: 0.01,
        2: 0.02,
        3: 0.03,
        4: 0.05,
        5: 0.06,
        6: 0.07,
        7: 0.10
    }

    reward = rewards_map.get(current_day, 0.01)
    user['balance'] += reward
    user['checkin_day'] = current_day
    user['last_checkin_date'] = today_str

    save_all()
    return jsonify({"status": "success", "message": f"Day {current_day} check-in successful! ₹{reward} added.", "reward": reward, "day": current_day})

@app.route('/api/apply_promo', methods=['POST'])
def apply_promo():
    username = session.get('user')
    if not username or username not in users_db:
        return jsonify({"status": "error", "message": "Not logged in"})

    data = request.json
    code = data.get('code', '').strip().upper()

    if not code:
        return jsonify({"status": "error", "message": "Kripya promo code daalein!"})

    if code not in promo_codes_db:
        return jsonify({"status": "error", "message": "Invalid promo code!"})

    promo = promo_codes_db[code]
    user = users_db[username]

    if "used_promos" not in user:
        user["used_promos"] = []

    if code in user["used_promos"]:
        return jsonify({"status": "error", "message": "Aap is promo code ko pehle hi use kar chuke hain!"})

    max_users = promo.get("max_users")
    used_count = len(promo.get("used_by", []))

    if max_users is not None and used_count >= max_users:
        return jsonify({"status": "error", "message": "Yeh promo code ki limit khatam ho chuki hai!"})

    amount = promo.get("amount", 0.0)
    user['balance'] += amount
    user["used_promos"].append(code)
    promo["used_by"].append(username)

    save_all()
    return jsonify({"status": "success", "message": f"Promo code applied successfully! ₹{amount} added."})

@app.route('/api/withdraw', methods=['POST'])
def withdraw():
    username = session.get('user')
    if not username:
        return jsonify({"status": "error", "message": "Please log in first"})

    data = request.json
    try:
        amount = float(data.get('amount', 0))
    except:
        return jsonify({"status": "error", "message": "Invalid amount"})

    method = data.get('method')
    details = data.get('details')
    user = users_db[username]

    if amount < 10:
        return jsonify({"status": "error", "message": "Minimum withdrawal is ₹10"})

    if user['balance'] < amount:
        return jsonify({"status": "error", "message": "Insufficient balance"})

    user['balance'] -= amount
    tx_id = len(withdrawals_db) + 1
    tx_data = {
        "id": tx_id,
        "username": username,
        "amount": amount,
        "method": method,
        "details": details,
        "status": "Pending"
    }
    withdrawals_db.append(tx_data)
    save_all()

    try:
        msg = f"🔔 *New Withdrawal Request!*\n\n" \
              f"🆔 TX ID: #{tx_id}\n" \
              f"👤 User: {username}\n" \
              f"💰 Amount: ₹{amount}\n" \
              f"📱 Method: {method}\n" \
              f"📋 Details: {details}"

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"approve_{tx_id}"},
                    {"text": "❌ Reject", "callback_data": f"reject_{tx_id}"}
                ]
            ]
        }

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": ADMIN_PAYMENT_CHANNEL,
            "text": msg,
            "parse_mode": "Markdown",
            "reply_markup": keyboard
        })
    except Exception as e:
        print("Telegram error:", e)

    return jsonify({"status": "success", "message": f"Withdrawal request #{tx_id} submitted!"})

@app.route('/api/history', methods=['GET'])
def get_history():
    username = session.get('user')
    if not username:
        return jsonify({"status": "error", "message": "Not logged in"})

    user_txs = [tx for tx in withdrawals_db if tx['username'] == username]
    return jsonify({"status": "success", "history": user_txs[::-1]})

@app.route('/api/support', methods=['POST'])
def support_ticket():
    username = session.get('user')
    if not username:
        return jsonify({"status": "error", "message": "Pehle login karein!"})

    data = request.json
    problem_type = data.get('problem_type', '').strip()
    description = data.get('description', '').strip()

    if not problem_type or not description:
        return jsonify({"status": "error", "message": "Kripya category aur description dono bharein!"})

    today_str = datetime.now().strftime("%Y-%m-%d")
    user_tickets_all = [t for t in support_tickets_db if t.get('username') == username]
    today_count = sum(1 for t in user_tickets_all if t.get('date', '').startswith(today_str))

    if today_count >= 5:
        return jsonify({"status": "error", "message": "Aap ek din mein sirf 5 hi support tickets bhej sakte hain!"})

    ticket_id = len(support_tickets_db) + 1
    ticket_info = {
        "id": ticket_id,
        "username": username,
        "problem_type": problem_type,
        "description": description,
        "status": "Pending",
        "admin_reply": "",
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    support_tickets_db.append(ticket_info)

    user_tickets = [t for t in support_tickets_db if t.get('username') == username]
    if len(user_tickets) > 5:
        oldest_ticket = user_tickets[0]
        if oldest_ticket in support_tickets_db:
            support_tickets_db.remove(oldest_ticket)

    if username in users_db:
        users_db[username]["support_tickets"] = [t for t in support_tickets_db if t.get('username') == username]

    save_all()

    try:
        msg = f"🛠 *Support Ticket #{ticket_id}*\n\n" \
              f"👤 User: `{username}`\n" \
              f"📌 Category: *{problem_type}*\n\n" \
              f"💬 Message:\n{description}\n\n" \
              f"ℹ️ *(Is message ko REPLY karke jo likhenge, wo user ke dashboard par history me dikhega)*"

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": ADMIN_SUPPORT_GC,
            "text": msg,
            "parse_mode": "Markdown"
        }).json()

        if resp.get("ok"):
            sent_msg_id = resp["result"]["message_id"]
            ticket_info["telegram_msg_id"] = sent_msg_id
            save_all()
    except Exception as e:
        print("Telegram Support Error:", e)

    return jsonify({"status": "success", "message": "Aapka support ticket successfully bhej diya gaya hai!"})

def add_notification(title, message, image=""):
    global notifications_db
    new_notif = {
        "id": next_notif_id(),
        "title": title,
        "message": message,
        "image": image,
        "likes": [],
        "comments": []
    }
    notifications_db.append(new_notif)

    if len(notifications_db) > 10:
        notifications_db.pop(0)

    save_all()

@app.route('/api/notification/like', methods=['POST'])
def notification_like():
    username = session.get('user')
    if not username:
        return jsonify({"status": "error", "message": "Please log in first"})

    data = request.json
    try:
        notif_id = int(data.get('id'))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid notification"})

    for n in notifications_db:
        if n.get('id') == notif_id:
            likes = n.setdefault('likes', [])
            if username in likes:
                likes.remove(username)
                liked = False
            else:
                likes.append(username)
                liked = True
            save_all()
            return jsonify({"status": "success", "liked": liked, "like_count": len(likes)})

    return jsonify({"status": "error", "message": "Notification not found"})

@app.route('/api/notification/comment', methods=['POST'])
def notification_comment():
    username = session.get('user')
    if not username:
        return jsonify({"status": "error", "message": "Please log in first"})

    data = request.json
    try:
        notif_id = int(data.get('id'))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid notification"})

    text = data.get('text', '').strip()
    if not text:
        return jsonify({"status": "error", "message": "Comment khaali nahi ho sakta!"})
    text = text[:300]

    for n in notifications_db:
        if n.get('id') == notif_id:
            comments = n.setdefault('comments', [])
            comments.append({
                "username": username,
                "text": text,
                "date": datetime.now().strftime("%Y-%m-%d %H:%M")
            })
            if len(comments) > 100:
                comments.pop(0)
            save_all()
            return jsonify({"status": "success", "comments": comments})

    return jsonify({"status": "error", "message": "Notification not found"})

@app.route('/api/telegram_webhook', methods=['POST'])
def telegram_webhook():
    data = request.json

    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        user_id = str(msg["from"]["id"])
        text = msg.get("text", "")

        # Admin can reply to a user's message (in the points group) with /block or
        # /unblock to act on that exact user directly — takes priority over everything else.
        if user_id == ADMIN_CHAT_ID and "reply_to_message" in msg and text.strip():
            first_word = text.strip().split()[0].lower()
            if first_word in ("/block", "/unblock"):
                reply_from = msg["reply_to_message"].get("from", {})
                handle_admin_reply_command(chat_id, text, reply_from)
                return jsonify({"status": "ok"})

        if user_id == ADMIN_CHAT_ID and str(chat_id) != str(POINTS_GC_CHAT_ID):
            if text.startswith("/refreshnames"):
                updated, failed = refresh_all_names()
                send_tg_message(chat_id, f"✅ Names refresh ho gaye — {updated} updated, {failed} fetch nahi ho paaye (shayad wo user group chhod chuka hai).")
                return jsonify({"status": "ok"})

            if text.startswith("/stats"):
                total_users = len(telegram_points_db)
                linked_users = sum(1 for e in telegram_points_db.values() if e.get("username"))
                pending_points = sum(e.get("points", 0) for e in telegram_points_db.values())
                pending_value = round(pending_points * POINT_VALUE, 2)
                blocked_now = sum(1 for e in telegram_points_db.values() if time.time() < e.get("blocked_until", 0))

                stats_lines = [
                    "📊 Points System Stats",
                    f"Total Telegram users tracked: {total_users}",
                    f"Website se linked: {linked_users}",
                    f"Pending points (sab users, abhi tak redeem nahi hue): {pending_points} (~₹{pending_value})",
                    f"Currently blocked: {blocked_now}",
                    f"Redeem threshold: {POINTS_REDEEM_THRESHOLD} points = ₹{round(POINTS_REDEEM_THRESHOLD * POINT_VALUE, 2)}",
                ]
                send_tg_message(chat_id, "\n".join(stats_lines))
                return jsonify({"status": "ok"})

            if text.startswith("/check"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2 or not parts[1].strip():
                    send_tg_message(chat_id, "Sahi format use karein: /check website_username (jaise /check sarim01)")
                    return jsonify({"status": "ok"})

                query_username = parts[1].strip()
                found_uid, found_entry = find_point_entry_by_website_username(query_username)

                if not found_entry:
                    send_tg_message(chat_id, f"❌ Website username '{query_username}' se koi Telegram account link nahi mila. User ne group me pehle 'redeem' ya '/link {query_username} wali Gmail' se apna account link kiya hoga tabhi ye milega.")
                    return jsonify({"status": "ok"})

                suspicious, flags, total_logged = analyze_cheating(found_entry)
                verdict = "🚩 SUSPICIOUS — cheating ho sakti hai" if suspicious else "✅ CLEAN — normal user lagta hai"

                blocked_note = ""
                if time.time() < found_entry.get("blocked_until", 0):
                    remaining_min = round((found_entry["blocked_until"] - time.time()) / 60)
                    blocked_note = f"\n🚫 Currently BLOCKED (~{remaining_min} min baaki)"

                report_lines = [
                    f"🔍 Report: {query_username}",
                    f"Points: {found_entry.get('points', 0)}",
                    f"Telegram: @{found_entry.get('tg_username') or 'unknown'}",
                    f"Analyzed messages: {total_logged} (last redeem ke baad se sab)",
                    blocked_note,
                    "",
                    verdict,
                    ""
                ] + flags

                send_tg_message(chat_id, "\n".join(report_lines))
                return jsonify({"status": "ok"})

            if text.startswith("/block"):
                parts = text.split(maxsplit=2)
                if len(parts) < 2 or not parts[1].strip():
                    send_tg_message(chat_id, "Sahi format use karein: /block website_username [hours] (jaise /block sarim01 24 — hours optional, default 24)")
                    return jsonify({"status": "ok"})

                query_username = parts[1].strip()
                hours = 24
                if len(parts) >= 3:
                    try:
                        hours = float(parts[2].strip())
                    except ValueError:
                        hours = 24

                found_uid, found_entry = find_point_entry_by_website_username(query_username)
                if not found_entry:
                    send_tg_message(chat_id, f"❌ Website username '{query_username}' se koi Telegram account link nahi mila.")
                    return jsonify({"status": "ok"})

                found_entry["blocked_until"] = time.time() + (hours * 3600)
                save_all()
                send_tg_message(chat_id, f"🚫 {query_username} ko {hours} hours ke liye block kar diya — is dauran points count nahi honge.")
                return jsonify({"status": "ok"})

            if text.startswith("/unblock"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2 or not parts[1].strip():
                    send_tg_message(chat_id, "Sahi format use karein: /unblock website_username")
                    return jsonify({"status": "ok"})

                query_username = parts[1].strip()
                found_uid, found_entry = find_point_entry_by_website_username(query_username)
                if not found_entry:
                    send_tg_message(chat_id, f"❌ Website username '{query_username}' se koi Telegram account link nahi mila.")
                    return jsonify({"status": "ok"})

                found_entry["blocked_until"] = 0
                save_all()
                send_tg_message(chat_id, f"✅ {query_username} ka block hata diya — points ab count honge.")
                return jsonify({"status": "ok"})

            if text.startswith("/promo "):
                parts = text.split()
                if len(parts) >= 3:
                    p_code = parts[1].strip().upper()
                    try:
                        p_amount = float(parts[2])
                        p_max = int(parts[3]) if len(parts) > 3 else None

                        promo_codes_db[p_code] = {
                            "amount": p_amount,
                            "max_users": p_max,
                            "used_by": []
                        }
                        save_all()

                        limit_text = f"{p_max} users" if p_max else "Unlimited users"
                        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                        requests.post(url, json={"chat_id": chat_id, "text": f"✅ Promo Code `{p_code}` successfully create ho gaya!\nAmount: ₹{p_amount}\nLimit: {limit_text}", "parse_mode": "Markdown"})
                        return jsonify({"status": "ok"})
                    except ValueError:
                        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                        requests.post(url, json={"chat_id": chat_id, "text": "❌ Sahi format use karein: `/promo CODE AMOUNT [MAX_USERS]`"})
                        return jsonify({"status": "ok"})
                else:
                    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                    requests.post(url, json={"chat_id": chat_id, "text": "❌ Sahi format use karein: `/promo CODE AMOUNT [MAX_USERS]`"})
                    return jsonify({"status": "ok"})

            if text == "/broadcast":
                broadcast_sessions[user_id] = {"step": "TITLE"}
                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                requests.post(url, json={"chat_id": chat_id, "text": "📢 Broadcast ka **Title** kya hai?"})
                return jsonify({"status": "ok"})

            if user_id in broadcast_sessions:
                session_data = broadcast_sessions[user_id]
                if session_data["step"] == "TITLE":
                    session_data["title"] = text
                    session_data["step"] = "MESSAGE"
                    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                    requests.post(url, json={"chat_id": chat_id, "text": "✍️ Ab apna poora broadcast message likhein:"})
                    return jsonify({"status": "ok"})

                elif session_data["step"] == "MESSAGE":
                    session_data["message"] = text
                    session_data["step"] = "ASK_PHOTO"

                    keyboard = {
                        "inline_keyboard": [
                            [
                                {"text": "✅ Yes, Add Pic", "callback_data": "bc_add_pic"},
                                {"text": "❌ No, Skip", "callback_data": "bc_skip_pic"}
                            ]
                        ]
                    }
                    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                    requests.post(url, json={
                        "chat_id": chat_id,
                        "text": "📷 Kya aap is broadcast me koi photo add karna chahte hain?",
                        "reply_markup": keyboard
                    })
                    return jsonify({"status": "ok"})

                elif session_data["step"] == "WAITING_PHOTO" and "photo" in msg:
                    photo_list = msg["photo"]
                    file_id = photo_list[-1]["file_id"]

                    file_info_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile?file_id={file_id}"
                    file_resp = requests.get(file_info_url).json()
                    if file_resp.get("ok"):
                        file_path = file_resp["result"]["file_path"]
                        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
                        session_data["image"] = image_url

                    add_notification(session_data["title"], session_data["message"], session_data.get("image", ""))

                    del broadcast_sessions[user_id]
                    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                    requests.post(url, json={"chat_id": chat_id, "text": "🎉 Photo ke sath broadcast notification successfully website par bhej di gayi hai!"})
                    return jsonify({"status": "ok"})

        elif POINTS_GC_CHAT_ID and str(chat_id) == str(POINTS_GC_CHAT_ID):
            # Non-admin message inside the points-earning group chat: count points / handle point,redeem,link commands
            handle_group_points_message(user_id, chat_id, msg)
            return jsonify({"status": "ok"})

        if "reply_to_message" in msg and "text" in msg:
            replied_msg_id = msg["reply_to_message"]["message_id"]
            admin_reply_text = msg["text"]
            chat_id = msg["chat"]["id"]

            matched_ticket = None
            for ticket in support_tickets_db:
                if ticket.get("telegram_msg_id") == replied_msg_id:
                    matched_ticket = ticket
                    break

            if matched_ticket:
                matched_ticket["admin_reply"] = admin_reply_text
                matched_ticket["status"] = "Resolved"

                uname = matched_ticket.get("username")
                if uname and uname in users_db:
                    for ut in users_db[uname].get("support_tickets", []):
                        if ut.get("id") == matched_ticket.get("id"):
                            ut["admin_reply"] = admin_reply_text
                            ut["status"] = "Resolved"

                save_all()

                try:
                    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                    requests.post(url, json={
                        "chat_id": chat_id,
                        "text": f"✅ Successfully Sent to User ({uname})!",
                        "reply_to_message_id": msg["message_id"]
                    })
                except Exception as e:
                    print("GC confirmation error:", e)

    if "callback_query" in data:
        callback = data["callback_query"]
        user_id = str(callback["from"]["id"])
        data_str = callback["data"]
        msg_id = callback["message"]["message_id"]
        chat_id = callback["message"]["chat"]["id"]

        if user_id == ADMIN_CHAT_ID and data_str.startswith("bc_"):
            answer_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
            requests.post(answer_url, json={"callback_query_id": callback["id"]})

            if data_str == "bc_add_pic":
                if user_id in broadcast_sessions:
                    broadcast_sessions[user_id]["step"] = "WAITING_PHOTO"
                    edit_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
                    requests.post(edit_url, json={
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "text": "📸 Ab apni photo bhejiye:"
                    })
            elif data_str == "bc_skip_pic":
                if user_id in broadcast_sessions:
                    session_data = broadcast_sessions[user_id]
                    add_notification(session_data["title"], session_data["message"], "")

                    del broadcast_sessions[user_id]
                    edit_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
                    requests.post(edit_url, json={
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "text": "🎉 Broadcast notification successfully website par bhej di gayi hai!"
                    })
            return jsonify({"status": "ok"})

        if user_id != ADMIN_CHAT_ID:
            answer_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
            requests.post(answer_url, json={
                "callback_query_id": callback["id"],
                "text": "⚠️ Aap iske admin nahi hain!",
                "show_alert": True
            })
            return jsonify({"status": "unauthorized"}), 403

        parts = data_str.split("_")
        if len(parts) == 2:
            action = parts[0]
            try:
                tx_id = int(parts[1])
            except ValueError:
                return jsonify({"status": "invalid_id"})

            response_text = ""
            found = False
            for tx in withdrawals_db:
                if tx["id"] == tx_id:
                    found = True
                    if action == "approve":
                        if tx["status"] == "Pending":
                            tx["status"] = "Success"
                            response_text = f"✅ Withdrawal #{tx_id} Approved!"

                            ref_username = tx["username"]
                            for u_name, u_data in users_db.items():
                                if "referrals" in u_data and ref_username in u_data["referrals"]:
                                    if not u_data.get(f"bonus_claimed_{ref_username}", False):
                                        u_data["balance"] += 0.20
                                        u_data[f"bonus_claimed_{ref_username}"] = True
                        else:
                            response_text = f"⚠️ Already processed."
                    elif action == "reject":
                        if tx["status"] == "Pending":
                            tx["status"] = "Rejected"
                            if tx["username"] in users_db:
                                users_db[tx["username"]]['balance'] += tx["amount"]
                            response_text = f"❌ Rejected & Refunded."
                        else:
                            response_text = f"⚠️ Already processed."
                    break

            if found:
                save_all()
                edit_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
                original_text = callback["message"].get("text", "Withdrawal Request")
                requests.post(edit_url, json={
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": original_text + f"\n\n*Status: {response_text}*",
                    "parse_mode": "Markdown"
                })

            answer_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
            requests.post(answer_url, json={"callback_query_id": callback["id"], "text": response_text})

    return jsonify({"status": "ok"})

if __name__ == '__main__':
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
