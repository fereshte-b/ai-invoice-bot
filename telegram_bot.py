import os
import json
import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
)

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("ai-invoice-bot")

# ----------------------------
# ENV
# ----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SERVICE_ACCOUNT_JSON_B64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY not set")
if not GOOGLE_SHEET_ID:
    raise ValueError("GOOGLE_SHEET_ID not set")
if not (GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_JSON_B64):
    raise ValueError("Google service account not set")

client = OpenAI(api_key=OPENAI_API_KEY)

# ----------------------------
# Google Sheets (NO creation)
# ----------------------------
def get_spreadsheet():
    if GOOGLE_SERVICE_ACCOUNT_JSON:
        sa_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    else:
        decoded = base64.b64decode(GOOGLE_SERVICE_ACCOUNT_JSON_B64).decode("utf-8")
        sa_info = json.loads(decoded)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(GOOGLE_SHEET_ID)

def get_ws(sh, name: str):
    return sh.worksheet(name)

# ----------------------------
# Helpers
# ----------------------------
def clean_json_only(text: str) -> str:
    text = (text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Model did not return valid JSON.")
    return text[start:end+1]

def to_number(value: Any) -> Optional[float]:
    """
    Converts '0', '0.000', ' 3,960 ' to float.
    Returns None if empty/invalid.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s.lower() in {"null", "none", "nan"}:
        return None
    s = s.replace(",", "")
    try:
        return float(s)
    except Exception:
        return None

def vat_yes_no(vat_amount: Any) -> str:
    """
    Yes فقط وقتی VAT واقعاً > 0 باشد.
    """
    num = to_number(vat_amount)
    if num is None:
        return "No"
    return "Yes" if num > 0 else "No"

def safe_str(x: Any) -> str:
    return str(x).strip() if x is not None else ""

# ----------------------------
# AI Extraction
# ----------------------------
def extract_with_ai(image_bytes: bytes) -> Dict[str, Any]:
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    prompt = """
You are extracting structured data from a TAX INVOICE image.

Return ONLY JSON in this exact schema:

{
  "date": "",
  "supplier": "",
  "net_total": "",
  "vat_amount": null,
  "sub_category": "",
  "items": [
    { "name": "", "qty": "", "rate": "", "discount": "", "vat": "" }
  ]
}

Rules (important):
- date: invoice date. If missing, empty string.
- supplier: store/company name (seller). If unclear, empty string.
- net_total: FINAL payable amount (grand total / net total). If unclear, empty string.
- vat_amount: TOTAL VAT amount for the invoice.
  - If VAT is not present OR shown as 0.000, return null (NOT "0").
- sub_category must be one of:
  Gas, Grocery, Restaurant, Office Supplies, Utilities, Transport, Maintenance, Other
  If uncertain or missing, return "Other".
- items:
  - Extract each line item row from the table.
  - qty = quantity
  - rate = unit price
  - discount = discount for that line (0 if not shown)
  - vat = VAT for that line (0 if not shown)
  - If you cannot see line items, return an empty list [] (not dummy items).
Return JSON only. No extra text.
"""

    resp = client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"},
                ],
            }
        ],
        temperature=0,
    )

    raw = resp.output_text or ""
    data = json.loads(clean_json_only(raw))

    # ---- Normalize fields ----
    date_val = safe_str(data.get("date"))
    if not date_val:
        date_val = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    supplier_val = safe_str(data.get("supplier"))
    net_total_val = safe_str(data.get("net_total"))

    subcat_val = safe_str(data.get("sub_category")) or "Other"
    allowed = {"Gas","Grocery","Restaurant","Office Supplies","Utilities","Transport","Maintenance","Other"}
    if subcat_val not in allowed:
        subcat_val = "Other"

    vat_flag = vat_yes_no(data.get("vat_amount"))

    items = data.get("items") or []
    if not isinstance(items, list):
        items = []

    # Build items text for Sheet1 (so Items column doesn't stay empty if items exist)
    items_text_lines: List[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = safe_str(it.get("name"))
        qty = safe_str(it.get("qty"))
        rate = safe_str(it.get("rate"))
        disc = safe_str(it.get("discount"))
        vat_i = safe_str(it.get("vat"))
        if not (name or qty or rate or disc or vat_i):
            continue
        # Nice compact line
        # ex: Chicken Wings | qty 6 | rate 0.660 | disc 0 | vat 0
        parts = []
        if name: parts.append(name)
        if qty: parts.append(f"qty {qty}")
        if rate: parts.append(f"rate {rate}")
        if disc: parts.append(f"disc {disc}")
        if vat_i: parts.append(f"vat {vat_i}")
        items_text_lines.append(" | ".join(parts))

    items_text = "\n".join(items_text_lines).strip()

    return {
        "date": date_val,
        "supplier": supplier_val,
        "net_total": net_total_val,
        "vat_flag": vat_flag,
        "sub_category": subcat_val,
        "items": items,
        "items_text": items_text,
    }

# ----------------------------
# Telegram
# ----------------------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.photo:
        return

    try:
        await msg.chat.send_action(ChatAction.TYPING)

        photo = msg.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        result = extract_with_ai(bytes(image_bytes))

        sh = get_spreadsheet()

        # ✅ IMPORTANT: your sheet tab is "invoices" (lowercase) based on your screenshot
        ws1 = get_ws(sh, "invoices")
        ws2 = get_ws(sh, "Detailed_Items")

        # Sheet1 row (Date, Supplier, Net Total, VAT, Sub-Category, Items)
        row1 = [
            result["date"],
            result["supplier"],
            result["net_total"],
            result["vat_flag"],
            result["sub_category"],
            result["items_text"],
        ]
        ws1.append_row(row1, value_input_option="USER_ENTERED")

        # Sheet2 rows per item
        items: List[Dict[str, Any]] = result["items"]

        # اگر هیچ آیتمی دیده نشد، شیت دوم رو خالی می‌ذاریم (تا داده اشتباه وارد نشه)
        if items:
            for item in items:
                if not isinstance(item, dict):
                    continue

                name = safe_str(item.get("name"))
                qty = to_number(item.get("qty")) or 0.0
                rate = to_number(item.get("rate")) or 0.0
                discount = to_number(item.get("discount")) or 0.0
                vat_i = to_number(item.get("vat")) or 0.0

                # اگر name هم خالیه، رد کن
                if not name:
                    continue

                total_price = (qty * rate) - discount + vat_i

                row2 = [
                    result["date"],
                    result["supplier"],
                    name,
                    qty,
                    rate,
                    discount,
                    vat_i,
                    total_price,
                ]
                ws2.append_row(row2, value_input_option="USER_ENTERED")

        await msg.reply_text("✅ ثبت شد (VAT دقیق‌تر + Category/Items اصلاح شد).")

    except Exception as e:
        logger.exception("Error processing invoice")
        # پیام خطا کوتاه و مفید
        await msg.reply_text(f"❌ خطا: {e}")

# ----------------------------
# Main
# ----------------------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
