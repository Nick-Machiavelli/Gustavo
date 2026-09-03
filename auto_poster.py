"""
auto_poster.py — ربات مستقل انتشار خودکار هر 15 دقیقه به کانال تلگرام
====================================================================
این اسکریپت جدا از main.py است ولی از کلاس Gustavo همان پروژه استفاده می‌کند.
- هر 15 دقیقه (قابل تنظیم با INTERVAL_MINUTES) اخبار جدید را fetch می‌کند
- فقط آیتم‌های جدید (که قبلا پست نشده) را به کانال تلگرام می‌فرستد
- بدون نیاز به GitHub Actions — روی هر سرور/لپ‌تاپ/ Railway اجرا می‌شود
- اگر AI_API_KEY ست نباشد، خودکار با ترجمه fallback کار می‌کند

نصب و اجرا:
    uv pip install -r requirements.txt   # یا pip install -r requirements.txt
    # توکن‌ها را ست کنید (روش 1: متغیر محیطی)
    set TG_BOT_TOKEN=123456:ABC...
    set TG_CHANNEL_ID=@Enqelab_e_Iran
    # یا فایل .env بسازید (روش 2)
    python auto_poster.py                # یک بار تست
    python auto_poster.py --once         # فقط یک دور اجرا و خروج (برای cron)
    python auto_poster.py --daemon       # حلقه بی‌نهایت هر 15 دقیقه

برای GitHub Actions هر 15 دقیقه: فایل .github/workflows/auto-post-15min.yml را ببینید
"""
import os
import sys
import time
import json
import logging
import argparse
import signal
from datetime import datetime, timezone

# .env را اگر بود لود کن (اختیاری)
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("auto_poster")

# تنظیمات
INTERVAL_MINUTES = int(os.environ.get("INTERVAL_MINUTES", "15"))
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHANNEL_ID = os.environ.get("TG_CHANNEL_ID", "")

# برای اینکه main.py همین پوشه را ببیند
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from main import Gustavo, CONFIG
except Exception as e:
    logger.error(f"main.py لود نشد: {e}")
    sys.exit(1)

# graceful shutdown
_stop = False
def _handle_signal(signum, frame):
    global _stop
    logger.info(f"سیگنال {signum} دریافت شد — خروج تمیز...")
    _stop = True

signal.signal(signal.SIGINT, _handle_signal)
try:
    signal.signal(signal.SIGTERM, _handle_signal)
except Exception:
    pass


def check_config(warn_only=False):
    """بررسی توکن‌ها — اگر نبود فقط هشدار بده ولی اجرا را متوقف نکن (برای تست)"""
    ok = True
    if not TG_BOT_TOKEN:
        msg = "TG_BOT_TOKEN ست نیست — پست به تلگرام ارسال نمی‌شود. مقدار را در .env یا Secrets ست کنید."
        if warn_only:
            logger.warning(msg)
        else:
            logger.error(msg)
        ok = False
    if not TG_CHANNEL_ID:
        msg = "TG_CHANNEL_ID ست نیست — مثلا @Enqelab_e_Iran یا -100xxxx"
        if warn_only:
            logger.warning(msg)
        else:
            logger.error(msg)
        ok = False
    # همگام‌سازی با CONFIG داخل main.py
    if TG_BOT_TOKEN:
        CONFIG["TELEGRAM"]["BOT_TOKEN"] = TG_BOT_TOKEN
    if TG_CHANNEL_ID:
        CONFIG["TELEGRAM"]["CHANNEL_ID"] = TG_CHANNEL_ID
    return ok


def run_once():
    """یک دور کامل fetch + post — همان Gustavo().run() ولی با لاگ فارسی"""
    start = time.time()
    tehran = Gustavo()._get_tehran_time().strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"━━━ شروع دور جدید — تهران {tehran} ━━━")
    try:
        before = len(Gustavo().existing_news)  # برای لاگ
        g = Gustavo()
        g.run()
        elapsed = round(time.time() - start, 1)
        logger.info(f"✅ دور تمام شد در {elapsed} ثانیه — اخبار کل: {len(g.existing_news)}")
        return True
    except Exception as e:
        logger.exception(f"❌ خطا در run_once: {e}")
        return False


def daemon_loop(interval_minutes=INTERVAL_MINUTES):
    logger.info(f"🚀 حالت daemon فعال — هر {interval_minutes} دقیقه یک بار")
    logger.info(f"کانال: {TG_CHANNEL_ID or '(ست نشده)'} | توقف با Ctrl+C")
    check_config(warn_only=True)

    # دور اول فوری
    run_once()

    while not _stop:
        # خواب با قابلیت interrupt هر ثانیه
        total_sleep = interval_minutes * 60
        logger.info(f"⏳ خواب {interval_minutes} دقیقه تا دور بعدی... (برای توقف Ctrl+C)")
        slept = 0
        while slept < total_sleep and not _stop:
            time.sleep(1)
            slept += 1
        if _stop:
            break
        run_once()

    logger.info("👋 daemon متوقف شد.")


def main():
    parser = argparse.ArgumentParser(description="ربات انتشار خودکار News.ir هر 15 دقیقه")
    parser.add_argument("--once", action="store_true", help="فقط یک دور اجرا و خروج")
    parser.add_argument("--daemon", action="store_true", help="حلقه بی‌نهایت (پیش‌فرض اگر هیچ فلگی ندهی هم daemon است)")
    parser.add_argument("--interval", type=int, default=INTERVAL_MINUTES, help="فاصله به دقیقه (پیش‌فرض 15)")
    parser.add_argument("--check", action="store_true", help="فقط بررسی تنظیمات و خروج")
    args = parser.parse_args()

    if args.check:
        ok = check_config(warn_only=False)
        print(f"INTERVAL: {args.interval} min")
        print(f"TG_BOT_TOKEN: {'✅ ست شده' if TG_BOT_TOKEN else '❌ خالی'}")
        print(f"TG_CHANNEL_ID: {TG_CHANNEL_ID or '❌ خالی'}")
        print(f"BASE_SITE_URL: {CONFIG.get('BASE_SITE_URL')}")
        sys.exit(0 if ok else 1)

    if args.once:
        check_config(warn_only=True)
        success = run_once()
        sys.exit(0 if success else 1)
    else:
        # پیش‌فرض: daemon — حتی اگر --daemon هم پاس داده نشده باشد
        daemon_loop(interval_minutes=args.interval)


if __name__ == "__main__":
    main()
