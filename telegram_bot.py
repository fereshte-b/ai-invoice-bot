import os
import json
import logging
import base64
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from google.oauth2 import service_account
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

client = OpenAI(api_key=OPENAI_API_KEY)

# -------- Google Sheets Setup -------- #

credentials_info = json.loads(GOOGLE_JSON)
credentials = service_account.Credentials.from_service_account_info(
    credentials_info,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)

sheet_service = build("sheets", "v4", credentials=credentials)
sheet = sheet_service.spreadsheets()

# -------- AI Extraction -------- #

def extract_with_ai(image_bytes):

    b64 = base64.b64encode(image_bytes).decode()

    response = client.responses.create(
        model="gpt-4o-mini",
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": """
Extract from this invoice and return ONLY JSON:

{
  "date": "",
  "supplier": "",
  "net_total": "",
  "vat_amount": ""
}

If VAT amount exists return the number.
If VAT does not exist return null.
"""},
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"}
                ],
            }
        ],
        temperature=0,
    )

    text = response.output_text.strip()

    start = text.find("{")
    end = text.rfind("}") + 1
    clean_json = text[start:end]

    data = json.loads(clean_json)

    vat_flag = "Yes" if data.get("vat_amount") not in (None, "", "0", 0) else "No"

    return [
        data.get("date", ""),
        data.get("supplier", ""),
        data.get("net_total", ""),
        vat_flag
    ]

# -------- Telegram Handler -------- #

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()

    await update.message.reply_text("در حال تحلیل فاکتور با AI...")

    row = extract_with_ai(bytes(image_bytes))

    sheet.values().append(
        spreadsheetId=SHEET_ID,
        range="A:D",
        valueInputOption="USER_ENTERED",
        body={"values": [row]}
    ).execute()

    await update.message.reply_text("✅ اطلاعات داخل Google Sheets ذخیره شد.")

# -------- Main -------- #

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == "__main__":
    main()
