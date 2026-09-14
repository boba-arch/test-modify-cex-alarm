import re
import json
import os
import time
import sqlite3
import logging
import threading
import requests
import feedparser
from datetime import datetime, timezone
from apscheduler.schedulers.blocking import BlockingScheduler
from bs4 import BeautifulSoup

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ.get("BOT_TOKEN")
CHANNEL_ID     = os.environ.get("CHANNEL_ID")
DEEPL_API_KEY  = os.environ.get("DEEPL_API_KEY")
OWNER_CHAT_ID  = os.environ.get("OWNER_CHAT_ID")  # chat ID pribadi kamu — otorisasi command /test
CHECK_EVERY    = 2
DB_PATH        = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", ".") + "/seen.db"

if not BOT_TOKEN or not CHANNEL_ID:
    raise ValueError("BOT_TOKEN dan CHANNEL_ID harus diisi di Railway Variables!")

# DeepL free-tier keys end with ":fx" and must hit the api-free host; paid keys
# use api.deepl.com. This picks the right host automatically from the key.
DEEPL_API_URL = (
    "https://api-free.deepl.com/v2/translate"
    if DEEPL_API_KEY and DEEPL_API_KEY.endswith(":fx")
    else "https://api.deepl.com/v2/translate"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

if not DEEPL_API_KEY:
    log.warning("⚠️ DEEPL_API_KEY tidak diisi — translasi akan dilewati (pakai teks asli).")

if not OWNER_CHAT_ID:
    log.warning("⚠️ OWNER_CHAT_ID tidak diisi — command /test akan menerima perintah dari SIAPA SAJA yang chat bot ini. Disarankan diisi.")

GATE_BUILD_ID = "Fn6h1ESDRJ7ImYZYPgoXC"

HEADERS_GATE = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.gate.com/announcements/lastest",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

# ─── KEYWORDS ──────────────────────────────────────────────────────────────────
KEYWORDS = [
    # Delisting
    "delist", "delisting", "will delist", "to delist",
    "removal", "remove trading pair", "trading pair removal", "end support", "temporarily paused",
    "cease trading", "suspend trading", "discontinue", "cease support", "temporarily closed", "monitoring tag", "st tag", "special treatment",
    # Migration & Contract
    "migration", "migrate", "token migration", "contract swap",
    "contract change", "contract address", "new contract", "token swap",
    "token rebranding", "rebrand", "transition", "discontinuation", "discontinue",
    # Ticker & Symbol
    "ticker change", "ticker symbol", "symbol change",
    "rename", "rebranding", "tick size",
    # Network & Upgrade
    "network upgrade", "network support termination",
    "mainnet upgrade", "mainnet launch",
    "hard fork", "hardfork", "hard-fork",
    "chain upgrade", "protocol upgrade", "suspension",
    "software upgrade", "node upgrade", "suspending", "resuming", "suspend", "resume",
    # Deposit/withdrawal
    "disable", "disabled", "suspend deposit", "suspend withdrawal", "completes integration", "maintenance", "wallet maintenance",
    # Snapshot
    "snapshot", "airdrop snapshot",
    # Notice
    "notice of removal", "important notice", "investment warning",
]

# ─── UPBIT KEYWORDS (Korea + Inggris) ─────────────────────────────────────────
UPBIT_KEYWORDS = [
    # English 
    "investment warning",
    "delisting",
    "trading support termination",

    # Korean — delisting
    "거래지원 종료",
    "상장폐지",

    # Korean — caution/early-warning stage
    "유의 종목",
    "유의종목",
    "투자유의",

    # Korean — deposit/withdrawal suspension
    "입출금 중단",
    "입출금 일시 중단",
]

def is_relevant_upbit(text: str) -> bool:
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in UPBIT_KEYWORDS)


# ─── TRANSLATION HELPER ────────────────────────────────────────────────────────
BAD_TRANSLATION_MARKERS = [
    "that's an error", "that’s an error",
    "error 500", "error 404", "error 403",
    "that's all we know", "that’s all we know",
    "<html", "<!doctype", "<body",
]

def _is_bad_translation(result: str, original: str) -> bool:
    if not result:
        return True
    if len(result) > max(200, len(original) * 5):
        return True
    low = result.lower()
    return any(marker in low for marker in BAD_TRANSLATION_MARKERS)

