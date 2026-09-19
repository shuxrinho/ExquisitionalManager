"""
bot-flask.py - Vocabulary lookup Telegram bot (Flask webhook version).

Requires vocab_scraper.py next to this file.
Environment (.env): BOT_TOKEN, SECRET_KEY, ADMIN_ID
"""

import difflib
import hmac
import html
import math
import os
import re
import sqlite3
import threading
import time
import traceback

import telebot
from dotenv import load_dotenv
from flask import Flask, abort, request

import vocab_scraper
from vocab_scraper import DB_PATH, update_database

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SECRET_KEY = os.getenv("SECRET_KEY")
_admin = os.getenv("ADMIN_ID", "")
ADMIN_ID = int(_admin) if _admin.strip().isdigit() else 0

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing - create a .env file next to this script.")
if not ADMIN_ID:
    print("WARNING: ADMIN_ID is not set or not numeric; /update will be refused for everyone.")

PAGE_SIZE = 20
MSG_LIMIT = 3800

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
app = Flask(__name__)

_update_lock = threading.Lock()

# chat_ids currently waiting to send a word submission after /add_words
_pending_submissions = set()


# --------------------------------------------------------------------------
# database helpers
# --------------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def db_ready():
    if not os.path.exists(DB_PATH):
        return False
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1 FROM lists LIMIT 1").fetchone()
        return True
    except sqlite3.Error:
        return False


def fmt_date(value):
    if not value:
        return ""
    try:
        y, m, d = value.split("-")
        return f"{d}.{m}.{y}"
    except ValueError:
        return ""


def esc(text):
    return html.escape(text or "")


# --------------------------------------------------------------------------
# message sending
# --------------------------------------------------------------------------
def chunk_html(text, limit=MSG_LIMIT):
    """
    Split an HTML-formatted message into Telegram-sized pieces.

    Splits on line breaks (falling back to spaces) so a piece never ends in the
    middle of a <b>...</b> tag, and re-opens a tag that a split left dangling.
    """
    pieces, buf = [], ""
    for line in text.split("\n"):
        while len(line) > limit:                      # single monster line
            cut = line.rfind(" ", 0, limit)
            if cut < limit // 2:
                cut = limit
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.append(line[:cut])
            line = line[cut:].lstrip()
        if len(buf) + len(line) + 1 > limit:
            pieces.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        pieces.append(buf)

    fixed, carry = [], ""
    for piece in pieces:
        piece = carry + piece
        carry = ""
        for tag in ("b", "i"):
            if piece.count(f"<{tag}>") > piece.count(f"</{tag}>"):
                piece += f"</{tag}>"
                carry = f"<{tag}>" + carry
        fixed.append(piece)
    return [p for p in fixed if p.strip()]


def send_html(chat_id, text, **kwargs):
    for piece in chunk_html(text):
        try:
            bot.send_message(chat_id, piece, parse_mode="HTML", **kwargs)
        except Exception:                              # bad markup -> send as plain text
            bot.send_message(chat_id, re.sub(r"<[^>]+>", "", piece), **kwargs)
        kwargs.pop("reply_markup", None)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def render_definition(row, list_row, via=None, via_rel=None):
    out = [f"<b>{esc(row['display'] or row['word'])}</b>"]
    if via:
        labels = {"alias": "an alternative form", "family": "a family word",
                  "syn": "a synonym", "phrase": "a phrase"}
        label = labels.get(via_rel, "a related word")
        out.append(f"<i>(you typed {label}: “{esc(via)}”)</i>")
    out.append("")
    out.append(esc(row["definition"]))
    if list_row:
        d = fmt_date(list_row["date"])
        head = f"{d} {list_row['title']}" if d else list_row["title"]
        out.append(f"\n📚 List {list_row['ord']}. {esc(head)}")
    return "\n".join(out)


