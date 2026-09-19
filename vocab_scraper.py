"""
vocab_scraper.py
================
Scrapes the "vocabularium" Telegram channel (https://t.me/s/exquisitional)
and builds the SQLite database used by the bot.

Run standalone to (re)build the database:

    python vocab_scraper.py            # build ./vocab.db
    python vocab_scraper.py --dump 12  # print list 12 for inspection

Design notes
------------
The old parser walked the <b> tags of every post and treated *every* bold run
as a new headword.  That breaks on the real posts, because bold is also used
inside definitions ("**to no avail**", "UK **vapour**") and multi-sense
entries put their senses on separate lines below the headword.  The result was
entries with empty definitions such as:

    savour -> "1) savour(noun, UK literary) - . 2) savour(verb, UK) - ."

This parser ignores formatting completely and works on the plain text of the
post, line by line:

  * a *list post* is any post that contains >= 2 lines of the form "N. word ..."
    where the numbers run consecutively (1, 2, 3, ...).  No keyword matching,
    so titles like "From the English olympiad on class level." are recognised;
  * everything between one numbered line and the next belongs to that entry,
    so sub-senses ("1) savour(noun, UK literary) - ...") and "Phrases:" blocks
    are kept as part of the definition;
  * a post whose first numbered entry is not 1 and that has no title line is
    treated as a *continuation* of the previous list (e.g. the olympiad list is
    split over posts 81 and 82).
"""

import argparse
import os
import re
import sqlite3
import sys
import time

import requests
from bs4 import BeautifulSoup

CHANNEL = "exquisitional"
BASE_URL = f"https://t.me/s/{CHANNEL}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; VocabBot/1.0)"}
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "vocab.db")
MAX_PAGES = 300
REQUEST_TIMEOUT = 20

# --------------------------------------------------------------------------
# regexes
# --------------------------------------------------------------------------
POS = (r"noun|verb|adjective|adj|adverb|adv|pronoun|preposition|prep|conjunction|"
       r"phrasal verb|phrase|idiom|plural|singular|past|v|n")

# "Endowment(noun)", "Savory(adj – US spelling)", "Compose(v)", "Plait (noun)"
FAMILY_RE = re.compile(r"\b([A-Za-z][A-Za-z'\u2019\-]{1,})\s*\(\s*(?:" + POS + r")\b", re.I)
PLURAL_RE = re.compile(r"\bPlurals?\s*:\s*([^.\n]+)", re.I)
SYN_RE = re.compile(r"\bSyn(?:onyms?)?\s*:\s*([^.\n]+)", re.I)
# "1) **to no avail**: *used for saying ...*"  (inside a Phrases: block)
PHRASE_RE = re.compile(r"^\d+\)\s*(.+?)\s*:\s+(.+)$")

ENTRY_DOT = re.compile(r"^(\d{1,3})\.\s+(\S.*)$")
ENTRY_ANY = re.compile(r"^(\d{1,3})[.)]\s+(\S.*)$")
# "pine (noun, B2) - An evergreen tree ..."  /  "savour:"  /  "vapor (US, UK vapour):"
HEAD_SPLIT = re.compile(r"^(?P<head>.+?)\s*(?:\s[-\u2013\u2014]\s+|:\s*)(?P<rest>.*)$")
ALT_SPELLING_RE = re.compile(r"\b(?:UK|US|also|or|AmE|BrE)\s+([A-Za-z][A-Za-z'\-]{2,})", re.I)
MD_JUNK_RE = re.compile(r"[*_`\u2060]")
STOPWORDS = {"the", "a", "an", "to", "of", "in", "on", "at", "for", "with",
             "under", "by", "be", "get", "have", "no", "not"}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _clean(text):
    """Collapse whitespace and drop stray markdown characters."""
    return re.sub(r"\s+", " ", MD_JUNK_RE.sub("", text)).strip()


def clean_word(head):
    """'pine (noun, B2)' -> 'pine';  'the grassroots' -> 'the grassroots'."""
    w = re.sub(r"\([^)]*\)", " ", head)
    w = _clean(w).strip(" .,:;\u2013\u2014-")
    return w


def word_aliases(head, word):
    """Extra keys the user might type for this entry."""
    out = set()
    low = word.lower()

    # alternative spellings mentioned in brackets: "(US, UK vapour)"
    for inner in re.findall(r"\(([^)]*)\)", head):
        for alt in ALT_SPELLING_RE.findall(inner):
            out.add(alt.lower())

    # "imbue something/someone with something" -> "imbue"
    trimmed = re.split(r"\s+(?:something|someone|somebody|sth|sb)\b", low)[0].strip()
    if trimmed and trimmed != low:
        out.add(trimmed)
    else:
        trimmed = low

    # "the grassroots" -> "grassroots"
    if trimmed.startswith("the "):
        out.add(trimmed[4:])

    # "bank on" -> "bank";  "appertain to" -> "appertain"
    tokens = trimmed.split()
    if 2 <= len(tokens) <= 3 and tokens[0] not in STOPWORDS:
        out.add(tokens[0])

    out.discard(low)
    return {a for a in out if len(a) > 2}


