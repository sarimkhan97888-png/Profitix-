from flask import Flask, render_template, request, jsonify, session, redirect
import random
import string
import json
import os
import requests
import smtplib
import ssl
from email.mime.text import MIMEText
from datetime import datetime, date, timedelta
from dotenv import load_dotenv

load_dotenv()  # reads .env file if present (local development)

app = Flask(__name__, template_folder=".")
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

DB_FILE = "users.json"

def load_data():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    if "users" not in data:
                        data = {"users": data, "withdrawals": [], "support_tickets": [], "notifications": [], "promo_codes": {}}
                    if "support_tickets" not in data:
                        data["support_tickets"] = []
                    if "notifications" not in data:
                        data["notifications"] = []
                    if "promo_codes" not in data:
                        data["promo_codes"] = {}
                    return data
        except:
            return {"users": {}, "withdrawals": [], "support_tickets": [], "notifications": [], "promo_codes": {}}
    return {"users": {}, "withdrawals": [], "support_tickets": [], "notifications": [], "promo_codes": {}}

def save_data(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

db = load_data()
users_db = db.get("users", {})
withdrawals_db = db.get("withdrawals", [])
support_tickets_db = db.get("support_tickets", [])
notifications_db = db.get("notifications", [])
promo_codes_db = db.get("promo_codes", {})
otp_db = {}
broadcast_sessions = {}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
ADMIN_SUPPORT_GC = os.environ.get("ADMIN_SUPPORT_GC", "")
ADMIN_PAYMENT_CHANNEL = os.environ.get("ADMIN_PAYMENT_CHANNEL", "@PROFITIX77")

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

    save_data({"users": users_db, "withdrawals": withdrawals_db, "support_tickets": support_tickets_db, "notifications": notifications_db, "promo_codes": promo_codes_db})

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
        save_data({"users": users_db, "withdrawals": withdrawals_db, "support_tickets": support_tickets_db, "notifications": notifications_db, "promo_codes": promo_codes_db})

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

    return jsonify({
        "status": "success",
        "balance": user['balance'],
        "referral_code": user['referral_code'],
        "total_referrals": len(user['referrals']),
        "referral_history": referral_details,
        "support_tickets": [t for t in support_tickets_db if t.get('username') == username],
        "notifications": notifications_db,
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

    save_data({"users": users_db, "withdrawals": withdrawals_db, "support_tickets": support_tickets_db, "notifications": notifications_db, "promo_codes": promo_codes_db})
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

    save_data({"users": users_db, "withdrawals": withdrawals_db, "support_tickets": support_tickets_db, "notifications": notifications_db, "promo_codes": promo_codes_db})
    return jsonify({"status": "success", "message": f"Promo code applied successfully! ₹{amount} added."})

@app.route('/api/get_captcha', methods=['GET'])
def get_captcha():
    num1 = random.randint(1, 9)
    num2 = random.randint(1, 9)
    session['captcha_ans'] = num1 + num2
    return jsonify({"num1": num1, "num2": num2})

@app.route('/api/verify_captcha', methods=['POST'])
def verify_captcha():
    data = request.json
    try:
        ans = int(str(data.get('answer', '')).strip())
    except ValueError:
        return jsonify({"status": "error", "message": "Invalid input"})

    if 'captcha_ans' not in session:
        return jsonify({"status": "error", "message": "Captcha expired!"})

    if ans == session.get('captcha_ans'):
        session['captcha_verified'] = True
        return jsonify({"status": "success"})

    return jsonify({"status": "error", "message": "Wrong captcha answer"})

@app.route('/api/claim_reward', methods=['POST'])
def claim_reward():
    username = session.get('user')
    if not username:
        return jsonify({"status": "error", "message": "Not logged in"})

    if not session.get('captcha_verified'):
        return jsonify({"status": "error", "message": "Please solve captcha first"})

    users_db[username]['balance'] += 0.10
    save_data({"users": users_db, "withdrawals": withdrawals_db, "support_tickets": support_tickets_db, "notifications": notifications_db, "promo_codes": promo_codes_db})
    session['captcha_verified'] = False

    return jsonify({"status": "success", "message": "₹0.10 added to balance!"})

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
    save_data({"users": users_db, "withdrawals": withdrawals_db, "support_tickets": support_tickets_db, "notifications": notifications_db, "promo_codes": promo_codes_db})

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

    save_data({"users": users_db, "withdrawals": withdrawals_db, "support_tickets": support_tickets_db, "notifications": notifications_db, "promo_codes": promo_codes_db})

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
            save_data({"users": users_db, "withdrawals": withdrawals_db, "support_tickets": support_tickets_db, "notifications": notifications_db, "promo_codes": promo_codes_db})
    except Exception as e:
        print("Telegram Support Error:", e)

    return jsonify({"status": "success", "message": "Aapka support ticket successfully bhej diya gaya hai!"})

def add_notification(title, message, image=""):
    global notifications_db
    new_notif = {
        "title": title,
        "message": message,
        "image": image
    }
    notifications_db.append(new_notif)

    if len(notifications_db) > 10:
        notifications_db.pop(0)

    save_data({"users": users_db, "withdrawals": withdrawals_db, "support_tickets": support_tickets_db, "notifications": notifications_db, "promo_codes": promo_codes_db})

@app.route('/api/telegram_webhook', methods=['POST'])
def telegram_webhook():
    data = request.json

    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        user_id = str(msg["from"]["id"])
        text = msg.get("text", "")

        if user_id == ADMIN_CHAT_ID:
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
                        save_data({"users": users_db, "withdrawals": withdrawals_db, "support_tickets": support_tickets_db, "notifications": notifications_db, "promo_codes": promo_codes_db})

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

                save_data({"users": users_db, "withdrawals": withdrawals_db, "support_tickets": support_tickets_db, "notifications": notifications_db, "promo_codes": promo_codes_db})

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
                save_data({"users": users_db, "withdrawals": withdrawals_db, "support_tickets": support_tickets_db, "notifications": notifications_db, "promo_codes": promo_codes_db})
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
