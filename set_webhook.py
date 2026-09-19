"""
set_webhook.py - point Telegram at your Flask app.

    python set_webhook.py                     # uses WEBHOOK_HOST from .env
    python set_webhook.py --delete            # remove the webhook (for polling)
    python set_webhook.py --info              # show Telegram's current view
"""

import argparse
import os
import sys
import time

import telebot
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
# e.g. WEBHOOK_HOST=https://shuxrinho.pythonanywhere.com
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://shuxrinho.pythonanywhere.com").rstrip("/")

if not BOT_TOKEN:
    sys.exit("BOT_TOKEN is missing - create a .env file next to this script.")

bot = telebot.TeleBot(BOT_TOKEN)

parser = argparse.ArgumentParser()
parser.add_argument("--delete", action="store_true")
parser.add_argument("--info", action="store_true")
args = parser.parse_args()

if args.info:
    info = bot.get_webhook_info()
    print(f"url:                  {info.url}")
    print(f"pending updates:      {info.pending_update_count}")
    print(f"last error:           {info.last_error_message}")
    sys.exit(0)

bot.remove_webhook()
time.sleep(1)

if args.delete:
    print("Webhook removed.")
    sys.exit(0)

url = f"{WEBHOOK_HOST}/{BOT_TOKEN}"
if bot.set_webhook(url=url, drop_pending_updates=True):
    print(f"Webhook set: {WEBHOOK_HOST}/<token>")
else:
    sys.exit("Telegram refused the webhook - check that the host is HTTPS and reachable.")