def extract_relations(definition, word):
    """Return [(related_word, relation), ...] found inside a definition."""
    rels = []
    low = word.lower()
    for m in FAMILY_RE.finditer(definition):
        fw = m.group(1).lower()
        if fw != low and len(fw) > 2:
            rels.append((fw, "family"))
    for m in PLURAL_RE.finditer(definition):
        for part in re.split(r",| or ", m.group(1)):
            p = _clean(part).lower()
            if p and p != low and len(p) > 2 and " " not in p:
                rels.append((p, "family"))
    for m in SYN_RE.finditer(definition):
        for part in m.group(1).split(","):
            p = _clean(part).lower()
            if p and p != low and len(p) > 2:
                rels.append((p, "syn"))
    for line in definition.split("\n"):
        m = PHRASE_RE.match(line.strip())
        if m:
            p = _clean(m.group(1)).lower().strip(" .,:;")
            if p and p != low and len(p) > 2:
                rels.append((p, "phrase"))
    return rels


# --------------------------------------------------------------------------
# post parsing
# --------------------------------------------------------------------------
def split_entries(lines, pattern):
    """
    Group the lines of a post into entries.

    A line starts a new entry only if its number is strictly larger than the
    previous entry's number.  Sub-sense blocks restart their numbering at 1
    ("1) savour(noun...)", "Phrases: 1) ..."), so they never look like a new
    entry and stay attached to the entry above them.  Skipped numbers in the
    post (a typo, a deleted item) do not truncate the rest of the list.
    """
    entries = []
    current = None
    last_num = None
    preamble = []
    for raw in lines:
        line = raw.strip()
        m = pattern.match(line)
        if m and (last_num is None or int(m.group(1)) > last_num):
            num = int(m.group(1))
            if current:
                entries.append(current)
            current = {"num": num, "lines": [m.group(2).strip()]}
            last_num = num
        elif current is not None:
            if line:
                current["lines"].append(line)
        elif line:
            preamble.append(line)
    if current:
        entries.append(current)
    return preamble, entries


def parse_post(text):
    """
    Turn the plain text of one post into {'title':…, 'entries':[(num, word, defn, head)]}
    or None if the post is not a vocabulary list.
    """
    if not text:
        return None
    lines = text.split("\n")
    head_blob = " ".join(lines[:3]).lower()
    if "#essay" in head_blob or head_blob.strip().startswith("essay"):
        return None

    preamble, raw_entries = split_entries(lines, ENTRY_DOT)
    if len(raw_entries) < 2:  # fall back to "1)" style numbering
        preamble, raw_entries = split_entries(lines, ENTRY_ANY)
    if len(raw_entries) < 2:
        return None

    title = ""
    for line in preamble:
        cand = _clean(line)
        if cand and cand.lower() not in ("vocabularium", "phrases:"):
            title = cand
            break

    entries = []
    for e in raw_entries:
        first = _clean(e["lines"][0])
        m = HEAD_SPLIT.match(first)
        if m:
            head = m.group("head")
            rest = m.group("rest").strip()
        else:  # headword alone on its line
            head, rest = first, ""
        word = clean_word(head)
        if not word:
            continue
        body = [rest] if rest else []
        body.extend(_clean(l) for l in e["lines"][1:])
        definition = "\n".join(b for b in body if b).strip()
        if not definition:
            definition = _clean(head)
        entries.append((e["num"], word, definition, head))

    if not entries:
        return None
    return {"title": title, "entries": entries}


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------
def fetch_posts(base_url=BASE_URL, max_pages=MAX_PAGES, sleep=0.4, log=print):
    """Walk the channel's public web preview and return posts sorted oldest-first."""
    posts = {}
    url = base_url
    seen_urls = set()
    pages = 0
    while url and pages < max_pages:
        if url in seen_urls:
            break
        seen_urls.add(url)
        pages += 1
        res = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        wraps = soup.find_all("div", class_="tgme_widget_message_wrap")
        if not wraps:
            break
        for wrap in wraps:
            link = wrap.find("a", class_="tgme_widget_message_date")
            href = link.get("href", "") if link else ""
            m = re.search(r"/(\d+)\s*$", href)
            post_id = int(m.group(1)) if m else None
            if post_id is None or post_id in posts:
                continue
            body = wrap.find("div", class_="tgme_widget_message_text")
            if not body:
                continue
            for br in body.find_all("br"):
                br.replace_with("\n")
            time_tag = wrap.find("time")
            dt_full = time_tag.get("datetime", "") if time_tag else ""
            posts[post_id] = {
                "id": post_id,
                "dt": dt_full[:10],
                "dt_full": dt_full,
                "text": body.get_text(),
            }
        more = soup.find("a", class_="tme_messages_more", href=True)
        if not more:
            more = soup.find("a", href=lambda x: x and "?before=" in x)
        if more and "?before=" in more["href"]:
            href = more["href"]
            url = href if href.startswith("http") else "https://t.me" + href
            log(f"  fetched page {pages} ({len(posts)} posts so far)")
            time.sleep(sleep)
        else:
            url = None
    return [posts[k] for k in sorted(posts)]


