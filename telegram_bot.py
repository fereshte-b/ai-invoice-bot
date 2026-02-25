import os
import json
import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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

client = OpenAI(api_key=OPENAI_API_KEY)

# ----------------------------
# Google Sheets
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
    if start == -1 or end == -1:
        raise ValueError("Invalid JSON")
    return text[start:end+1]

def safe_str(x: Any) -> str:
    return str(x).strip() if x is not None else ""

def to_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except:
        return None

def vat_yes_no(vat_amount: Any) -> str:
    num = to_number(vat_amount)
    if num is None:
        return "No"
    return "Yes" if num > 0 else "No"

def normalize_date(raw: Any) -> str:
    s = str(raw).strip() if raw else ""
    if not s:
        return datetime.now(timezone.utc).strftime("%Y/%m/%d")
    fmts = ["%Y-%m-%d","%Y/%m/%d","%d-%m-%Y","%d/%m/%Y","%d-%m-%y","%d/%m/%y"]
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            return dt.strftime("%Y/%m/%d")
        except:
            pass
    return datetime.now(timezone.utc).strftime("%Y/%m/%d")

# ----------------------------
# AI
# ----------------------------
def extract_with_ai(image_bytes: bytes):

    b64 = base64.b64encode(image_bytes).decode()

    prompt = """
Return ONLY JSON:

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
      "vat": "",
      "line_total": ""
    }
  ]
}
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

    date_val = normalize_date(data.get("date"))
    supplier_val = safe_str(data.get("supplier"))
    net_total_val = safe_str(data.get("net_total"))

    vat_flag = vat_yes_no(data.get("vat_amount"))
    invoice_vat_no = (vat_flag == "No")

    subcat_val = safe_str(data.get("sub_category")) or "Other"

    items = data.get("items") or []

    return {
        "date": date_val,
        "supplier": supplier_val,
        "net_total": net_total_val,
        "vat_flag": vat_flag,
        "invoice_vat_no": invoice_vat_no,
        "sub_category": subcat_val,
        "items": items,
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

        # 👇 اسم کاربر فرستنده
        user = update.effective_user
        paid_by = user.full_name if user.full_name else user.username or "Unknown"

        photo = msg.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        result = extract_with_ai(bytes(image_bytes))

        sh = get_spreadsheet()

        ws1 = get_ws(sh, "invoices")
        ws2 = get_ws(sh, "Detailed_Items")

        # Sheet 1 (added paid by column)
      ws1.append_row([
    result["date"],
    result["supplier"],
    result["net_total"],
    result["vat_flag"],
    result["sub_category"],
    result.get("items_text", ""),
    paid_by
], value_input_option="USER_ENTERED")
   
# Sheet 2 (added paid by column)
        items = result["items"]

        if items:
            for item in items:
                name = safe_str(item.get("name"))
                if not name:
                    continue

                qty = to_number(item.get("qty")) or 0
                rate = to_number(item.get("rate")) or 0
                discount = to_number(item.get("discount")) or 0

                vat_i = 0 if result["invoice_vat_no"] else (to_number(item.get("vat")) or 0)

                line_total = to_number(item.get("line_total"))
                if line_total is None:
                    total_price = (qty * rate) - discount + vat_i
                else:
                    total_price = line_total

                ws2.append_row([
                    result["date"],
                    result["supplier"],
                    name,
                    qty,
                    rate,
                    discount,
                    vat_i,
                    total_price,
                    paid_by
                ], value_input_option="USER_ENTERED")
        else:
            ws2.append_row([
                result["date"],
                result["supplier"],
                "UNREADABLE_ITEMS",
                "",
                "",
                "",
                0,
                "",
                paid_by
            ], value_input_option="USER_ENTERED")

        await msg.reply_text("با موفقیت ثبت شد")

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