def _deepl_translate(text: str, target_lang: str) -> str:
    if not text or not DEEPL_API_KEY:
        return text
    try:
        r = requests.post(
            DEEPL_API_URL,
            headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"},
            data={"text": text, "target_lang": target_lang},
            timeout=(5, 15),  # (connect timeout, read timeout) — lebih defensif dari hang
        )
        r.raise_for_status()
        translations = r.json().get("translations", [])
        if not translations:
            log.warning(f"⚠️ Respons DeepL tanpa hasil translasi, pakai teks asli.")
            return text
        result = translations[0].get("text", text)
        if _is_bad_translation(result, text):
            log.warning(f"⚠️ Gagal translate, pakai judul asli. Raw: {str(result)[:80]}")
            return text
        return result
    except Exception as e:
        log.error(f"⚠️ Gagal translate ke {target_lang} (DeepL): {e}")
        return text

def translate_to_en(text: str) -> str:
    return _deepl_translate(text, "EN-US")

def translate_to_zh(text: str) -> str:
    return _deepl_translate(text, "ZH")


# ─── CEX SOURCES ───────────────────────────────────────────────────────────────
SOURCES = [
    # ── Binance ──
    {
        "name": "Binance",
        "type": "binance_api",
        "catalog_id": 161,
        "url": "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&pageNo=1&pageSize=20&catalogId=161",
        "logo": "🟡",
        "base_link": "https://www.binance.com/en/support/announcement/",
    },
    {
        "name": "Binance",
        "type": "binance_api",
        "catalog_id": 157,
        "url": "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&pageNo=1&pageSize=20&catalogId=157",
        "logo": "🟡",
        "base_link": "https://www.binance.com/en/support/announcement/",
    },
    {
        "name": "Binance",
        "type": "binance_api",
        "catalog_id": 49,
        "url": "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&pageNo=1&pageSize=20&catalogId=49",
        "logo": "🟡",
        "base_link": "https://www.binance.com/en/support/announcement/",
    },
    # ── Bybit ──
    {
        "name": "Bybit",
        "type": "scrape",
        "url": "https://announcements.bybit.com/en/?category=&page=1",
        "logo": "🟠",
    },
    # ── Bitfinex ──
    {
        "name": "Bitfinex",
        "type": "bitfinex_api",
        "url": "https://api-pub.bitfinex.com/v2/posts/hist?limit=20&type=1",
        "logo": "🟢",
        "base_link": "https://www.bitfinex.com/posts/",
    },
    # ── Coinbase ──
    {
        "name": "Coinbase",
        "type": "rss",
        "url": "https://status.coinbase.com/history.rss",
        "logo": "⚪",
    },
    # ── OKX ──
    {
        "name": "OKX",
        "type": "scrape",
        "url": "https://www.okx.com/help/section/announcements-latest-announcements",
        "logo": "⚫",
    },
    # ── Crypto.com ──
    {
        "name": "Crypto-com",
        "type": "cryptocom_api",
        "url": "https://static2.crypto.com/exchange/announcements_en.json",
        "logo": "🔶",
        "base_link": "https://crypto.com/exchange/announcements/all",
    },
    # ── KuCoin ──
    {
        "name": "Kucoin",
        "type": "kucoin_api",
        "url": "https://api.kucoin.com/api/ua/v1/market/announcement?annType=latest-announcements&lang=en_US&page=1&pageSize=20",
        "logo": "🟢",
    },
    # ── Gate.io ──
    {
        "name": "Gate-io",
        "type": "gate_scrape",
        "url": "https://www.gate.com/announcements/lastest",
        "logo": "🔵",
    },
    # ── MEXC ──
    {
        "name": "MEXC",
        "type": "scrape",
        "url": "https://www.mexc.com/announcements/all",
        "logo": "🔷",
    },
    # ── Bitget ──
    {
        "name": "Bitget",
        "type": "bitget_scrape",
        "url": "https://www.bitget.com/support/sections/12508313443483",
        "logo": "🟣",
        "base_link": "https://www.bitget.com",
    },
    # ── Poloniex ──
    {
        "name": "Poloniex",
        "type": "scrape",
        "url": "https://support.poloniex.com/hc/en-us/sections/360006455114-Latest-Announcements",
        "logo": "🔴",
    },
    # ── HTX ──
    {
        "name": "HTX",
        "type": "scrape",
        "url": "https://www.htx.com/support/",
        "logo": "🟤",
    },
        # ── Upbit ──
    {
        "name": "Upbit",
        "type": "upbit_api",
        "url": "https://pub-info.upbit.com/api/v1/announcements",
        "logo": "♦️",
        "base_link": "https://upbit.com/service_center/notice?id=",
    },
]

