"""
Cashbook Telegram bot (v3 — no AI API, local OCR only).

Meant to run every minute via cron (short-lived process, not a long-running poller):
    * * * * * /usr/bin/python3 /path/to/bot.py >> /path/to/cashbook_bot.log 2>&1

Extraction uses Tesseract OCR (local, free) + regex/keyword parsing instead of
a vision model. It's less reliable than an AI model at reading stylized UPI
screenshots, so:
  - If it can't find an amount, it asks you to include it in your reply.
  - If it guesses direction (paid/received) wrong, you can correct it by
    starting your reply with "paid" or "received".

Flow:
  - Send a UPI screenshot -> bot OCRs it, holds a pending entry, and asks
    what it was for (and for the amount too, if it couldn't read it).
  - Reply -> bot finalizes the pending entry. Reply format:
        <amount optional> <paid|received optional> <note>
    e.g. "500 paid lunch with client" or just "lunch with client" if the
    amount/direction were already read correctly.
  - No screenshot (cash payment)? Send:
        /cash paid 500 lunch with client
        /cash received 2000 rent from tenant
  - /today or /month -> quick spoken summary.
  - After a payment is logged, the bot asks "Do you have a bill for this?"
    (Yes / No buttons). Yes -> send a photo or PDF of the bill and it is
    stored in the cashbook-photos bucket under bills/, linked to that entry
    through cashbook_entries.bill_path. No -> nothing more is asked.

State (offset, pending entries) lives in Supabase, so cron runs stay stateless
on disk.

Required environment variables (put these in a .env file, see README.md):
  TELEGRAM_BOT_TOKEN
  ALLOWED_TELEGRAM_USER_ID   (your own Telegram numeric user id)
  SUPABASE_URL
  SUPABASE_SERVICE_KEY       (service_role key, NOT the anon key)

System dependency (install once on the VPS):
  sudo apt-get install -y tesseract-ocr
"""

import os
import re
import io
import uuid
from datetime import date

import requests
from PIL import Image
import pytesseract

# ── config ──────────────────────────────────────────────────────────
# Read from the environment (.env on the VPS, repo secrets on GitHub Actions).
# Never hard-code these: the service key has full access to the database.
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = os.environ["ALLOWED_TELEGRAM_USER_ID"].strip()
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
PHOTO_BUCKET = "cashbook-photos"

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SB_HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
}

CASH_RE = re.compile(r"^/cash\s+(paid|received)\s+([\d.]+)\s+(.+)$", re.IGNORECASE | re.DOTALL)
REPLY_RE = re.compile(
    r"^\s*(?:(?:₹|rs\.?|inr)\s*)?(?:(?P<amount>[\d,]+(?:\.\d{1,2})?)\s+)?"
    r"(?:(?P<direction>paid|received)\s+)?(?P<note>.*)$",
    re.IGNORECASE | re.DOTALL,
)

AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
PAID_KEYWORDS = ["paid to", "you paid", "money sent", "sent to", "debited", "payment successful"]
RECEIVED_KEYWORDS = ["received from", "money received", "credited", "payment received"]
APP_KEYWORDS = {
    "Google Pay": ["google pay", "gpay"],
    "PhonePe": ["phonepe"],
    "Paytm": ["paytm"],
    "BHIM": ["bhim"],
    "Amazon Pay": ["amazon pay"],
}


# ── supabase REST helpers (PostgREST, no SDK needed) ──────────────────
def sb_get(path, params=None):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=SB_HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def sb_post(path, payload):
    headers = {**SB_HEADERS, "Prefer": "return=representation"}
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{path}", headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def sb_patch(path, payload, params):
    headers = {**SB_HEADERS, "Prefer": "return=representation"}
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{path}", headers=headers, json=payload, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def sb_delete(path, params):
    r = requests.delete(f"{SUPABASE_URL}/rest/v1/{path}", headers=SB_HEADERS, params=params, timeout=30)
    r.raise_for_status()


def get_last_offset():
    rows = sb_get("bot_state", params={"key": "eq.last_update_id", "select": "value"})
    return int(rows[0]["value"]) if rows else 0


def set_last_offset(offset):
    existing = sb_get("bot_state", params={"key": "eq.last_update_id"})
    if existing:
        sb_patch("bot_state", {"value": str(offset)}, params={"key": "eq.last_update_id"})
    else:
        sb_post("bot_state", {"key": "last_update_id", "value": str(offset)})


