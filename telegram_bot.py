import os
import logging
import base64
import pandas as pd
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
RAILWAY_STATIC_URL = os.getenv("RAILWAY_STATIC_URL")
PORT = int(os.getenv("PORT", 8080))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set")

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY not set")

if not RAILWAY_STATIC_URL:
    raise ValueError("RAILWAY_STATIC_URL not set")

client = OpenAI(api_key=OPENAI_API_KEY)

EXCEL_PATH = "/data/invoices.xlsx"

logging.basicConfig(level=logging.INFO)


# -------- AI EXTRACTION -------- #

def extract_with_ai(image_path):

    with open(image_path, "rb") as f:
        base64_image = base64.b64encode(f.read()).decode("utf-8")

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": """
Extract the following from this invoice and respond ONLY in JSON format:

{
  "date": "",
  "supplier": "",
  "net_total": "",
  "vat": ""
}

If VAT amount exists return the VAT number.
If VAT does not exist return "No".
"""
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}"
                        }
                    }
                ],
            }
        ],
        temperature=0
    )

    content = response.choices[0].message.content
    return eval(content)  # چون خروجی JSON هست


# -------- TELEGRAM HANDLER -------- #

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    image_path = "/data/last_photo.jpg"
    await file.download_to_drive(image_path)

    invoice_data = extract_with_ai(image_path)

    df_new = pd.DataFrame([{
        "Date": invoice_data["date"],
        "Supplier": invoice_data["supplier"],
        "Net Total": invoice_data["net_total"],
        "VAT": invoice_data["vat"]
    }])

    if os.path.exists(EXCEL_PATH):
        df_existing = pd.read_excel(EXCEL_PATH)
        df_final = pd.concat([df_existing, df_new], ignore_index=True)
    else:
        df_final = df_new

    df_final.to_excel(EXCEL_PATH, index=False)

    await update.message.reply_text("اکسل بروزرسانی شد ✅")

    await update.message.reply_document(
        document=open(EXCEL_PATH, "rb"),
        filename="invoices.xlsx"
    )


# -------- MAIN -------- #

def main():

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    webhook_url = f"https://{RAILWAY_STATIC_URL}"

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=webhook_url,
    )

if __name__ == "__main__":
    main()
