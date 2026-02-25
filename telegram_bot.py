import os
import json
import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
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
# Google Sheet
# ----------------------------
def _load_service_account_info():
    if GOOGLE_SERVICE_ACCOUNT_JSON:
        return json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    decoded = base64.b64decode(GOOGLE_SERVICE_ACCOUNT_JSON_B64).decode()
    return json.loads(decoded)


def get_spreadsheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(
        _load_service_account_info(), scopes=scopes
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(GOOGLE_SHEET_ID)


def get_ws(sh, name):
    return sh.worksheet(name)


def ensure_header(ws, header):
    first_row = ws.row_values(1)
    if first_row != header:
        ws.update(f"A1:{chr(64+len(header))}1", [header])


# ----------------------------
# AI Extraction
# ----------------------------
def clean_json_only(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Invalid JSON from model")
    return text[start : end + 1]


def extract_with_ai(image_bytes: bytes):
    b64 = base64.b64encode(image_bytes).decode()

    prompt = """
Extract invoice data and return ONLY JSON:

{
  "date": "",
  "supplier": "",
  "net_total": "",
  "vat_amount": null,
  "sub_category": "",
  "items": [
    {
      "name": "",
      "qty": "",
      "rate": "",
      "discount": "",
      "vat": ""
    }
  ]
}

Rules:
- net_total = final payable amount.
- vat_amount = total VAT for invoice (null if none).
- items: include per-item discount and vat if shown, else "".
- Return JSON only.
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

    date_val = data.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    supplier_val = data.get("supplier", "")
    net_total = data.get("net_total", "")
    vat_amount = data.get("vat_amount")
    subcat = data.get("sub_category", "Other")

    vat_flag = "Yes" if vat_amount else "No"

    items = data.get("items") or []

    return {
        "summary": [date_val, supplier_val, net_total, vat_flag, subcat],
        "items": items,
        "date": date_val,
        "supplier": supplier_val,
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

        # -------- Sheet 1 (Invoices) --------
        ws1 = get_or_create_ws(sh, "Invoices")
        header1 = ["Date", "Supplier", "Net Total", "VAT", "Sub-Category"]
        ensure_header(ws1, header1)
        ws1.append_row(result["summary"], value_input_option="USER_ENTERED")

        # -------- Sheet 2 (Detailed_Items) --------
        ws2 = get_or_create_ws(sh, "Detailed_Items")
        header2 = [
            "date",
            "supplier",
            "product description",
            "quantity",
            "rate",
            "discount",
            "vat",
            "total price",
        ]
        ensure_header(ws2, header2)

        for item in result["items"]:
            name = str(item.get("name", "")).strip()
            qty = float(item.get("qty") or 0)
            rate = float(item.get("rate") or 0)
            discount = float(item.get("discount") or 0)
            vat = float(item.get("vat") or 0)

            total_price = (qty * rate) - discount + vat

            row = [
                result["date"],
                result["supplier"],
                name,
                qty,
                rate,
                discount,
                vat,
                total_price,
            ]

            ws2.append_row(row, value_input_option="USER_ENTERED")

        await msg.reply_text("✅ ثبت شد در ai invoices (هر دو شیت).")

    except Exception as e:
        logger.exception("Error")
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

