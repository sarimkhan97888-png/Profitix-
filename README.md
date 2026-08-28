# PROFITIX

## Local Setup

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # then fill in your real values in .env
python app.py
```

## Gmail se real OTP bhejne ke liye (zaroori)

Google normal Gmail password se SMTP login allow nahi karta — "App Password" banana padega:

1. https://myaccount.google.com/security kholo
2. **2-Step Verification** ON karo (agar pehle se nahi hai)
3. Usi page pe **App passwords** search karo → naya app password generate karo (koi bhi naam de do, e.g. "profitix")
4. Google 16-character password dega (spaces ke bina copy karo)
5. `.env` file me daalo:
   ```
   GMAIL_ADDRESS=youraccount@gmail.com
   GMAIL_APP_PASSWORD=abcd efgh ijkl mnop   (spaces hata dena)
   ```
6. Server restart karo — ab `/api/send_otp` real email bhejega

**Note:** Agar `.env` me Gmail credentials nahi diye, to app dev-mode me fallback karega (OTP screen pe dikhega) taaki local testing na ruke.

## GitHub par push karna

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<your-username>/<repo-name>.git
git push -u origin main
```

`.env` aur `users.json` `.gitignore` me hain — ye kabhi GitHub par push nahi honge (inme secrets/user data hai).

## Deploy (Render / Railway / PythonAnywhere)

- Start command: `gunicorn app:app`
- Environment variables wahi daalo jo `.env.example` me hain (Gmail, Telegram token, Secret key)
- `users.json` file-based storage hai — free-tier hosting par restart hone se data delete ho sakta hai. Production ke liye asli database (SQLite/PostgreSQL) use karna better rahega.

## Ads (AdSense)

AdSense script aur ad slots (`adsbygoogle`) already `templates/index.html` me lage hain (client ID: `ca-pub-5117289399435886`). Payment shuru karne ke liye:
1. https://www.google.com/adsense/ par apni live (deployed, custom domain wali) site verify/approve karwao
2. Approval ke baad wahi ad code kaam karna start kar dega, koi extra step nahi
3. Earning AdSense dashboard me track hogi, payment threshold cross hone par unka payout milega

⚠️ **Zaroori baat:** App abhi users ko real ₹ balance de raha hai (check-in, referral, promo) jo withdraw ho sakta hai — ye paisa kahin se fund hona chahiye (aapki AdSense earning se ya aap khud). AdSense per-view earning bahut choti hoti hai (paisa/cents), jabki app ₹0.10-0.20 per action de raha hai — is gap ko dhyan me rakh kar reward amounts set karna, warna payouts sustainable nahi honge.