def lists_page_text(page, total):
    start = (page - 1) * PAGE_SIZE + 1
    end = min(page * PAGE_SIZE, total)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ord, date, title FROM lists WHERE ord BETWEEN ? AND ? ORDER BY ord",
            (start, end)).fetchall()
    pages = max(1, math.ceil(total / PAGE_SIZE))
    out = [f"<b>Word lists {start}-{end} of {total}</b> (page {page}/{pages})", ""]
    for r in rows:
        d = fmt_date(r["date"])
        title = esc(r["title"])
        out.append(f"{r['ord']}. {d} {title}" if d else f"{r['ord']}. {title}")
    out.append("\nType a list number to see its words.")
    return "\n".join(out)


def lists_keyboard(total, current):
    pages = max(1, math.ceil(total / PAGE_SIZE))
    if pages < 2:
        return None
    buttons = [
        telebot.types.InlineKeyboardButton(
            f"·{i}·" if i == current else str(i), callback_data=f"lpage:{i}")
        for i in range(1, pages + 1)
    ]
    rows = [buttons[i:i + 8] for i in range(0, len(buttons), 8)]
    return telebot.types.InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------
# lookup
# --------------------------------------------------------------------------
_suggest_cache = {"mtime": None, "words": []}


def all_keys():
    """Cached list of every searchable key, refreshed when the DB file changes."""
    try:
        mtime = os.path.getmtime(DB_PATH)
    except OSError:
        return []
    if _suggest_cache["mtime"] != mtime:
        with get_conn() as conn:
            words = [r[0] for r in conn.execute("SELECT DISTINCT word FROM words")]
            words += [r[0] for r in conn.execute("SELECT DISTINCT fword FROM family")]
        _suggest_cache.update(mtime=mtime, words=sorted(set(words)))
    return _suggest_cache["words"]


def find_word(query):
    """Return (rows, via, rel).  rows may be empty."""
    q = query.lower().strip()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM words WHERE word=? ORDER BY list_id, pos", (q,)).fetchall()
        if rows:
            return rows, None, None
        fam = conn.execute(
            "SELECT headword, rel FROM family WHERE fword=? ORDER BY "
            "CASE rel WHEN 'alias' THEN 0 WHEN 'family' THEN 1 "
            "WHEN 'phrase' THEN 2 ELSE 3 END", (q,)).fetchone()
        if fam:
            rows = conn.execute(
                "SELECT * FROM words WHERE word=? ORDER BY list_id, pos",
                (fam["headword"],)).fetchall()
            if rows:
                return rows, q, fam["rel"]
        # multi-word entries: "imbue" typed for "imbue something with something"
        rows = conn.execute(
            "SELECT * FROM words WHERE word LIKE ? ORDER BY list_id, pos LIMIT 3",
            (q + " %",)).fetchall()
        if rows:
            return rows, None, None
    return [], None, None


def get_list_row(conn, ord_):
    return conn.execute(
        "SELECT ord, date, title FROM lists WHERE ord=?", (ord_,)).fetchone()


# --------------------------------------------------------------------------
# bot handlers
# --------------------------------------------------------------------------
@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    send_html(message.chat.id,
              "Hi! Send me any word and I'll look it up.\n\n"
              "• a word — its definition (family words like <i>evasive</i> work too)\n"
              "• /lists — index of all word lists, oldest first\n"
              "• a list number — all words from that list\n"
              "• /words — how many words are in the database\n"
              "• /add_words — submit a word or word list for the admin to review\n"
              "• /update — rebuild the database from the channel (admin only)\n"
              "• /version — diagnostic info (admin only, useful after deploying)")


@bot.message_handler(commands=["version"])
def show_version(message):
    """Diagnostic: confirms the running process matches the file on disk.
    If mtime is old after you've edited vocab_scraper.py, the process needs
    a full restart/reload, not just /update."""
    path = os.path.abspath(vocab_scraper.__file__)
    try:
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path)))
    except OSError:
        mtime = "unknown"
    with_last = ""
    if db_ready():
        with get_conn() as conn:
            row = conn.execute("SELECT v FROM meta WHERE k='updated_at'").fetchone()
        if row:
            with_last = f"\nLast successful /update: {row[0]}"
    bot.reply_to(message,
                 f"vocab_scraper.py loaded from:\n{path}\n"
                 f"file last modified: {mtime}{with_last}\n\n"
                 "If you just edited the code and this mtime looks stale, "
                 "the process needs a full restart (or Reload on PythonAnywhere) "
                 "before /update will use the new logic.")


