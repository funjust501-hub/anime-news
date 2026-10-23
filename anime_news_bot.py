#!/usr/bin/env python3
"""
Anime News Bot
Watches RSS feeds and posts NEW stories to Telegram and/or Discord.
No extra libraries needed (Python 3.8+).

Setup (set as environment variables):
  TELEGRAM_BOT_TOKEN   token from @BotFather
  TELEGRAM_CHAT_ID     your channel/chat id (e.g. @mychannel or -100123...)
  DISCORD_WEBHOOK_URL  Discord channel webhook URL
  ANTHROPIC_API_KEY    (optional) writes a short news-style caption per story

Run once:      python anime_news_bot.py
Run in a loop: python anime_news_bot.py --loop 600     (checks every 10 min)
"""
import os, sys, json, time, re, html, hashlib
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET

FEEDS = {
    "Anime News Network": "https://www.animenewsnetwork.com/all/rss.xml",
    "MyAnimeList": "https://myanimelist.net/rss/news.xml",
    "Crunchyroll News": "https://cr-news-api-service.prd.crunchyrollsvc.com/v1/en-US/rss",
}

SEEN_FILE = "seen.json"
MAX_POSTS_PER_RUN = 5          # avoids flooding your channel
UA = {"User-Agent": "Mozilla/5.0 (AnimeNewsBot)"}

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")
DISCORD = os.getenv("DISCORD_WEBHOOK_URL")
AI_KEY = os.getenv("ANTHROPIC_API_KEY")


def http(url, data=None, headers=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def fetch_feed(name, url):
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
        items.append({
            "source": name,
            "title": title,
            "link": link,
            "summary": clean(it.findtext("description"))[:500],
        })
    return items


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except Exception:
        return None  # None = first run


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen)[-3000:], f)


def uid(item):
    return hashlib.md5(item["link"].encode()).hexdigest()


def caption(item):
    """Optional: short punchy news-style caption via Claude."""
    if not AI_KEY:
        return item["summary"][:220]
    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 200,
        "messages": [{"role": "user", "content":
            "Rewrite this anime news as a short, punchy 2-sentence social post "
            "in your own words. No hashtags, no emojis.\n\n"
            f"Title: {item['title']}\nSummary: {item['summary']}"}],
    }).encode()
    try:
        out = json.loads(http("https://api.anthropic.com/v1/messages", body, {
            "content-type": "application/json",
            "x-api-key": AI_KEY,
            "anthropic-version": "2023-06-01",
        }, timeout=40))
        return out["content"][0]["text"].strip()
    except Exception as e:
        print(f"[!] caption failed: {e}")
        return item["summary"][:220]


def post(item):
    cap = caption(item)
    text = f"📰 {item['title']}\n\n{cap}\n\n🔗 {item['link']}\nSource: {item['source']}"
    sent = False
    if TG_TOKEN and TG_CHAT:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
        try:
            http(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data)
            sent = True
        except Exception as e:
            print(f"[!] Telegram failed: {e}")
    if DISCORD:
        data = json.dumps({"content": text[:1900]}).encode()
        try:
            http(DISCORD, data, {"content-type": "application/json"})
            sent = True
        except Exception as e:
            print(f"[!] Discord failed: {e}")
    if not (TG_TOKEN or DISCORD):
        print("\n" + text + "\n" + "-" * 40)  # console mode
        sent = True
    return sent


def run_once():
    seen = load_seen()
    first_run = seen is None
    seen = seen or set()
    new = []
    for name, url in FEEDS.items():
        for item in fetch_feed(name, url):
            if uid(item) not in seen:
                new.append(item)
    if first_run:
        # Don't spam old news on the very first run
        for item in new:
            seen.add(uid(item))
        save_seen(seen)
        print(f"First run: marked {len(new)} existing stories as seen. New ones will post from now on.")
        return
    for item in new[:MAX_POSTS_PER_RUN]:
        if post(item):
            seen.add(uid(item))
        time.sleep(2)
    save_seen(seen)
    print(f"Done. {min(len(new), MAX_POSTS_PER_RUN)} posted, {len(new)} new found.")


if __name__ == "__main__":
    if "--loop" in sys.argv:
        i = sys.argv.index("--loop")
        wait = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 600
        while True:
            run_once()
            time.sleep(wait)
    else:
        run_once()
