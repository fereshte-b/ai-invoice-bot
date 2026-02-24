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
# ENV Vars
# ----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")  # Spreadsheet ID
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Invoices")  # Worksheet name

# Service account JSON:
# Either set GOOGLE_SERVICE_ACCOUNT_JSON (raw json)
# or GOOGLE_SERVICE_ACCOUNT_JSON_B64 (base64 of json)
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SERVICE_ACCOUNT_JSON_B64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64")

# Optional: force model
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# ----------------------------
# Validate required envs
# ----------------------------
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set")

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY not set")

if not GOOGLE_SHEET_ID:
    raise ValueError("GOOGLE_SHEET_ID not set")

if not (GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_JSON_B64):
    raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_JSON_B64 not set")


# ----------------------------
# Clients
# ----------------------------
client = OpenAI(api_key=OPENAI_API_KEY)


def _load_service_account_info() -> Dict[str, Any]:
    if GOOGLE_SERVICE_ACCOUNT_JSON:
        return json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)

    # else b64
    decoded = base64.b64decode(GOOGLE_SERVICE_ACCOUNT_JSON_B64.encode("utf-8")).decode("utf-8")
    return json.loads(decoded)


def get_sheet():
    """
    Opens the Google Sheet and returns the worksheet.
    """
    sa_info = _load_service_account_info()
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(GOOGLE_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=GOOGLE_SHEET_NAME, rows=1000, cols=20)
    return ws


def ensure_header(ws):
    """
    Ensures header exists exactly as requested.
    """
    header = ["Date", "Supplier", "Net Total", "VAT", "Sub-Category", "Items"]
    first_row = ws.row_values(1)
    if first_row != header:
        # If sheet is empty or wrong header, set it
        ws.resize(rows=max(ws.row_count, 2), cols=max(ws.col_count, len(header)))
        ws.update("A1:F1", [header])


def normalize_yes_no_vat(vat_amount: Any) -> str:
    """
    VAT column should be Yes if vat_amount exists and is not 0.
    Otherwise No.
    """
    if vat_amount is None:
        return "No"
    if isinstance(vat_amount, (int, float)):
        return "Yes" if vat_amount != 0 else "No"
    s = str(vat_amount).strip()
    if s == "" or s.lower() in ("null", "none"):
        return "No"
    # remove commas/spaces
    s2 = s.replace(",", "").replace(" ", "")
    # if numeric and zero
    try:
        val = float(s2)
        return "Yes" if val != 0 else "No"
    except Exception:
        # non-numeric but something exists -> Yes
        return "Yes"


def clean_json_only(text: str) -> str:
    """
    Extracts first {...} JSON block from model output.
    """
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Model did not return JSON.")
    return text[start : end + 1]


def extract_with_ai(image_bytes: bytes) -> List[Any]:
    """
    Extracts date, supplier, net_total, vat_amount, sub_category, items(list)
    then returns row values:
    [Date, Supplier, Net Total, VAT(Yes/No), Sub-Category, Items(text)]
    """
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    prompt = """
Extract from this invoice image and return ONLY JSON in this exact schema:

{
  "date": "",
  "supplier": "",
  "net_total": "",
  "vat_amount": null,
  "sub_category": "",
  "items": [
    {"name": "", "qty": "", "price": ""}
  ]
}

Rules:
- date: best-effort. If missing, return empty string.
- supplier: store/company name if available; otherwise empty string.
- net_total: final payable total (Net total). If unknown, empty string.
- vat_amount: if VAT exists, return the VAT numeric amount (or string if shown). If no VAT, return null.
- sub_category must be one of:
  Gas, Grocery, Restaurant, Office Supplies, Utilities, Transport, Maintenance, Other
  If uncertain, use "Other".
- items: include line items if visible. If not visible, return empty list [].

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
    json_text = clean_json_only(raw)
    data = json.loads(json_text)

    vat_flag = normalize_yes_no_vat(data.get("vat_amount"))

    # Items into a single cell (multi-line)
    items_text_lines: List[str] = []
    items = data.get("items") or []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            qty = str(item.get("qty", "")).strip()
            price = str(item.get("price", "")).strip()
            # Skip totally empty rows
            if not (name or qty or price):
                continue
            # Format: Name (qty × price)
            if qty and price:
                items_text_lines.append(f"{name} ({qty} × {price})".strip())
            else:
                # fallback format
                parts = [p for p in [name, qty, price] if p]
                items_text_lines.append(" - ".join(parts).strip())

    items_text = "\n".join(items_text_lines).strip()

    # If date empty, fill with today's UTC as fallback (optional)
    date_val = str(data.get("date", "")).strip()
    supplier_val = str(data.get("supplier", "")).strip()
    net_total_val = str(data.get("net_total", "")).strip()
    subcat_val = str(data.get("sub_category", "Other")).strip() or "Other"

    # Keep Date as extracted if exists; otherwise use current local timestamp-like string
    if not date_val:
        date_val = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return [date_val, supplier_val, net_total_val, vat_flag, subcat_val, items_text]


# ----------------------------
# Telegram Handlers
# ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام 😊\n"
        "عکس فاکتور رو بفرست تا اطلاعاتش رو با AI استخراج کنم و داخل Google Sheet ذخیره کنم.\n\n"
        "ستون‌ها: Date / Supplier / Net Total / VAT / Sub-Category / Items"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "راهنما:\n"
        "1) فقط عکس فاکتور رو بفرست.\n"
        "2) من اطلاعات رو با AI استخراج می‌کنم.\n"
        "3) خروجی داخل Google Sheets ذخیره می‌شه.\n"
        "4) VAT فقط Yes/No هست.\n"
        "5) Items داخل یک سلول چندخطی ذخیره می‌شه."
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.photo:
        return

    try:
        await msg.chat.send_action(action=ChatAction.TYPING)

        # Get best quality photo
        photo = msg.photo[-1]
        file = await context.bot.get_file(photo.file_id)

        # Download bytes
        image_bytes = await file.download_as_bytearray()
        image_bytes = bytes(image_bytes)

        # Extract data with AI
        await msg.chat.send_action(action=ChatAction.TYPING)
        row = extract_with_ai(image_bytes)

        # Save to Google Sheet
        ws = get_sheet()
        ensure_header(ws)
        ws.append_row(row, value_input_option="USER_ENTERED")

        await msg.reply_text("✅ ثبت شد و داخل Google Sheet ذخیره شد.")

    except Exception as e:
        logger.exception("Failed to process photo")
        await msg.reply_text(f"❌ خطا: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_error_handler(error_handler)

    logger.info("Bot is running (polling)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