@bot.message_handler(commands=["words"])
def total_words(message):
    if not db_ready():
        bot.reply_to(message, "The database is empty. An admin needs to run /update.")
        return
    with get_conn() as conn:
        n_words = conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        n_uniq = conn.execute("SELECT COUNT(DISTINCT word) FROM words").fetchone()[0]
        n_lists = conn.execute("SELECT COUNT(*) FROM lists").fetchone()[0]
        upd = conn.execute("SELECT v FROM meta WHERE k='updated_at'").fetchone()
    text = (f"📊 {n_words} entries ({n_uniq} unique words) in {n_lists} lists.")
    if upd:
        text += f"\nLast update: {upd[0]}"
    bot.reply_to(message, text)


@bot.message_handler(commands=["lists"])
def show_lists(message):
    if not db_ready():
        bot.reply_to(message, "The database is empty. An admin needs to run /update.")
        return
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM lists").fetchone()[0]
    send_html(message.chat.id, lists_page_text(1, total),
              reply_markup=lists_keyboard(total, 1))


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("lpage:"))
def lists_page(call):
    try:
        page = int(call.data.split(":")[1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id)
        return
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM lists").fetchone()[0]
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(page, 1), pages)
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(lists_page_text(page, total),
                              call.message.chat.id, call.message.message_id,
                              parse_mode="HTML",
                              reply_markup=lists_keyboard(total, page))
    except Exception:
        pass          # "message is not modified" when the same page is tapped twice


@bot.message_handler(commands=["update"])
def force_update(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "You are not authorized to do this.")
        return
    if _update_lock.locked():
        bot.reply_to(message, "An update is already running, please wait.")
        return

    chat_id = message.chat.id
    try:
        status = bot.reply_to(message, "Updating database, please wait…")
        status_id = status.message_id
    except Exception:
        # The original message may already be gone (e.g. Telegram retried an
        # old webhook delivery). Fall back to a plain message in the chat.
        status = bot.send_message(chat_id, "Updating database, please wait…")
        status_id = status.message_id

    def run():
        with _update_lock:
            try:
                n_lists, n_words = update_database(log=lambda *a: None)
                text = f"✅ Database updated: {n_lists} lists, {n_words} words."
            except Exception as exc:                                    # noqa: BLE001
                traceback.print_exc()
                text = (f"❌ Update failed: {type(exc).__name__}: {exc}\n\n"
                        "The previous database is untouched.")
        try:
            bot.edit_message_text(text, chat_id, status_id)
        except Exception:
            bot.send_message(chat_id, text)

    # IMPORTANT: this must not block the webhook request. Telegram expects a
    # fast HTTP response and will retry (re-deliver the same /update command)
    # if it doesn't get one, which is what caused the "message to be replied
    # not found" errors - overlapping retries of a handler that used to run
    # the whole scrape inline. Doing the work in a thread lets this handler
    # (and the webhook route) return immediately.
    threading.Thread(target=run, daemon=True).start()


@bot.message_handler(commands=["add_words"])
def add_words_prompt(message):
    _pending_submissions.add(message.chat.id)
    bot.reply_to(message,
                 "Send me the word(s) and definition(s) you'd like to add, "
                 "all in one message. Type /cancel to stop.")


@bot.message_handler(commands=["cancel"],
                     func=lambda m: m.chat.id in _pending_submissions)
def cancel_submission(message):
    _pending_submissions.discard(message.chat.id)
    bot.reply_to(message, "Cancelled — nothing was submitted.")


@bot.message_handler(func=lambda m: m.chat.id in _pending_submissions,
                     content_types=["text"])
