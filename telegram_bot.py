import os
import logging
from datetime import datetime
from openpyxl import Workbook, load_workbook
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PORT", 8080))
RAILWAY_STATIC_URL = os.getenv("RAILWAY_STATIC_URL")

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set")

if not RAILWAY_STATIC_URL:
    raise ValueError("RAILWAY_STATIC_URL not set")

DATA_DIR = "/data"
EXCEL_FILE = os.path.join(DATA_DIR, "invoices.xlsx")


def create_excel_if_not_exists():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)

    if not os.path.exists(EXCEL_FILE):
        wb = Workbook()
        ws = wb.active
        ws.append(["Date", "User ID", "Username", "File Name"])
        wb.save(EXCEL_FILE)


def save_to_excel(user_id, username, file_name):
    wb = load_workbook(EXCEL_FILE)
    ws = wb.active
    ws.append([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        user_id,
        username,
        file_name
    ])
    wb.save(EXCEL_FILE)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    photo = update.message.photo[-1]

    file = await context.bot.get_file(photo.file_id)
    image_path = os.path.join(DATA_DIR, f"{photo.file_id}.jpg")
    await file.download_to_drive(image_path)

    save_to_excel(
        user.id,
        user.username if user.username else "NoUsername",
        image_path
    )

    await update.message.reply_text("✅ عکس ثبت شد")


def main():
    create_excel_if_not_exists()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    webhook_url = f"https://{RAILWAY_STATIC_URL}/"

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=webhook_url
    )


if __name__ == "__main__":
    main()
