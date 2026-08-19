# Cashbook bot — setup

No AI API involved anywhere — extraction is local OCR (Tesseract) running on your VPS.

## 1. Supabase
1. Open your Supabase project → SQL Editor → paste and run `schema.sql`.
2. Storage → New bucket → name it exactly `cashbook-photos` → set **Private**.
   Storage → cashbook-photos → Policies → add a SELECT policy for role `anon`.
3. Project Settings → API → copy:
   - `Project URL` → used as `SUPABASE_URL` everywhere below
   - `anon public` key → goes into `dashboard.html`
   - `service_role` key → goes into the bot's `.env` (never put this one in the dashboard — it bypasses all row-level security)

There's no login on the dashboard — it connects directly with the anon key,
which is fine for a single-user personal tool. Just don't share the dashboard
URL or publish the anon key anywhere public, since anyone with both could
read/edit your entries.

## 2. Telegram bot
1. Message **@BotFather** on Telegram → `/newbot` → follow prompts → copy the token it gives you.
2. Message **@userinfobot** (or similar) to get your own numeric Telegram user ID.
   This locks the bot so only you can log entries.
3. Send your new bot one message so it registers a chat (avoids confusion on the first cron run).

## 3. Deploy the bot on your Hostinger VPS
Install the OCR engine once (system package, not pip):
```bash
sudo apt-get update && sudo apt-get install -y tesseract-ocr
```

Then the bot itself:
```bash
mkdir -p /opt/cashbook_bot && cd /opt/cashbook_bot
# upload bot.py + requirements.txt here
pip install -r requirements.txt --break-system-packages

cat > .env <<'EOF'
TELEGRAM_BOT_TOKEN=xxxx
ALLOWED_TELEGRAM_USER_ID=123456789
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_KEY=xxxx
EOF
```

Since `bot.py` reads plain `os.environ`, wrap it with `env $(cat .env | xargs)` in cron, or switch the top of the script to `python-dotenv` if you'd rather — up to you, both work fine for a cron job.

Add to crontab (`crontab -e`):
```
* * * * * cd /opt/cashbook_bot && env $(cat .env | xargs) /usr/bin/python3 bot.py >> cashbook_bot.log 2>&1
```

That polls Telegram once a minute, processes anything new, and exits — no long-running process to babysit, same pattern as BRAHMA.

## 4. Dashboard
1. Open `dashboard.html`, fill in `SUPABASE_URL` and `SUPABASE_ANON_KEY` near the top of the `<script>` block.
2. Upload it to Hostinger as a static file, same as your other sites, e.g. `cashbook.viruslabs.shop/index.html` or a subfolder.
3. Just visit it — no login.

## Using it day to day

**UPI screenshot:**
1. Send the bot the screenshot (no caption needed).
2. It OCRs it and replies with what it read — e.g. "Read ₹500 (paid)" — then asks what it's for.
3. Reply with the purpose, e.g. "lunch with client". That's saved as the note.
4. If it couldn't read the amount, it'll ask you to include it: reply like `500 paid lunch with client`.
5. If it read the direction wrong, start your reply with `paid` or `received` to override it, e.g. `received rent from tenant`.

**Cash payment (no screenshot):**
```
/cash paid 500 lunch with client
/cash received 2000 rent from tenant
```
Format is always `/cash <paid|received> <amount> <note...>`.

**Quick totals:**
`/today` or `/month` in the chat.

## OCR accuracy — what to expect
Local OCR is noticeably less reliable than a vision model at reading UPI screenshots — icons, colored backgrounds, and varied app layouts (GPay vs PhonePe vs Paytm vs bank apps) can all throw it off. In practice:
- Amount is usually read correctly since it's large, high-contrast text.
- Direction (paid/received) depends on the screenshot containing an exact phrase like "paid to" or "money received" — if the app phrases it differently, you'll need to correct it via the reply.
- Counterparty name isn't extracted at all (too unreliable without a vision model) — just include it in your note if you want it recorded.
- UPI reference number extraction is a best-effort regex guess and may be blank or wrong.

`raw_extraction` (including the raw OCR text) is stored per row in Supabase, so you can always see exactly what it read and hand-correct via the dashboard if something's off.

## Notes / things to adjust later
- `category` is free text you fill in from the dashboard — nothing auto-categorizes.
- The bot only accepts messages from `ALLOWED_TELEGRAM_USER_ID`, so it's safe even if someone else finds your bot's @handle.
- If you send two screenshots back-to-back before replying to either, the bot matches your next reply to the *oldest* unanswered one (FIFO) — answer them in the order you sent them.
- Dashboard lets you edit note/category inline, view the original receipt, delete bad entries, and export a month as CSV.