def receive_submission(message):
    _pending_submissions.discard(message.chat.id)
    user = message.from_user
    who = f"@{user.username}" if user.username else (user.first_name or "someone")
    forward_text = (
        f"📥 <b>New word submission</b>\n"
        f"From: {esc(who)} (id <code>{user.id}</code>)\n\n"
        f"{esc(message.text)}"
    )
    sent_to_admin = False
    if ADMIN_ID:
        try:
            send_html(ADMIN_ID, forward_text)
            sent_to_admin = True
        except Exception:
            traceback.print_exc()
    if sent_to_admin:
        bot.reply_to(message, "✅ Your words have been submitted. Thank you!")
    else:
        bot.reply_to(message,
                     "⚠️ I couldn't reach the admin right now, so this wasn't "
                     "delivered. Please try again later.")


@bot.message_handler(func=lambda m: m.chat.id in _pending_submissions,
                     content_types=["photo", "document", "audio", "video",
                                    "voice", "sticker", "animation"])
def receive_submission_wrong_type(message):
    bot.reply_to(message,
                 "Please send the word(s) as text (or /cancel to stop).")


@bot.message_handler(func=lambda m: m.text and m.text.strip().isdigit(),
                     content_types=["text"])
def show_list(message):
    if not db_ready():
        bot.reply_to(message, "The database is empty. An admin needs to run /update.")
        return
    n = int(message.text.strip())
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM lists").fetchone()[0]
        lst = get_list_row(conn, n)
        if not lst:
            bot.reply_to(message, f"Please type a number between 1 and {total}, or a word.")
            return
        words = conn.execute(
            "SELECT pos, display, word, definition FROM words WHERE list_id=? ORDER BY pos, id",
            (n,)).fetchall()
    d = fmt_date(lst["date"])
    head = f"<b>List {n}. {d} {esc(lst['title'])}</b>\n"
    body = []
    for r in words:
        defn = esc(r["definition"]).replace("\n", " ")
        body.append(f"{r['pos']}. <b>{esc(r['display'] or r['word'])}</b> — {defn}")
    send_html(message.chat.id, head + "\n" + "\n\n".join(body))


@bot.message_handler(func=lambda m: True, content_types=["text"])
def lookup(message):
    query = (message.text or "").strip()
    if not query:
        return
    if not db_ready():
        bot.reply_to(message, "The database is empty. An admin needs to run /update.")
        return

    rows, via, rel = find_word(query)
    if rows:
        with get_conn() as conn:
            parts = [render_definition(r, get_list_row(conn, r["list_id"]), via, rel)
                     for r in rows[:3]]
        send_html(message.chat.id, "\n\n———\n\n".join(parts))
        return

    suggestions = difflib.get_close_matches(query.lower(), all_keys(), n=5, cutoff=0.75)
    text = f"Sorry, “{esc(query)}” isn't in the database yet."
    if suggestions:
        text += "\n\nDid you mean: " + ", ".join(f"<b>{esc(s)}</b>" for s in suggestions)
    send_html(message.chat.id, text)


# --------------------------------------------------------------------------
# web routes
# --------------------------------------------------------------------------
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    if request.headers.get("content-type") != "application/json":
        abort(403)
    update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
    bot.process_new_updates([update])
    return "", 200


@app.route("/update_db")
def trigger_update():
    supplied = request.args.get("secret", "")
    if not SECRET_KEY or not hmac.compare_digest(supplied, SECRET_KEY):
        abort(403)
    if _update_lock.locked():
        return "Update already running.", 409

    def run():
        with _update_lock:
            try:
                update_database(log=lambda *a: None)
            except Exception:                                       # noqa: BLE001
                traceback.print_exc()

    # Don't block this request on the scrape - a slow/blocked outbound
    # connection could otherwise tie up the worker for the platform's whole
    # request-timeout window. Callers that need the result should poll
    # /version or /words afterward rather than waiting on this response.
    threading.Thread(target=run, daemon=True).start()
    return "Update started in the background.", 202


@app.route("/")
def index():
    return "Vocabulary bot is running.", 200


if __name__ == "__main__":
    import sys
    if "--update" in sys.argv:            # rebuild the DB from the command line
        vocab_scraper.update_database()
    else:
        app.run(host="0.0.0.0", port=8080)