def build_lists(posts, log=print):
    """Parse posts (oldest first) into vocabulary lists, merging continuations."""
    lists = []
    for post in posts:
        parsed = parse_post(post["text"])
        if not parsed:
            continue
        first_num = parsed["entries"][0][0]
        if not parsed["title"] and first_num > 1 and lists:
            prev = lists[-1]
            prev["entries"].extend(parsed["entries"])
            prev["post_ids"].append(post["id"])
            log(f"  post {post['id']}: continuation of '{prev['title'][:40]}' "
                f"(+{len(parsed['entries'])} words)")
            continue
        lists.append({
            "dt": post["dt"],
            "dt_full": post["dt_full"],
            "post_id": post["id"],
            "post_ids": [post["id"]],
            "title": parsed["title"] or f"List from {post['dt']}",
            "entries": list(parsed["entries"]),
        })
        log(f"  post {post['id']}: '{lists[-1]['title'][:50]}' "
            f"({len(parsed['entries'])} words)")
    return lists


# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE lists (
    ord     INTEGER PRIMARY KEY,
    date    TEXT,
    title   TEXT,
    post_id INTEGER
);
CREATE TABLE words (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    word       TEXT NOT NULL,
    display    TEXT,
    definition TEXT,
    list_id    INTEGER,
    pos        INTEGER
);
CREATE TABLE family (
    fword    TEXT NOT NULL,
    headword TEXT NOT NULL,
    rel      TEXT DEFAULT 'family',
    PRIMARY KEY (fword, headword)
);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX idx_words_word ON words(word);
CREATE INDEX idx_words_list ON words(list_id, pos);
CREATE INDEX idx_family_fword ON family(fword);
"""


def write_database(lists, db_path=DB_PATH):
    """Build a fresh database next to the live one, then swap it in atomically."""
    tmp_path = db_path + ".new"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    conn = sqlite3.connect(tmp_path)
    conn.executescript(SCHEMA)
    cur = conn.cursor()
    n_words = 0
    for i, lst in enumerate(lists, 1):
        cur.execute("INSERT INTO lists VALUES (?,?,?,?)",
                    (i, lst["dt"], lst["title"], lst["post_id"]))
        for pos, word, definition, head in lst["entries"]:
            cur.execute(
                "INSERT INTO words (word, display, definition, list_id, pos) VALUES (?,?,?,?,?)",
                (word.lower(), word, definition, i, pos))
            n_words += 1
            rels = [(a, "alias") for a in word_aliases(head, word)]
            rels += extract_relations(definition, word)
            for fword, rel in rels:
                cur.execute(
                    "INSERT OR IGNORE INTO family (fword, headword, rel) VALUES (?,?,?)",
                    (fword.lower(), word.lower(), rel))
    cur.execute("INSERT INTO meta VALUES ('updated_at', ?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"),))
    cur.execute("INSERT INTO meta VALUES ('n_lists', ?)", (str(len(lists)),))
    cur.execute("INSERT INTO meta VALUES ('n_words', ?)", (str(n_words),))
    conn.commit()
    conn.close()
    os.replace(tmp_path, db_path)          # atomic: the old DB stays until this point
    return len(lists), n_words


def update_database(db_path=DB_PATH, log=print):
    """
    Full refresh.  Raises on network/parse failure *before* touching the live
    database, so a failed update can never wipe the existing data.
    """
    log("Fetching channel...")
    posts = fetch_posts(log=log)
    if not posts:
        raise RuntimeError("no posts found - is the channel reachable?")
    log(f"Parsing {len(posts)} posts...")
    lists = build_lists(posts, log=log)
    if not lists:
        raise RuntimeError(f"fetched {len(posts)} posts but found no vocabulary lists")
    n_lists, n_words = write_database(lists, db_path)
    log(f"Done: {n_lists} lists, {n_words} words.")
    return n_lists, n_words


# --------------------------------------------------------------------------
def _dump(db_path, n):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT date, title FROM lists WHERE ord=?", (n,)).fetchone()
    if not row:
        print("no such list")
        return
    print(f"List {n}. {row[0]} {row[1]}\n")
    for pos, w, d in conn.execute(
            "SELECT pos, display, definition FROM words WHERE list_id=? ORDER BY pos", (n,)):
        print(f"{pos}. {w} - {d}\n")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--dump", type=int, help="print one list from the existing DB")
    args = ap.parse_args()
    if args.dump:
        _dump(args.db, args.dump)
        sys.exit(0)
    try:
        update_database(args.db)
    except Exception as exc:  # noqa: BLE001
        print(f"UPDATE FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
