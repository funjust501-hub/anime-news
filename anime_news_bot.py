#!/usr/bin/env python3
"""
Anime / Manga / Manhwa News Bot  (v2)
Posts NEW stories to Telegram and/or Discord in this style:

  🚨 HEADLINE IN CAPS

  Short body in 2-3 short paragraphs.

  Engagement question?

  #Anime #AnimeNews

  🔗 source link            (+ the story's image when the feed has one)

Environment variables:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   -> Telegram
  DISCORD_WEBHOOK_URL                    -> Discord (optional)
  ANTHROPIC_API_KEY                      -> optional: Claude rewrites each story
                                            in its own words + writes the question
No extra libraries needed (Python 3.8+).
"""
import os, sys, json, time, re, html, hashlib, random
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET

# (name, url, needs_topic_filter)
GNEWS = "https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q="
FEEDS = [
    ("Anime News Network", "https://www.animenewsnetwork.com/all/rss.xml", True),
    ("MyAnimeList", "https://myanimelist.net/rss/news.xml", False),
    ("Crunchyroll News", "https://cr-news-api-service.prd.crunchyrollsvc.com/v1/en-US/rss", False),
    ("Anime Corner", "https://animecorner.me/feed/", False),
    ("Otaku USA", "https://otakuusamagazine.com/feed/", True),
    ("Google News (Manhwa)", GNEWS + urllib.parse.quote("manhwa OR webtoon OR manhua when:2d"), False),
    ("Google News (Manga)", GNEWS + urllib.parse.quote("manga announcement OR \"manga adaptation\" when:2d"), False),
]

TOPIC_WORDS = re.compile(
    r"\b(anime|manga|manhwa|manhua|webtoon|light novel|isekai|shonen|seinen|"
    r"shojo|ova|season \d|episode|chapter|volume|adaptation|trailer|studio)\b", re.I)
MANHWA_WORDS = re.compile(r"\b(manhwa|webtoon|manhua|korean|naver|kakao)\b", re.I)
MANGA_WORDS = re.compile(r"\b(manga|light novel|chapter|serializ|volume|tankobon|magazine)\b", re.I)

QUESTIONS = {
    "Manhwa": ["Are you reading this one?", "Which manhwa deserves an anime next?",
               "Would you watch this as an anime?"],
    "Manga": ["Are you reading this manga?", "Do you think the anime will do it justice?",
              "What chapter hit you the hardest?"],
    "Anime": ["Are you hyped for this?", "What would you like to see next?",
              "Will you be watching this?"],
}

SEEN_FILE = "seen.json"
MAX_POSTS_PER_RUN = 5
UA = {"User-Agent": "Mozilla/5.0 (AnimeNewsBot)"}

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")
DISCORD = os.getenv("DISCORD_WEBHOOK_URL")
AI_KEY = os.getenv("ANTHROPIC_API_KEY")


# ---------- helpers ----------
def http(url, data=None, headers=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(text):
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def local(tag):
    return tag.split("}")[-1]


def find_image(it):
    for el in it.iter():
        t = local(el.tag)
        url = el.get("url") or ""
        if t in ("thumbnail", "content") and url:
            if el.get("medium") == "image" or re.search(r"\.(jpe?g|png|webp)(\?|$)", url, re.I) \
                    or t == "thumbnail":
                return url
        if t == "enclosure" and "image" in (el.get("type") or "") and url:
            return url
    blob = " ".join(html.unescape(el.text or "") for el in it.iter()
                    if local(el.tag) in ("description", "encoded"))
    m = re.search(r"<img[^>]+src=[\"']([^\"']+)", blob)
    return m.group(1) if m else None


def fetch_feed(name, url, filt):
    items = []
    try:
        root = ET.fromstring(http(url))
    except Exception as e:
        print(f"[!] {name} failed: {e}")
        return items
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        title = clean(it.findtext("title"))
        if not link or not title:
            continue
        if name.startswith("Google News"):
            title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)  # drop " - Publisher"
        summary = clean(it.findtext("description"))
        if summary.lower().startswith(title.lower()[:30]):
            summary = ""  # google news repeats the title
        text = f"{title} {summary}"
        if filt and not TOPIC_WORDS.search(text):
            continue
        items.append({"source": name, "title": title, "link": link,
                      "summary": summary[:600], "image": find_image(it)})
    return items


def topic_of(item):
    text = f"{item['title']} {item['summary']}"
    if MANHWA_WORDS.search(text):
        return "Manhwa"
    if MANGA_WORDS.search(text) and not re.search(r"\banime\b", item["title"], re.I):
        return "Manga"
    return "Anime"


def uid(item):
    return hashlib.md5(item["link"].encode()).hexdigest()


def words(t):
    return set(re.findall(r"[a-z0-9]{3,}", t.lower()))


def same_story(a, b):
    wa, wb = words(a), words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) > 0.6


# ---------- state ----------
def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return dict.fromkeys(json.load(f))
    except Exception:
        return None  # first run