CONTENT_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "pdf": "application/pdf"}


def upload_photo(content, ext, folder=""):
    """Upload a file to the storage bucket. Returns its path inside the bucket."""
    ext = (ext or "jpg").lower()
    filename = f"{folder}{uuid.uuid4()}.{ext}"
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/{PHOTO_BUCKET}/{filename}",
        headers={
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": CONTENT_TYPES.get(ext, "application/octet-stream"),
        },
        data=content,
        timeout=60,
    )
    r.raise_for_status()
    return filename


# ── bill follow-up state ─────────────────────────────────────────────
# After an entry is logged the bot asks "Do you have a bill for this?".
# What it is waiting for lives in bot_state under bill_wait:<chat_id>,
# as "<stage>:<entry id>", where stage is "ask" (waiting for yes/no)
# or "photo" (waiting for the bill photo).
def _bill_key(chat_id):
    return f"bill_wait:{chat_id}"


def get_bill_wait(chat_id):
    rows = sb_get("bot_state", params={"key": f"eq.{_bill_key(chat_id)}", "select": "value"})
    if not rows or not rows[0].get("value"):
        return None
    stage, _, entry_id = rows[0]["value"].partition(":")
    return {"stage": stage, "entry_id": entry_id}


def set_bill_wait(chat_id, stage, entry_id):
    key = _bill_key(chat_id)
    value = f"{stage}:{entry_id}"
    if sb_get("bot_state", params={"key": f"eq.{key}"}):
        sb_patch("bot_state", {"value": value}, params={"key": f"eq.{key}"})
    else:
        sb_post("bot_state", {"key": key, "value": value})


def clear_bill_wait(chat_id):
    sb_delete("bot_state", params={"key": f"eq.{_bill_key(chat_id)}"})


def get_oldest_pending(chat_id):
    rows = sb_get(
        "pending_entries",
        params={"chat_id": f"eq.{chat_id}", "order": "created_at.asc", "limit": 1},
    )
    return rows[0] if rows else None


def delete_pending(entry_id):
    sb_delete("pending_entries", params={"id": f"eq.{entry_id}"})


# ── telegram helpers ────────────────────────────────────────────────
def tg_get_updates(offset):
    r = requests.get(f"{TG_API}/getUpdates", params={"offset": offset, "timeout": 0}, timeout=30)
    r.raise_for_status()
    return r.json().get("result", [])


YES_NO_KEYBOARD = {
    "keyboard": [[{"text": "Yes"}, {"text": "No"}]],
    "one_time_keyboard": True,
    "resize_keyboard": True,
}
REMOVE_KEYBOARD = {"remove_keyboard": True}


def tg_send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    requests.post(f"{TG_API}/sendMessage", json=payload, timeout=30)


