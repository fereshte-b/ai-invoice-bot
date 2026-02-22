import os
import logging
from datetime import datetime
from openpyxl import Workbook, load_workbook
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

# =========================
# تنظیمات اصلی
# =========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set in environment variables")

DATA_DIR = "/data"
EXCEL_FILE = os.path.join(DATA_DIR, "invoices.xlsx")

# =========================
# لاگ‌گیری
# =========================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# =========================
# ساخت فایل اکسل اگر وجود نداشت
# =========================

def create_excel_if_not_exists():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)

    if not os.path.exists(EXCEL_FILE):
        wb = Workbook()
        ws = wb.active
        ws.title = "Invoices"
        ws.append(["Date", "User ID", "Username", "File Name"])
        wb.save(EXCEL_FILE)
        print("Excel file created at:", EXCEL_FILE)

# =========================
# ذخیره اطلاعات در اکسل
# =========================

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
    print("Saved to Excel:", file_name)

# =========================
# هندلر دریافت عکس
# =========================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):

    print("PHOTO RECEIVED")

    user = update.effective_user
    photo = update.message.photo[-1]

    file = await context.bot.get_file(photo.file_id)

    image_path = os.path.join(DATA_DIR, f"{photo.file_id}.jpg")
    await file.download_to_drive(image_path)

    print("Image saved at:", image_path)

    save_to_excel(
        user.id,
        user.username if user.username else "NoUsername",
        image_path
    )

    await update.message.reply_text("✅ عکس دریافت و ثبت شد")

# =========================
# اجرای ربات
# =========================

def main():
    create_excel_if_not_exists()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()