# ─── DATABASE ──────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            id      TEXT PRIMARY KEY,
            seen_at TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    con.commit()
    con.close()

def is_seen(uid):
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT 1 FROM seen WHERE id=?", (uid,)).fetchone()
    con.close()
    return row is not None

def mark_seen(uid):
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (uid, datetime.now(timezone.utc).isoformat()))
    con.commit()
    con.close()

def try_mark_seen(uid) -> bool:
    """
    Cek-dan-tandai 'sudah dilihat' dalam SATU operasi atomik (INSERT OR IGNORE),
    bukan is_seen() lalu mark_seen() terpisah. Ini menutup celah race condition:
    kalau dua proses/replica jalan bersamaan dan sama-sama ngecek uid yang sama
    di waktu hampir bersamaan, hanya proses yang commit INSERT duluan yang akan
    dapat rowcount > 0 (artinya "genuinely new") — proses lain otomatis kalah
    karena constraint PRIMARY KEY, walau connection-nya beda.
    Return True kalau uid ini baru pertama kali ditandai (boleh lanjut kirim),
    False kalau sudah pernah ada (skip, jangan kirim lagi).
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        "INSERT OR IGNORE INTO seen VALUES (?,?)",
        (uid, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    is_new = cur.rowcount > 0
    con.close()
    return is_new

def is_baseline_done():
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT value FROM meta WHERE key='baseline_done'").fetchone()
    con.close()
    return row is not None and row[0] == "1"

def set_baseline_done():
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('baseline_done', '1')")
    con.commit()
    con.close()

# ─── KEYWORD CHECK ─────────────────────────────────────────────────────────────
def is_relevant(text):
    return any(kw in text.lower() for kw in KEYWORDS)

def normalize_uid(href: str) -> str:
    m = re.match(r'^(.*)-\d{8,}$', href.rstrip('/'))
    return m.group(1) if m else href

# ─── TELEGRAM SENDER ───────────────────────────────────────────────────────────
def send_announcement(logo, cex, title, link, translate_title=False):
    # Saat baseline (baris pertama kali dijalankan), pesan tidak pernah benar-benar
    # dikirim ke Telegram — jadi jangan buang waktu/kuota DeepL untuk translate
    # judul yang hasilnya toh dibuang.
    if not is_baseline_done():
        return
    if translate_title:
        title = translate_to_en(title)
    send_telegram(format_message(logo, cex, title, link))

def send_telegram(message):
    if not is_baseline_done():
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("✅ Pesan terkirim ke channel")
    except Exception as e:
        log.error(f"❌ Gagal kirim ke Telegram: {e}")

def format_message(logo, cex, title, link):
    title_cn = translate_to_zh(title)
    return (
        f"{logo} <b>[{cex}]</b>\n"
        f"{title}\n"
        f"\n"
        f"[{cex}]\n"
        f"{title_cn}\n"
        f"🔗 <a href='{link}'>Announcement</a>"
    )

# ─── COMMAND /test (TOMBOL TES DI TELEGRAM) ────────────────────────────────────
def _telegram_api(method, payload=None, timeout=20):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    r = requests.post(url, json=payload or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()

def _send_with_test_button(chat_id, text):
    """Balas ke chat_id dengan pesan + tombol inline '🔁 Test Lagi'."""
    try:
        _telegram_api("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": {
                "inline_keyboard": [[{"text": "🔁 Test Lagi", "callback_data": "run_test"}]]
            },
        })
    except Exception as e:
        log.error(f"❌ Gagal balas command /test: {e}")

def run_dummy_test(chat_id):
    """Bikin dummy announcement, translate via DeepL, kirim ke CHANNEL_ID
    (label jelas TEST), lalu balas konfirmasi + tombol 'Test Lagi' ke chat_id
    yang minta. Sengaja tidak lewat send_telegram()/is_baseline_done() supaya
    tes bisa jalan kapan saja, gak perlu nunggu baseline selesai."""
    dummy_title = "Binance Will Delist BTTOLD, MBL and ANT on 2026-09-20 (DUMMY TEST)"
    dummy_link  = "https://example.com/test-announcement"
    try:
        message = format_message("🧪", "TEST", dummy_title, dummy_link)
        _telegram_api("sendMessage", {
            "chat_id": CHANNEL_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        _send_with_test_button(
            chat_id,
            "✅ Dummy alert terkirim ke channel & translate DeepL jalan normal."
        )
        log.info("✅ /test: dummy alert terkirim & translate sukses")
    except Exception as e:
        _send_with_test_button(chat_id, f"❌ Test gagal: {e}")
        log.error(f"❌ /test gagal: {e}")

def poll_telegram_commands():
    """Long-polling sederhana buat nangkep command '/test' dan tombol
    '🔁 Test Lagi' dari chat pribadi kamu ke bot. Jalan di thread terpisah,
    gak ganggu scheduler check_all() yang jalan di thread utama."""
    log.info("🕹️  Listener command /test aktif...")
    offset = None
    while True:
        try:
            resp = _telegram_api("getUpdates", {"timeout": 30, "offset": offset})
            for update in resp.get("result", []):
                offset = update["update_id"] + 1

                msg = update.get("message")
                if msg and msg.get("text") == "/test":
                    chat_id = str(msg["chat"]["id"])
                    if OWNER_CHAT_ID and chat_id != str(OWNER_CHAT_ID):
                        log.warning(f"⚠️ Command /test diabaikan dari chat_id tidak dikenal: {chat_id}")
                        continue
                    run_dummy_test(chat_id)

                cb = update.get("callback_query")
                if cb and cb.get("data") == "run_test":
                    chat_id = str(cb["message"]["chat"]["id"])
                    if OWNER_CHAT_ID and chat_id != str(OWNER_CHAT_ID):
                        continue
                    _telegram_api("answerCallbackQuery", {
                        "callback_query_id": cb["id"],
                        "text": "Menjalankan test...",
                    })
                    run_dummy_test(chat_id)
        except Exception as e:
            log.error(f"⚠️ Error polling command /test: {e}")
            time.sleep(5)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# ─── FETCHERS ──────────────────────────────────────────────────────────────────

def fetch_binance_api(source):
    cat = source.get("catalog_id", "?")
    log.info(f"🔌 Cek API: Binance (catalog {cat})")
    try:
        r = requests.get(source["url"], headers=HEADERS, timeout=15)
        data = r.json()
        catalogs = data.get("data", {}).get("catalogs", [])
        articles = []
        if catalogs:
            for cat_data in catalogs:
                if str(cat_data.get("catalogId")) == str(source.get("catalog_id")):
                    articles = cat_data.get("articles", [])
                    break
            if not articles:
                for cat_data in catalogs:
                    articles.extend(cat_data.get("articles", []))
        else:
            articles = data.get("data", {}).get("articles", [])

        log.info(f"   → {len(articles)} artikel ditemukan")
        for article in articles:
            title = article.get("title", "")
            code  = article.get("code", "")
            if not code or not is_relevant(title):
                continue
            uid = f"binance_{code}"
            if not try_mark_seen(uid):
                continue
            link = f"{source['base_link']}{code}"
            send_announcement(source["logo"], source["name"], title, link)
            time.sleep(1)
    except Exception as e:
        log.error(f"❌ Error API Binance: {e}")


def fetch_rss(source: dict):
    log.info(f"📶 Cek RSS: {source['name']}")
    try:
        r = requests.get(source["url"], headers=HEADERS, timeout=15)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
        log.info(f"   → status:{r.status_code} | {len(feed.entries)} artikel ditemukan")
        for entry in feed.entries:
            title   = entry.get("title", "")
            summary = entry.get("summary", "")
            link    = entry.get("link", "")
            uid     = entry.get("id", link)
            combined_text = f"{title} {summary}"
            if not uid or not is_relevant(combined_text):
                continue
            if not try_mark_seen(uid):
                continue
            send_announcement(source["logo"], source["name"], title, link)
            time.sleep(1)
    except Exception as e:
        log.error(f"❌ Error RSS {source['name']}: {e}")


def fetch_gate_scrape(source):
    log.info(f"🕷️  Scrape: Gate.io")
    try:
        url = f"https://www.gate.com/announcements/_next/data/{GATE_BUILD_ID}/en/announcements/lastest.json?category=lastest"
        r = requests.get(url, headers=HEADERS_GATE, timeout=15)
        log.info(f"   → status: {r.status_code} | len: {len(r.text)}")

        if r.status_code == 404:
            log.error("❌ Gate.io: 404 — GATE_BUILD_ID sudah basi, perlu diupdate manual")
            return

        if r.status_code != 200:
            log.error(f"❌ Gate.io: status code {r.status_code}")
            log.error(f"   cuplikan: {r.text[:300]}")
            return

        data = r.json()
        articles = (
            data.get("pageProps", {})
                .get("listData", {})
                .get("list", [])
        )
        log.info(f"   → {len(articles)} artikel ditemukan")
        for a in articles:
            title = a.get("title", "")
            aid = a.get("id", "")
            url_path = a.get("url", "")
            if not aid or len(title) < 10 or not is_relevant(title):
                continue
            uid = f"gate_{aid}"
            if not try_mark_seen(uid):
                continue
            link = f"https://www.gate.com{url_path}" if url_path else f"https://www.gate.com/announcements/article/{aid}"
            send_announcement(source["logo"], source["name"], title, link)
            time.sleep(1)
    except Exception as e:
        log.error(f"❌ Error scrape Gate.io: {e}")


def fetch_bitfinex_api(source):
    log.info("🔌 Cek API: Bitfinex")
    try:
        r = requests.get(source["url"], headers=HEADERS, timeout=15)
        items = r.json()
        log.info(f"   → {len(items)} artikel ditemukan")
        for item in items:
            if not isinstance(item, list) or len(item) < 5:
                continue
            post_id = item[0]
            title   = item[3] or ""
            body    = item[4] or ""
            if not post_id:
                continue
            combined_text = f"{title} {body}"
            if not is_relevant(combined_text):
                continue
            uid = f"bitfinex_{post_id}"
            if not try_mark_seen(uid):
                continue
            link = f"{source['base_link']}{post_id}"
            send_announcement(source["logo"], source["name"], title, link)
            time.sleep(1)
    except Exception as e:
        log.error(f"❌ Error API Bitfinex: {e}")


def fetch_cryptocom_api(source):
    log.info("🔌 Cek API: Crypto.com")
    try:
        r = requests.get(source["url"], headers=HEADERS, timeout=15)
        announcements = r.json()
        log.info(f"   → {len(announcements)} artikel ditemukan")
        for a in announcements:
            aid   = a.get("id", "")
            title = a.get("title", "")
            if not aid or not is_relevant(title):
                continue
            uid = f"cryptocom_{aid}"
            if not try_mark_seen(uid):
                continue
            link = source["base_link"]
            send_announcement(source["logo"], source["name"], title, link)
            time.sleep(1)
    except Exception as e:
        log.error(f"❌ Error API Crypto.com: {e}")


def fetch_kucoin_api(source):
    log.info("🔌 Cek API: KuCoin")
    try:
        r = requests.get(source["url"], headers=HEADERS, timeout=15)
        data = r.json()
        items = data.get("data", {}).get("list", [])
        log.info(f"   → {len(items)} artikel ditemukan")
        for item in items:
            title       = item.get("title", "")
            description = item.get("description", "")
            uid   = str(item.get("id", ""))
            url   = item.get("url", f"https://www.kucoin.com/announcement/{uid}")
            combined_text = f"{title} {description}"
            if not uid or not is_relevant(combined_text):
                continue
            uid_key = f"kucoin_{uid}"
            if not try_mark_seen(uid_key):
                continue
            send_announcement(source["logo"], source["name"], title, url)
            time.sleep(1)
    except Exception as e:
        log.error(f"❌ Error API KuCoin: {e}")


def fetch_scrape(source):
    log.info(f"🕷️  Scrape: {source['name']}")
    try:
        r = requests.get(source["url"], headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        links = soup.find_all("a", href=True)
        log.info(f"   → total <a>={len(links)}")

        seen_uids = set()
        matched = 0
        for a in links:
            href  = a["href"]
            title = a.get_text(separator=" ", strip=True)
            if len(title) < 15 or len(title) > 200:
                continue
            if not is_relevant(title):
                continue
            if href.startswith("/"):
                base = "/".join(source["url"].split("/")[:3])
                href = base + href
            elif not href.startswith("http"):
                continue
            uid = normalize_uid(href)
            if uid in seen_uids:
                continue
            seen_uids.add(uid)
            if not try_mark_seen(uid):
                continue
            matched += 1
            send_announcement(source["logo"], source["name"], title, href)
            time.sleep(1)
        log.info(f"   → {matched} artikel baru cocok keyword & terkirim")
    except Exception as e:
        log.error(f"❌ Error scrape {source['name']}: {e}")


def fetch_upbit_api(source):
    log.info("🔌 Cek API: Upbit")
    try:
        headers = {
            **HEADERS,
            "Referer": "https://www.upbit.com/",
            "Accept": "application/json",
        }
        r = requests.get(
            source["url"],
            params={"os": "web", "page": 1, "per_page": 20, "category": "all"},
            headers=headers, timeout=15
        )
        data = r.json()
        if not data.get("success"):
            log.error(f"❌ Upbit API success=false: {data}")
            return

        notices = data.get("data", {}).get("notices", [])
        log.info(f"   → {len(notices)} artikel ditemukan")

        for n in notices:
            title = n.get("title", "")
            nid   = n.get("id", "")
            if not nid:
                continue

            body_text = (
                n.get("content")
                or n.get("body")
                or n.get("description")
                or ""
            )
            combined_text = f"{title} {body_text}"

            if not is_relevant_upbit(combined_text):
                continue

            uid = f"upbit_{nid}"
            if not try_mark_seen(uid):
                continue

            link = f"{source['base_link']}{nid}"
            send_announcement(source["logo"], source["name"], title, link, translate_title=True)
            time.sleep(1)
    except Exception as e:
        log.error(f"❌ Error API Upbit: {e}")


def fetch_upbit_notice_body(notice_id: str) -> str:
    try:
        url = f"https://www.upbit.com/service_center/notice?id={notice_id}&view=share"
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        body = soup.get_text(separator=" ", strip=True)
        return body
    except Exception as e:
        log.error(f"⚠️ Gagal fetch detail notice Upbit {notice_id}: {e}")
        return ""


def fetch_bitget_scrape(source):
    log.info(f"🕷️  Scrape: Bitget")
    try:
        r = requests.get(source["url"], headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        seen_uids = set()
        links = soup.find_all("a", href=True)
        log.info(f"   → total <a>={len(links)}")

        for a in links:
            href = a["href"]
            if "/support/articles/" not in href:
                continue

            title = a.get_text(strip=True)
            if len(title) < 10 or not is_relevant(title):
                continue

            if href.startswith("/"):
                href = source["base_link"] + href

            uid = f"bitget_{href.rstrip('/').split('/')[-1]}"
            if uid in seen_uids:
                continue

            seen_uids.add(uid)
            if not try_mark_seen(uid):
                continue
            send_announcement(source["logo"], source["name"], title, href)
            time.sleep(1)
    except Exception as e:
        log.error(f"❌ Error scrape Bitget: {e}")

# ─── MAIN JOB ──────────────────────────────────────────────────────────────────
def check_all():
    log.info("🔄 Mulai pengecekan semua CEX...")
    for source in SOURCES:
        t = source["type"]
        if t == "binance_api":
            fetch_binance_api(source)
        elif t == "rss":
            fetch_rss(source)
        elif t == "gate_scrape":
            fetch_gate_scrape(source)
        elif source["type"] == "cryptocom_api":
            fetch_cryptocom_api(source)
        elif t == "kucoin_api":
            fetch_kucoin_api(source)
        elif t == "bitget_scrape":
            fetch_bitget_scrape(source)
        elif t == "bitfinex_api":
            fetch_bitfinex_api(source)
        elif t == "scrape":
            fetch_scrape(source)
        elif t == "upbit_api":
            fetch_upbit_api(source)
    log.info("✅ Selesai pengecekan.")

# ─── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    log.info("🚀 Bot dimulai!")

    if not is_baseline_done():
        log.info("📋 Merekam pengumuman yang sudah ada (tanpa kirim)...")
        check_all()
        set_baseline_done()
        log.info("✅ Baseline selesai, mulai monitoring normal.")
    else:
        log.info("ℹ️ Baseline sudah pernah dijalankan sebelumnya, langsung mode normal.")

    send_telegram("🤖 <b>Crypto CEX Alarm Bot aktif!</b> 🚀")

    # Listener command /test jalan di background thread, terpisah dari scheduler
    threading.Thread(target=poll_telegram_commands, daemon=True).start()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(check_all, "interval", minutes=CHECK_EVERY, max_instances=1, coalesce=True)
    scheduler.start()
