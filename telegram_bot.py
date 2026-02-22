import os
import io
import json
import base64
import openpyxl
from PIL import Image
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from openai import OpenAI

# =========================
# ENV VARIABLES (Railway)
# =========================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

client = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# STORAGE (Railway Volume)
# =========================

IMAGE_FOLDER = "/data/invoice_images"
os.makedirs(IMAGE_FOLDER, exist_ok=True)

EXCEL_FILE = "/data/ai_invoices.xlsx"

# =========================
# IMAGE OPTIMIZATION (کم‌حجم برای کاهش هزینه)
# =========================

def prepare_image_base64(path):
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((1400, 1400))
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=75, optimize=True)
        return base64.b64encode(buffer.getvalue()).decode("utf-8")

# =========================
# AI EXTRACTION
# =========================

def extract_invoice_fields(image_path):

    base64_image = prepare_image_base64(image_path)

    response = client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=200,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": """Extract ONLY these 3 fields from this invoice image:

1) company
2) invoice_date
3) final_total

Return strictly JSON:
{
  "company": "...",
  "invoice_date": "...",
  "final_total": "..."
}
"""
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}"
                        }
                    }
                ]
            }
        ]
    )

    return json.loads(response.choices[0].message.content)

# =========================
# SAVE TO EXCEL
# =========================

def save_to_excel(data):

    if os.path.exists(EXCEL_FILE):
        wb = openpyxl.load_workbook(EXCEL_FILE)
        ws = wb.active
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Invoices"
        ws.append(["Company", "Invoice Date", "Final Total"])

    ws.append([
        data.get("company", ""),
        data.get("invoice_date", ""),
        data.get("final_total", "")
    ])

    wb.save(EXCEL_FILE)

# =========================
# TELEGRAM HANDLER
# =========================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    photo = await update.message.photo[-1].get_file()
    file_path = os.path.join(IMAGE_FOLDER, f"{photo.file_id}.jpg")
    await photo.download_to_drive(file_path)

    try:
        result = extract_invoice_fields(file_path)
        save_to_excel(result)

        await update.message.reply_text(
            f"✅ Saved to Excel\n\n"
            f"Company: {result['company']}\n"
            f"Date: {result['invoice_date']}\n"
            f"Total: {result['final_total']}"
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

# =========================
# MAIN
# =========================

def main():

    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY not set")

    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN not set")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("🤖 AI Invoice Bot running on Railway...")
    app.run_polling()

if __name__ == "__main__":
    main()