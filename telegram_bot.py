import os
import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
)
import pandas as pd
import requests

# ---------------- CONFIG ---------------- #

BOT_TOKEN = os.getenv("BOT_TOKEN")
RAILWAY_STATIC_URL = os.getenv("RAILWAY_STATIC_URL")
PORT = int(os.getenv("PORT", 8080))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set")

if not RAILWAY_STATIC_URL:
    raise ValueError("RAILWAY_STATIC_URL not set")

EXCEL_PATH = "/data/invoices.xlsx"

logging.basicConfig(level=logging.INFO)

# ---------------- PHOTO HANDLER ---------------- #

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    file_path = "/data/last_photo.jpg"
    await file.download_to_drive(file_path)

    # اینجا فعلاً فقط نمونه دیتا اضافه می‌کنیم
    # بعداً میشه OCR واقعی گذاشت

    data = {
        "User": [update.effective_user.first_name],
        "Photo_File": [file_path],
    }

    df_new = pd.DataFrame(data)

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

# ---------------- MAIN ---------------- #

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
