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

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY not set")
if not GOOGLE_SHEET_ID:
    raise ValueError("GOOGLE_SHEET_ID not set")
if not (GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_JSON_B64):
    raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_JSON_B64 not set")

client = OpenAI(api_key=OPENAI_API_KEY)

SHEET1_NAME = "invoices"         # دقیقا مثل تب شیت شما
SHEET2_NAME = "Detailed_Items"   # دقیقا مثل تب شیت شما

# ----------------------------
# Google Sheets (NO creation)
# ----------------------------
def _load_service_account_info() -> Dict[str, Any]:
    if GOOGLE_SERVICE_ACCOUNT_JSON:
        return json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    decoded = base64.b64decode(GOOGLE_SERVICE_ACCOUNT_JSON_B64).decode("utf-8")
    return json.loads(decoded)

def get_spreadsheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(_load_service_account_info(), scopes=scopes)
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
    return text[start:end + 1]

def safe_str(x: Any) -> str:
    return str(x).strip() if x is not None else ""

def to_number(value: Any) -> Optional[float]:
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
    num = to_number(vat_amount)
    if num is None:
        return "No"
    return "Yes" if num > 0 else "No"

def normalize_date_yyyy_mm_dd_slash(raw: Any) -> str:
    s = str(raw).strip() if raw is not None else ""
    if not s:
        return datetime.now(timezone.utc).strftime("%Y/%m/%d")

    fmts = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d-%m-%y",
        "%d/%m/%y",
        "%d-%b-%y",
        "%d-%b-%Y",
        "%d %b %Y",
        "%d %B %Y",
    ]
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            return dt.strftime("%Y/%m/%d")
        except Exception:
            pass

    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%Y/%m/%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y/%m/%d")

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

Rules:
- date: invoice date. If missing, empty.
- supplier: seller/company name.
- net_total: final payable amount (grand/net total).
- vat_amount: total VAT for invoice.
  - If VAT not present OR shown as 0.000 => return null.
- sub_category must be one of:
  Gas, Grocery, Restaurant, Office Supplies, Utilities, Transport, Maintenance, Other
  If uncertain => "Other"
- items: extract each line from table if visible.
  - qty, rate, discount, vat per line
  - line_total = amount/total shown for that item row (if visible)
  - If cannot see items => []
Return JSON only.
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

    date_val = normalize_date_yyyy_mm_dd_slash(data.get("date"))
    supplier_val = safe_str(data.get("supplier"))
    net_total_val = safe_str(data.get("net_total"))

    subcat_val = safe_str(data.get("sub_category")) or "Other"
    allowed = {"Gas", "Grocery", "Restaurant", "Office Supplies", "Utilities", "Transport", "Maintenance", "Other"}
    if subcat_val not in allowed:
        subcat_val = "Other"

    vat_flag = vat_yes_no(data.get("vat_amount"))
    invoice_vat_no = (vat_flag == "No")

    items = data.get("items") or []
    if not isinstance(items, list):
        items = []

    # Build items text for Sheet1
    lines: List[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = safe_str(it.get("name"))
        qty = safe_str(it.get("qty"))
        rate = safe_str(it.get("rate"))
        disc = safe_str(it.get("discount"))
        vat_i = safe_str(it.get("vat"))
        lt = safe_str(it.get("line_total"))
        if not (name or qty or rate or disc or vat_i or lt):
            continue
        parts = []
        if name:
            parts.append(name)
        if qty:
            parts.append(f"qty {qty}")
        if rate:
            parts.append(f"rate {rate}")
        if disc:
            parts.append(f"disc {disc}")
        if vat_i:
            parts.append(f"vat {vat_i}")
        if lt:
            parts.append(f"line {lt}")
        lines.append(" | ".join(parts))

    items_text = "\n".join(lines).strip()
    if not items_text:
        items_text = "UNREADABLE_ITEMS"

    return {
        "date": date_val,
        "supplier": supplier_val,
        "net_total": net_total_val,
        "vat_flag": vat_flag,
        "invoice_vat_no": invoice_vat_no,
        "sub_category": subcat_val,
        "items": items,
        "items_text": items_text,
    }

# ----------------------------
# Telegram Handler
# ----------------------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.photo:
        return

    try:
        await msg.chat.send_action(ChatAction.TYPING)

        # paid by (sender name)
        user = update.effective_user
        paid_by = user.full_name if (user and user.full_name) else (user.username if user and user.username else "Unknown")

        # get image bytes
        photo = msg.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        result = extract_with_ai(bytes(image_bytes))

        sh = get_spreadsheet()
        ws1 = get_ws(sh, SHEET1_NAME)
        ws2 = get_ws(sh, SHEET2_NAME)

        # Sheet1 columns:
        # date, supplier, net_total, vat, sub_category, items, paid by
        ws1.append_row(
            [
                result["date"],
                result["supplier"],
                result["net_total"],
                result["vat_flag"],
                result["sub_category"],
                result.get("items_text", ""),
                paid_by,
            ],
            value_input_option="USER_ENTERED",
        )

        # Sheet2 columns:
        # date, supplier, product description, quantity, rate, discount, vat, total price, paid by
        items: List[Dict[str, Any]] = result["items"]

        if items:
            wrote_any = False
            for item in items:
                if not isinstance(item, dict):
                    continue

                name = safe_str(item.get("name"))
                if not name:
                    continue

                qty = to_number(item.get("qty")) or 0.0
                rate = to_number(item.get("rate")) or 0.0
                discount = to_number(item.get("discount")) or 0.0

                # Fix VAT per item
                vat_i = 0.0 if result["invoice_vat_no"] else (to_number(item.get("vat")) or 0.0)

                # Prefer AI line_total
                lt = to_number(item.get("line_total"))
                if lt is not None:
                    total_price = lt
                else:
                    total_price = (qty * rate) - discount + vat_i

                ws2.append_row(
                    [
                        result["date"],
                        result["supplier"],
                        name,
                        qty,
                        rate,
                        discount,
                        vat_i,
                        total_price,
                        paid_by,
                    ],
                    value_input_option="USER_ENTERED",
                )
                wrote_any = True

            if not wrote_any:
                ws2.append_row(
                    [
                        result["date"],
                        result["supplier"],
                        "UNREADABLE_ITEMS",
                        "",
                        "",
                        "",
                        0,
                        "",
                        paid_by,
                    ],
                    value_input_option="USER_ENTERED",
                )
        else:
            ws2.append_row(
                [
                    result["date"],
                    result["supplier"],
                    "UNREADABLE_ITEMS",
                    "",
                    "",
                    "",
                    0,
                    "",
                    paid_by,
                ],
                value_input_option="USER_ENTERED",
            )

        await msg.reply_text("با موفقیت ثبت شد")

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