def save_seen(seen):
    keys = list(seen)
    markers = [k for k in keys if k.startswith("feed:")]
    ids = [k for k in keys if not k.startswith("feed:")][-4000:]
    with open(SEEN_FILE, "w") as f:
        json.dump(ids + markers, f)


# ---------- writing the post ----------
def ai_write(item):
    if not AI_KEY:
        return {}
    prompt = (
        "You write posts for an anime/manga/manhwa news page. Rewrite this story "
        "in your OWN words (never copy sentences). Reply with ONLY JSON:\n"
        '{"body": "2-3 short paragraphs separated by \\n\\n, max 70 words total",'
        ' "question": "one short engagement question for fans",'
        ' "series_hashtag": "main series/title as a hashtag like #TheApothecaryDiaries or empty"}\n\n'
        f"Title: {item['title']}\nSummary: {item['summary']}\nSource: {item['source']}")
    body = json.dumps({"model": "claude-sonnet-4-6", "max_tokens": 400,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    try:
        out = json.loads(http("https://api.anthropic.com/v1/messages", body, {
            "content-type": "application/json", "x-api-key": AI_KEY,
            "anthropic-version": "2023-06-01"}, timeout=40))
        txt = out["content"][0]["text"]
        txt = re.sub(r"```json|```", "", txt).strip()
        return json.loads(txt)
    except Exception as e:
        print(f"[!] AI write failed: {e}")
        return {}


def template_body(item):
    s = item["summary"]
    if not s:
        return "Fans are talking about this one. Details at the link below."
    sentences = re.split(r"(?<=[.!?])\s+", s)
    return " ".join(sentences[:2])[:300]


def build_text(item, ai, limit=1000):
    topic = topic_of(item)
    body = (ai.get("body") or template_body(item)).strip()
    question = (ai.get("question") or random.choice(QUESTIONS[topic])).strip()
    tags = [f"#{topic}", f"#{topic}News"]
    series = (ai.get("series_hashtag") or "").strip()
    if re.fullmatch(r"#[A-Za-z0-9_]{2,40}", series):
        tags.insert(0, series)
    head = f"🚨 {item['title'].upper()}"
    tail = f"\n\n{question}\n\n{' '.join(tags)}\n\n🔗 {item['link']}"
    room = limit - len(head) - len(tail) - 2
    if len(body) > room:
        body = body[:max(room - 1, 0)].rstrip() + "…"
    return f"{head}\n\n{body}{tail}"


# ---------- sending ----------
def post(item):
    text = build_text(item, ai_write(item))
    img = item.get("image")
    sent = False
    if TG_TOKEN and TG_CHAT:
        base = f"https://api.telegram.org/bot{TG_TOKEN}"
        try:
            if img:
                try:
                    http(f"{base}/sendPhoto", urllib.parse.urlencode(
                        {"chat_id": TG_CHAT, "photo": img, "caption": text}).encode())
                    sent = True
                except Exception as e:
                    print(f"[!] photo failed ({e}), sending text only")
            if not sent:
                http(f"{base}/sendMessage", urllib.parse.urlencode(
                    {"chat_id": TG_CHAT, "text": text}).encode())
                sent = True
        except Exception as e:
            print(f"[!] Telegram failed: {e}")
    if DISCORD:
        payload = {"content": text[:1900]}
        if img:
            payload["embeds"] = [{"image": {"url": img}}]
        try:
            http(DISCORD, json.dumps(payload).encode(), {"content-type": "application/json"})
            sent = True
        except Exception as e:
            print(f"[!] Discord failed: {e}")
    if not (TG_TOKEN or DISCORD):
        print("\n" + text + ("\n[image] " + img if img else "") + "\n" + "-" * 40)
        sent = True
    return sent


# ---------- main ----------
def run_once():
    seen = load_seen()
    first_run = seen is None
    seen = seen or {}
    queue = []
    for name, url, filt in FEEDS:
        items = fetch_feed(name, url, filt)
        marker = f"feed:{name}"
        if first_run or marker not in seen:
            # brand-new source: mark its current stories as seen, post nothing
            if items:
                for item in items:
                    seen[uid(item)] = None
                seen[marker] = None
                print(f"Seeded {name}: {len(items)} existing stories skipped.")
            continue
        for item in items:
            if uid(item) in seen:
                continue
            if any(same_story(item["title"], q["title"]) for q in queue):
                seen[uid(item)] = None  # same news from another source
                continue
            queue.append(item)
    posted = 0
    for item in queue:
        if posted >= MAX_POSTS_PER_RUN:
            break
        if post(item):
            seen[uid(item)] = None
            posted += 1
            time.sleep(2)
    save_seen(seen)
    print(f"Done. {posted} posted, {len(queue)} new found.")


if __name__ == "__main__":
    if "--loop" in sys.argv:
        i = sys.argv.index("--loop")
        wait = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 600
        while True:
            run_once()
            time.sleep(wait)
    else:
        run_once()
