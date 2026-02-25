import os
import json
import base64
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any

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
        decoded = base64.b64decode(GOOGLE_SERVICE_ACCOUNT_JSON_B64).decode()
        sa_info = json.loads(decoded)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    gc = gspread.authorize(creds)

    return gc.open_by_key(GOOGLE_SHEET_ID)


def get_ws(sh, name):
    # فقط بگیر، نساز
    return sh.worksheet(name)


# ----------------------------
# AI Extraction
# ----------------------------
def clean_json_only(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Model did not return valid JSON.")
    return text[start:end+1]


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
- vat_amount = total VAT of invoice (null if none).
- If per-item discount or vat not visible, return "".
- Return JSON only.
"""

    resp = client.responses.create(
        model="gpt-4o-mini",
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{b64}",
                    },
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

    return {
        "summary_row": [
            date_val,
            supplier_val,
            net_total,
            vat_flag,
            subcat,
        ],
        "items": data.get("items", []),
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

        # -------- Invoices (Sheet 1)
        ws1 = get_ws(sh, "Invoices")
        ws1.append_row(result["summary_row"], value_input_option="USER_ENTERED")

        # -------- Detailed_Items (Sheet 2)
        ws2 = get_ws(sh, "Detailed_Items")

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

        await msg.reply_text("✅ ثبت شد در AI invoices (هر دو شیت).")

    except Exception as e:
        logger.exception("Error processing invoice")
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
