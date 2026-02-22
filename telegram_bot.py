import os
import logging
from datetime import datetime

import openpyxl
from openpyxl import Workbook
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -----------------------
# تنظیمات اصلی
# -----------------------

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# مسیر ذخیره اکسل داخل Railway (با Volume)
EXCEL_FILE_PATH = "/data/invoices.xlsx"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# -----------------------
# ساخت فایل اکسل اگر وجود نداشت
# -----------------------

def create_excel_if_not_exists():
    if not os.path.exists(EXCEL_FILE_PATH):
        wb = Workbook()
        ws = wb.active
        ws.title = "Invoices"
        ws.append(["Date", "User ID", "Username", "Message Text"])
        wb.save(EXCEL_FILE_PATH)


# -----------------------
# اضافه کردن ردیف جدید به اکسل
# -----------------------

def append_to_excel(user_id, username, text):
    create_excel_if_not_exists()

    wb = openpyxl.load_workbook(EXCEL_FILE_PATH)
    ws = wb.active

    ws.append([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        user_id,
        username,
        text
    ])

    wb.save(EXCEL_FILE_PATH)


# -----------------------
# دستورات تلگرام
# -----------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ربات فعال است ✅\nفاکتور یا متن بفرستید.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text

    append_to_excel(
        user_id=user.id,
        username=user.username if user.username else "NoUsername",
        text=text,
    )

    await update.message.reply_text("اطلاعات ذخیره شد ✅")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    append_to_excel(
        user_id=user.id,
        username=user.username if user.username else "NoUsername",
        text="Photo received"
    )

    await update.message.reply_text("عکس دریافت و ثبت شد ✅")


# -----------------------
# اجرای اصلی
# -----------------------

def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN not set in environment variables")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