def tg_download_file(file_id):
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30)
    r.raise_for_status()
    file_path = r.json()["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    content = requests.get(file_url, timeout=30).content
    ext = file_path.split(".")[-1] if "." in file_path else "jpg"
    return content, ext


# ── local OCR extraction (no API) ──────────────────────────────────
def ocr_text(content):
    img = Image.open(io.BytesIO(content))
    if img.mode != "RGB":
        img = img.convert("RGB")
    return pytesseract.image_to_string(img)


def find_ref(text):
    for line in text.splitlines():
        if re.search(r"(ref|txn|transaction|utr)", line, re.IGNORECASE):
            m = re.search(r"\d{8,18}", line)
            if m:
                return m.group(0)
    m = re.search(r"\b\d{10,18}\b", text)
    return m.group(0) if m else None


def extract_from_text(text):
    lower = text.lower()

    amount = None
    m = AMOUNT_RE.search(text)
    if m:
        amount = float(m.group(1).replace(",", ""))

    direction = None
    for kw in PAID_KEYWORDS:
        if kw in lower:
            direction = "paid"
            break
    if not direction:
        for kw in RECEIVED_KEYWORDS:
            if kw in lower:
                direction = "received"
                break

    app = "Unknown"
    for name, kws in APP_KEYWORDS.items():
        if any(kw in lower for kw in kws):
            app = name
            break

    return {
        "amount": amount,
        "direction": direction,
        "upi_ref": find_ref(text),
        "counterparty": None,  # not reliable to pull from OCR text without AI, left blank
        "payment_app": app,
        "ocr_text": text[:1500],
    }


# ── reply parsing (amount/direction/note overrides from your message) ─
def parse_reply(text, extracted):
    m = REPLY_RE.match(text.strip())
    amount = extracted.get("amount")
    direction = extracted.get("direction")
    note = text.strip()

    if m:
        if m.group("amount"):
            amount = float(m.group("amount").replace(",", ""))
        if m.group("direction"):
            direction = m.group("direction").lower()
        note = (m.group("note") or "").strip()

    return amount, direction, note


# ── summaries ───────────────────────────────────────────────────────
def handle_summary_command(chat_id, cmd):
    today = date.today()
    if cmd == "/today":
        gte = today.isoformat()
        rows = sb_get("cashbook_entries", params={"transaction_date": f"eq.{gte}", "select": "amount,direction"})
        label = "Today"
    else:  # /month
        gte = today.replace(day=1).isoformat()
        rows = sb_get("cashbook_entries", params={"transaction_date": f"gte.{gte}", "select": "amount,direction"})
        label = "This month"

    paid = sum(r["amount"] or 0 for r in rows if r["direction"] == "paid")
    received = sum(r["amount"] or 0 for r in rows if r["direction"] == "received")
    tg_send_message(
        chat_id,
        f"{label}: {len(rows)} entries\nPaid: ₹{paid:,.2f}\nReceived: ₹{received:,.2f}\nNet: ₹{received - paid:,.2f}",
    )


# ── core handlers ───────────────────────────────────────────────────
def handle_photo(msg, chat_id, user_id):
    try:
        photo = msg["photo"][-1]  # largest resolution
        content, ext = tg_download_file(photo["file_id"])
        filename = upload_photo(content, ext)
        text = ocr_text(content)
        extracted = extract_from_text(text)

        sb_post("pending_entries", {
            "chat_id": chat_id,
            "telegram_user_id": user_id,
            "extracted": extracted,
            "photo_path": filename,
        })

        if extracted["amount"] is not None:
            direction = extracted["direction"] or "unclear direction"
            tg_send_message(
                chat_id,
                f"Read ₹{extracted['amount']:g} ({direction})\n"
                f"What's this for? (start your reply with 'paid' or 'received' if I got that wrong)",
            )
        else:
            tg_send_message(
                chat_id,
                "Couldn't read the amount from that screenshot.\n"
                "Reply like: 500 paid lunch with client",
            )
    except Exception as e:  # noqa: BLE001 - cron job, keep going next run
        tg_send_message(chat_id, f"Couldn't process that screenshot. Try /cash instead for this one.\n({e})")


def finalize_pending(pending, reply_text):
    extracted = pending["extracted"] or {}
    amount, direction, note = parse_reply(reply_text, extracted)

    if amount is None:
        return None  # still missing amount, caller should re-ask and keep pending

    row = {
        "amount": amount,
        "direction": direction or "paid",
        "upi_ref": extracted.get("upi_ref"),
        "payee_name": extracted.get("counterparty"),
        "payment_app": extracted.get("payment_app"),
        "transaction_date": date.today().isoformat(),
        "note": note,
        "photo_path": pending["photo_path"],
        "telegram_user_id": pending["telegram_user_id"],
        "raw_extraction": extracted,
    }
    inserted = sb_post("cashbook_entries", row)
    row["id"] = inserted[0]["id"] if inserted else None
    delete_pending(pending["id"])
    return row


def handle_cash_command(chat_id, user_id, text):
    m = CASH_RE.match(text.strip())
    if not m:
        tg_send_message(chat_id, "Format: /cash paid 500 lunch with client\nor: /cash received 2000 rent from tenant")
        return
    direction, amount_str, note = m.groups()
    row = {
        "amount": float(amount_str),
        "direction": direction.lower(),
        "note": note.strip(),
        "transaction_date": date.today().isoformat(),
        "payment_app": "cash",
        "telegram_user_id": user_id,
    }
    inserted = sb_post("cashbook_entries", row)
    tg_send_message(chat_id, f"Logged ₹{amount_str} ({direction}, cash) — {note.strip()}")
    if inserted and direction.lower() == "paid":
        ask_for_bill(chat_id, inserted[0]["id"])


# ── bills ───────────────────────────────────────────────────────────
YES_WORDS = {"yes", "y", "haan", "ha", "han"}
NO_WORDS = {"no", "n", "nahi", "skip", "cancel"}


def ask_for_bill(chat_id, entry_id):
    set_bill_wait(chat_id, "ask", entry_id)
    tg_send_message(chat_id, "Do you have a bill for this?", reply_markup=YES_NO_KEYBOARD)


def bill_file_from_message(msg):
    """Return the Telegram file_id of a bill photo, image file or PDF in msg, or None."""
    if "photo" in msg:
        return msg["photo"][-1]["file_id"]  # largest resolution
    doc = msg.get("document")
    if doc:
        mime = doc.get("mime_type") or ""
        if mime.startswith("image/") or mime == "application/pdf":
            return doc["file_id"]
    return None


def save_bill(chat_id, entry_id, file_id):
    content, ext = tg_download_file(file_id)
    path = upload_photo(content, ext, folder="bills/")
    sb_patch("cashbook_entries", {"bill_path": path}, params={"id": f"eq.{entry_id}"})
    clear_bill_wait(chat_id)
    tg_send_message(chat_id, "Bill saved with this entry.", reply_markup=REMOVE_KEYBOARD)


def handle_bill_flow(msg, chat_id, wait):
    """Handle a message while a bill question is open. Returns True if it used the message."""
    text = (msg.get("text") or "").strip()
    word = text.lower().rstrip(".!")

    if word in NO_WORDS:
        clear_bill_wait(chat_id)
        tg_send_message(chat_id, "OK, no bill for this one.", reply_markup=REMOVE_KEYBOARD)
        return True

    if wait["stage"] == "ask":
        if word in YES_WORDS:
            set_bill_wait(chat_id, "photo", wait["entry_id"])
            tg_send_message(
                chat_id,
                "Send a photo of the bill (a PDF works too). Reply No to skip.",
                reply_markup=REMOVE_KEYBOARD,
            )
            return True
        # Anything else (a new screenshot, /cash, a reply to another entry)
        # means you've moved on: drop the question and handle it normally.
        clear_bill_wait(chat_id)
        return False

    # stage == "photo": the next photo or PDF is the bill.
    file_id = bill_file_from_message(msg)
    if file_id:
        try:
            save_bill(chat_id, wait["entry_id"], file_id)
        except Exception as e:  # noqa: BLE001 - cron job, keep going next run
            tg_send_message(chat_id, f"Couldn't save that bill, try sending it again.\n({e})")
        return True
    if text.startswith("/"):
        clear_bill_wait(chat_id)
        return False
    tg_send_message(chat_id, "Waiting for the bill photo. Send it now, or reply No to skip.")
    return True


def handle_text(msg, chat_id, user_id, text):
    if text.lower().startswith("/cash"):
        handle_cash_command(chat_id, user_id, text)
        return

    if text in ("/today", "/month"):
        handle_summary_command(chat_id, text)
        return

    pending = get_oldest_pending(chat_id)
    if pending:
        row = finalize_pending(pending, text)
        if row is None:
            tg_send_message(chat_id, "Still need an amount. Reply like: 500 paid lunch with client")
            return
        tg_send_message(chat_id, f"Logged ₹{row['amount']:g} ({row['direction']}) — {row['note']}")
        if row.get("id") and row["direction"] == "paid":
            ask_for_bill(chat_id, row["id"])
        return

    tg_send_message(
        chat_id,
        "Send a UPI screenshot to log a payment, or for cash use:\n/cash paid 500 lunch with client",
    )


def process_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    if str(user_id) != ALLOWED_USER_ID:  # Telegram sends the id as a number
        return  # ignore anyone who isn't you

    wait = get_bill_wait(chat_id)
    if wait and handle_bill_flow(msg, chat_id, wait):
        return

    if "photo" in msg:
        handle_photo(msg, chat_id, user_id)
        return

    text = msg.get("text", "")
    if text:
        handle_text(msg, chat_id, user_id, text)


def main():
    offset = get_last_offset()
    updates = tg_get_updates(offset + 1)
    if not updates:
        return
    for update in updates:
        if "message" in update:
            process_message(update["message"])
        offset = update["update_id"]
    set_last_offset(offset)


if __name__ == "__main__":
    main()
