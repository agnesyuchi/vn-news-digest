#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py — 越南/寮國新聞每日摘要產生器

流程：
  1. 從各新聞來源抓取「標題 + 連結 + 發布時間（若有）」
     - 一般網站：requests（或 curl_cffi 偽裝瀏覽器指紋 / Playwright 實際渲染）+ BeautifulSoup
     - RSS 來源：feedparser
     - Google News 站內搜尋：feedparser，並保留原始出處網站名稱
  2. 篩選出落在「越南時間前一日 07:00:00 ~ 當日 06:59:59」區間內的新聞
  3. 先做網址去重，再用 Gemini 做「語意去重」（同一事件不同來源的重複報導只保留一則）
  4. 呼叫 Gemini API，批次進行「翻譯為繁體中文標題」+「分類（01政治/02經濟/03其他/discard）」
  5. 將結果寫入 data/YYYY-MM-DD.json，並更新 data/index.json（可用日期清單）

執行方式（本機測試）：
  export GEMINI_API_KEY="你的API金鑰"
  python main.py

在 GitHub Actions 中，GEMINI_API_KEY 會由 Repo Secrets 注入為環境變數。
"""

import os
import sys
import json
import time
import re
from datetime import datetime, timedelta, date
from urllib.parse import urljoin

import pytz
import requests
import feedparser
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 基本設定
# ---------------------------------------------------------------------------

VN_TZ = pytz.timezone("Asia/Ho_Chi_Minh")  # UTC+7，寮國與越南同時區
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
GEMINI_MODEL = "gemini-3.5-flash-lite"
# 選用 flash-lite 而非一般 flash：這是 Google 官方定位為「高流量、低延遲的翻譯／
# 分類／資料萃取」任務的輕量模型，完全符合本專案的使用情境，且免費額度遠高於
# 一般 flash 模型（一般 flash 近期免費額度被大幅緊縮，部分帳戶每日僅 20 次請求）。
# 註：Gemini 模型名稱會隨時間淘汰／更新（例如 gemini-1.5-flash 已於 2025 年停用）。
# 若之後執行時又出現「404 NOT_FOUND ... is not found for API version」，
# 表示這裡設定的模型名稱已被 Google 淘汰，請至 https://ai.google.dev/gemini-api/docs/models
# 查詢目前可用的最新模型名稱並更新此變數。

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.google.com/",
}

# 關鍵字（用於 Google News RSS 搜尋）
KEYWORDS = ["越南", "寮國"]

# ---------------------------------------------------------------------------
# 資料來源設定
# ---------------------------------------------------------------------------
# type = "rss"  : 直接用 feedparser 解析
# type = "html" : 用「curl_cffi 偽裝瀏覽器指紋 → Playwright 實際渲染」兩階段方式取得頁面，
#                 再用 BeautifulSoup 解析。
#                 item_selector / title_selector / link_selector 為 CSS selector，
#                 實際 HTML 結構會隨網站改版而變動，部署前請打開瀏覽器「檢查元素」確認。

SOURCES = [
    {
        "name": "越南投資 (yuenan.com)",
        "type": "html",
        "list_url": "https://yuenan.com/news/",
        # 此來源改用專屬解析邏輯（見 parse_yuenan_listing），
        # 因為 item-title 與 item-meta 是「兄弟節點」而非巢狀結構，
        # 不適合套用下方通用的 item_selector/title_selector 模式。
        "parser": "yuenan",
    },
    {
        "name": "VietnamPlus 中文網",
        "type": "rss",
        # VietnamPlus 多語版通常提供 RSS，實際路徑請以官網公告為準，
        # 若中文版無獨立 RSS，可改用其英文/越南文版 RSS 後仍以標題原文送入 Gemini 翻譯。
        "feed_url": "https://zh.vietnamplus.vn/rss/home.rss",
    },
    # 中央通訊社 (CNA) 官方站內搜尋頁為前端 JavaScript 動態載入結果，
    # 不適合直接爬取（搜尋網址也常隨改版變動），改用下方「Google News 站內搜尋」取得。
]

# Google News RSS：依關鍵字動態組出搜尋 RSS 網址
# 除了關鍵字全站搜尋，也加入「site: 限定網域」查詢，作為 yuenan.com、CNA、
# VietnamPlus 等來源在直接爬蟲失敗時的可靠替代管道。
SITE_RESTRICTED_SOURCES = [
    ("yuenan.com", "越南投資"),
    ("cna.com.tw", "中央通訊社 CNA"),
    ("zh.vietnamplus.vn", "VietnamPlus 中文網"),
]


def google_news_rss_sources():
    """建立 Google News RSS 來源清單。
    這些來源的每一則新聞，其「出處」會在 fetch_rss() 中依 RSS 內容
    動態解析為實際發布媒體名稱，並統一標註為「XXX（Google News 轉載）」。
    """
    sources = []
    for kw in KEYWORDS:
        sources.append({
            "name": f"Google News - {kw}",
            "type": "rss",
            "via_google_news": True,
            "feed_url": (
                f"https://news.google.com/rss/search?q={kw}"
                f"&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
            ),
        })
        for site, label in SITE_RESTRICTED_SOURCES:
            sources.append({
                "name": f"{label} (Google News) - {kw}",
                "type": "rss",
                "via_google_news": True,
                "feed_url": (
                    f"https://news.google.com/rss/search?q=site:{site}+{kw}"
                    f"&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
                ),
            })
    return sources


# ---------------------------------------------------------------------------
# 抓取函式：RSS
# ---------------------------------------------------------------------------

def _extract_google_news_origin(entry, fallback_title):
    """從 Google News RSS 的單一 entry 中，盡量解析出實際發布媒體名稱。
    優先讀取 RSS <source> 標籤；若無，退而求其次從標題常見的
    「標題文字 - 媒體名稱」格式中取出結尾的媒體名稱。
    回傳 (origin_name_or_None, title_without_suffix)
    """
    origin = None
    src_field = getattr(entry, "source", None)
    if src_field:
        origin = getattr(src_field, "title", None) or getattr(src_field, "value", None)
        if origin:
            origin = origin.strip()

    cleaned_title = fallback_title
    if " - " in fallback_title:
        head, _, tail = fallback_title.rpartition(" - ")
        tail = tail.strip()
        if not origin and tail:
            origin = tail
        # 若結尾片段與解析出的媒體名稱相符，視為標題自帶的來源標記，予以移除
        if origin and tail == origin:
            cleaned_title = head.strip()

    return origin, cleaned_title


def fetch_rss(source):
    """解析 RSS/Atom feed，回傳 [{title, link, published, source}, ...]"""
    items = []
    try:
        feed = feedparser.parse(source["feed_url"])
        for entry in feed.entries:
            raw_title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "").strip()
            published_dt = None
            for time_field in ("published_parsed", "updated_parsed"):
                tstruct = getattr(entry, time_field, None)
                if tstruct:
                    published_dt = datetime(*tstruct[:6], tzinfo=pytz.UTC).astimezone(VN_TZ)
                    break

            source_name = source["name"]
            title = raw_title
            if source.get("via_google_news"):
                origin, cleaned_title = _extract_google_news_origin(entry, raw_title)
                title = cleaned_title
                if origin:
                    source_name = f"{origin}（Google News 轉載）"
                else:
                    source_name = "未知媒體（Google News 轉載）"

            if title and link:
                items.append({
                    "title": title,
                    "link": link,
                    "published": published_dt,
                    "source": source_name,
                })
    except Exception as e:
        print(f"[WARN] RSS 抓取失敗 ({source['name']}): {e}", file=sys.stderr)
    return items


# ---------------------------------------------------------------------------
# 抓取函式：HTML（含防爬蟲繞道機制）
# ---------------------------------------------------------------------------

def _fetch_page_html(url):
    """依序嘗試三種方式取得頁面 HTML，回傳 (html_or_None, method_used)：
      1. requests：最快，適用於沒有防爬蟲機制的網站
      2. curl_cffi：偽裝瀏覽器 TLS/JA3 指紋，可繞過多數 Cloudflare 等
         「基本防護」（僅檢查請求指紋、不需執行 JavaScript 的防護）
      3. Playwright：啟動真實無頭瀏覽器渲染頁面，可通過需要執行 JavaScript
         的進階防護（例如 JS 驗證挑戰），但速度較慢、資源消耗較大
    """
    # 方式一：一般 requests
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            return resp.text, "requests"
        print(f"[INFO] requests 取得 {url} 狀態碼 {resp.status_code}，改嘗試 curl_cffi", file=sys.stderr)
    except Exception as e:
        print(f"[INFO] requests 抓取 {url} 失敗（{e}），改嘗試 curl_cffi", file=sys.stderr)

    # 方式二：curl_cffi 偽裝瀏覽器指紋
    try:
        from curl_cffi import requests as curl_requests
        resp = curl_requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=20)
        if resp.status_code == 200:
            return resp.text, "curl_cffi"
        print(f"[INFO] curl_cffi 取得 {url} 狀態碼 {resp.status_code}，改嘗試 Playwright", file=sys.stderr)
    except Exception as e:
        print(f"[INFO] curl_cffi 抓取 {url} 失敗（{e}），改嘗試 Playwright", file=sys.stderr)

    # 方式三：Playwright 實際渲染頁面
    # 注意：wait_until 使用 "domcontentloaded" 而非 "networkidle"。
    # networkidle 要求頁面「完全沒有背景網路活動」才視為載入完成，
    # 但真實網站常有廣告/追蹤碼持續發送零星請求，導致這個條件幾乎等不到，
    # 每次都會硬等滿逾時時間（非常慢）。我們需要的內容是伺服器端渲染好的
    # 靜態 HTML，domcontentloaded（主要內容載入完成）已經足夠。
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, timeout=15000, wait_until="domcontentloaded")
            html = page.content()
            browser.close()
            return html, "playwright"
    except Exception as e:
        print(f"[WARN] Playwright 抓取 {url} 也失敗（{e}），此來源本次略過", file=sys.stderr)

    return None, None


def _parse_relative_chinese_time(text, reference_dt):
    """解析常見的中文相對時間文字（例如「2小时前」「3天前」「刚刚」「昨天」），
    找不到對應格式時，退回一般日期格式解析（try_parse_time）。
    """
    text = text.strip()
    if text in ("刚刚", "剛剛"):
        return reference_dt

    m = re.match(r"(\d+)\s*分钟前", text) or re.match(r"(\d+)\s*分鐘前", text)
    if m:
        return reference_dt - timedelta(minutes=int(m.group(1)))

    m = re.match(r"(\d+)\s*小时前", text) or re.match(r"(\d+)\s*小時前", text)
    if m:
        return reference_dt - timedelta(hours=int(m.group(1)))

    m = re.match(r"(\d+)\s*天前", text)
    if m:
        return reference_dt - timedelta(days=int(m.group(1)))

    if text in ("昨天",):
        return reference_dt - timedelta(days=1)

    return try_parse_time(text)


def _is_within_roughly_24h(text):
    """【步驟一：粗篩】依列表頁的相對時間文字（例如「2小时前」），
    粗略判斷這則新聞是否可能落在「24 小時內」，決定要不要進一步花成本
    造訪文章內頁取得精確時間。判斷從寬：格式無法辨識時一律視為候選
    （寧可多查一次，也不要漏掉真正在區間內的新聞）。
    """
    text = text.strip()
    if text in ("刚刚", "剛剛", "昨天"):
        return True

    m = re.match(r"(\d+)\s*分钟前", text) or re.match(r"(\d+)\s*分鐘前", text)
    if m:
        return True

    m = re.match(r"(\d+)\s*小时前", text) or re.match(r"(\d+)\s*小時前", text)
    if m:
        return int(m.group(1)) <= 24

    m = re.match(r"(\d+)\s*天前", text)
    if m:
        # 「1天前」可能落在區間邊界附近，保留為候選；2天以上明顯超出範圍，直接排除。
        return int(m.group(1)) <= 1

    return True  # 格式未知，保守納入候選


def parse_yuenan_listing(html, base_url, source_name):
    """越南投資 (yuenan.com) 專屬解析邏輯。
    實際頁面結構（依使用者提供的頁面原始碼確認）：
        <div class="item-content">
            <h3 class="item-title"><a href="...">標題</a></h3>
            <div class="item-excerpt">...</div>
        </div>
        <div class="item-meta">
            ...
            <span class="item-meta-li date">2小时前</span>
            ...
        </div>
    item-content 與 item-meta 是「兄弟節點」，因此改用 find_next_sibling 取得對應的
    item-meta，而非依賴不確定的外層容器 class 名稱。

    時間判斷採兩步驟：
    【步驟一】先用這裡解析出的相對時間文字（如「2小时前」）做粗略判斷與估算時間，
             同時標記是否為「24 小時內候選」（_within_24h_candidate）。
    【步驟二】實際精確時間交由 enrich_yuenan_published_times() 進一步造訪文章內頁、
             讀取 entry-date published 補齊——只針對步驟一判定為候選的項目執行，
             避免對明顯超出範圍的舊文章浪費抓取次數。
    """
    soup = BeautifulSoup(html, "html.parser")
    reference_dt = datetime.now(VN_TZ)
    items = []

    for h3 in soup.select("h3.item-title"):
        a = h3.select_one("a")
        if not a:
            continue
        title = a.get_text(strip=True)
        link = a.get("href", "").strip()
        if link and not link.startswith("http"):
            link = urljoin(base_url, link)
        if not title or not link:
            continue

        published_dt = None
        within_24h_candidate = True  # 找不到時間標籤時，保守視為候選
        item_content = h3.find_parent("div", class_="item-content")
        if item_content:
            item_meta = item_content.find_next_sibling("div", class_="item-meta")
            if item_meta:
                date_el = item_meta.select_one(".item-meta-li.date") or item_meta.select_one(".date")
                if date_el:
                    date_text = date_el.get_text(strip=True)
                    published_dt = _parse_relative_chinese_time(date_text, reference_dt)
                    within_24h_candidate = _is_within_roughly_24h(date_text)

        items.append({
            "title": title,
            "link": link,
            "published": published_dt,
            "source": source_name,
            "_within_24h_candidate": within_24h_candidate,
        })

    return items, soup


def parse_generic_listing(soup, source):
    """通用 HTML 解析邏輯（適用於未指定專屬 parser 的來源）。
    item_selector / title_selector / link_selector 為 CSS selector，
    實際 HTML 結構會隨網站改版而變動，部署前請打開瀏覽器「檢查元素」確認。
    """
    items = []
    nodes = soup.select(source["item_selector"])
    for node in nodes:
        title_el = node.select_one(source["title_selector"])
        link_el = node.select_one(source["link_selector"])
        if not title_el or not link_el:
            continue
        title = title_el.get_text(strip=True)
        link = link_el.get("href", "").strip()
        if link and not link.startswith("http"):
            link = urljoin(source["list_url"], link)

        published_dt = None
        if source.get("time_selector"):
            time_el = node.select_one(source["time_selector"])
            if time_el:
                published_dt = try_parse_time(time_el.get_text(strip=True))

        if title and link:
            items.append({
                "title": title,
                "link": link,
                "published": published_dt,
                "source": source["name"],
            })
    return items


def fetch_html(source):
    """取得新聞列表頁 HTML 並解析。
    無法取得精確發布時間時，published 設為 None，
    後續會改用容錯邏輯保留該筆讓 Gemini 依內容判斷。
    """
    items = []
    html_content, method_used = _fetch_page_html(source["list_url"])
    if html_content is None:
        return items

    print(f"[INFO] {source['name']} 以「{method_used}」方式成功取得頁面", file=sys.stderr)

    try:
        if source.get("parser") == "yuenan":
            items, soup = parse_yuenan_listing(html_content, source["list_url"], source["name"])
        else:
            soup = BeautifulSoup(html_content, "html.parser")
            items = parse_generic_listing(soup, source)
    except Exception as e:
        print(f"[WARN] HTML 解析失敗 ({source['name']}): {e}", file=sys.stderr)
        return items

    if len(items) == 0:
        # 頁面成功取得，但解析不到任何項目，代表網頁結構與目前解析邏輯不符。
        # 印出頁面片段方便直接從 log 除錯，不需要另外用瀏覽器「檢查元素」。
        print(
            f"[WARN] {source['name']} 頁面成功取得，但解析到 0 筆，"
            f"可能是網頁結構與目前的解析邏輯不符。",
            file=sys.stderr,
        )
        _debug_dump_html_snippet(source["name"], soup)

    return items


def _debug_dump_html_snippet(source_name, soup):
    """印出頁面中「看起來像新聞連結」的候選元素，協助判斷正確的 CSS selector。"""
    print(f"[DEBUG] ===== {source_name} 頁面結構除錯資訊開始 =====", file=sys.stderr)

    # 印出所有帶連結文字看起來像新聞標題的 <a> 標籤（文字長度 > 8 個字），
    # 並附上該 <a> 標籤往上兩層的父層標籤名稱與 class，方便推斷 item_selector。
    candidate_links = [a for a in soup.find_all("a") if a.get_text(strip=True) and len(a.get_text(strip=True)) > 8]
    print(f"[DEBUG] 頁面中共找到 {len(soup.find_all('a'))} 個 <a> 標籤，"
          f"其中文字長度 > 8 的有 {len(candidate_links)} 個，列出前 15 個：", file=sys.stderr)

    for a in candidate_links[:15]:
        parent = a.parent
        grandparent = parent.parent if parent else None
        parent_desc = f"<{parent.name} class='{' '.join(parent.get('class', []))}'>" if parent else "無"
        grandparent_desc = f"<{grandparent.name} class='{' '.join(grandparent.get('class', []))}'>" if grandparent else "無"
        href = a.get("href", "")
        text = a.get_text(strip=True)[:40]
        print(f"[DEBUG]   文字: 「{text}」 | href: {href[:60]} | 父層: {parent_desc} | 祖父層: {grandparent_desc}", file=sys.stderr)

    print(f"[DEBUG] ===== {source_name} 頁面結構除錯資訊結束 =====", file=sys.stderr)


def try_parse_time(text):
    """嘗試解析常見中文/數字時間格式，失敗回傳 None。"""
    from dateutil import parser as dateparser
    try:
        dt = dateparser.parse(text, fuzzy=True)
        if dt:
            if dt.tzinfo is None:
                dt = VN_TZ.localize(dt)
            return dt.astimezone(VN_TZ)
    except Exception:
        pass
    return None


def _apply_yuenan_published_time(item, html_content):
    """從文章內頁 HTML 中解析精確發布時間，成功則寫入 item['published']。"""
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        time_el = soup.select_one("time.entry-date.published") or soup.select_one("time.published")
        if time_el:
            dt_str = time_el.get("datetime")
            if dt_str:
                dt = datetime.fromisoformat(dt_str)
                item["published"] = dt.astimezone(VN_TZ)
                return True
    except Exception as e:
        print(f"[WARN] 解析文章內頁時間失敗 ({item['link']}): {e}", file=sys.stderr)
    return False


def enrich_yuenan_published_times(items):
    """【步驟二：精確查證】針對「步驟一（列表頁相對時間粗篩）」判定為
    24 小時內候選的 yuenan.com 文章，進一步造訪該篇文章的內頁，
    讀取標準格式的發布時間標籤：
        <time class="entry-date published" datetime="2026-08-14T17:08:03+07:00">
    這個時間戳精確到秒且含時區，用來取代列表頁「2小时前」這類相對時間估算值。

    明顯超出 24 小時範圍的文章（例如「2天前」以上）在步驟一就已標記為非候選，
    這裡不會浪費抓取次數在這些文章上，只需保留其估算時間供時間篩選階段判斷即可。

    此函式會直接修改傳入 items 中符合條件項目的 "published" 欄位。

    效能考量：先用較快的 requests / curl_cffi 嘗試每篇文章；只有這兩種方式
    都失敗的文章，才集中改用「同一個」Playwright 瀏覽器實例依序處理
    （而非每篇文章各自啟動一次瀏覽器），因為啟動瀏覽器的成本遠高於單純換頁。
    """
    targets = [
        it for it in items
        if "yuenan.com" in it["link"] and it.get("_within_24h_candidate", True)
    ]
    skipped = sum(1 for it in items if "yuenan.com" in it["link"]) - len(targets)
    if skipped:
        print(f"[INFO] {skipped} 篇 yuenan.com 文章依列表頁時間判斷明顯超出 24 小時範圍，略過精確查證", file=sys.stderr)
    if not targets:
        return

    print(f"[INFO] 針對 {len(targets)} 筆 24 小時內候選文章造訪內頁取得精確發布時間", file=sys.stderr)

    success_count = 0
    needs_playwright = []

    for it in targets:
        html_content = None
        try:
            resp = requests.get(it["link"], headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                html_content = resp.text
        except Exception:
            pass

        if html_content is None:
            try:
                from curl_cffi import requests as curl_requests
                resp = curl_requests.get(it["link"], headers=HEADERS, impersonate="chrome124", timeout=20)
                if resp.status_code == 200:
                    html_content = resp.text
            except Exception:
                pass

        if html_content is not None:
            if _apply_yuenan_published_time(it, html_content):
                success_count += 1
        else:
            needs_playwright.append(it)
        time.sleep(0.5)

    if needs_playwright:
        print(
            f"[INFO] {len(needs_playwright)} 篇文章需改用 Playwright（將共用同一瀏覽器實例依序處理）",
            file=sys.stderr,
        )
        consecutive_failures = 0
        max_consecutive_failures = 5  # 連續失敗達此次數，視為該網站本次不可用，提前放棄剩餘項目
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent=HEADERS["User-Agent"])
                for it in needs_playwright:
                    if consecutive_failures >= max_consecutive_failures:
                        print(
                            f"[WARN] 已連續 {max_consecutive_failures} 篇 Playwright 抓取失敗，"
                            f"判斷網站本次暫時無法存取，提前放棄剩餘 "
                            f"{len(needs_playwright) - needs_playwright.index(it)} 篇（避免無謂等待）",
                            file=sys.stderr,
                        )
                        break
                    try:
                        # 同 _fetch_page_html：改用 domcontentloaded 避免 networkidle
                        # 因背景雜訊請求而幾乎必定逾時的問題，並縮短單頁逾時時間。
                        page.goto(it["link"], timeout=15000, wait_until="domcontentloaded")
                        if _apply_yuenan_published_time(it, page.content()):
                            success_count += 1
                            consecutive_failures = 0
                        else:
                            consecutive_failures += 1
                    except Exception as e:
                        print(f"[WARN] Playwright 抓取文章內頁失敗 ({it['link']}): {e}", file=sys.stderr)
                        consecutive_failures += 1
                browser.close()
        except Exception as e:
            print(f"[WARN] Playwright 初始化失敗，剩餘文章無法補齊精確時間: {e}", file=sys.stderr)

    print(f"[INFO] 成功補齊 {success_count}/{len(targets)} 筆 yuenan.com 精確發布時間", file=sys.stderr)


def collect_all_items():
    all_items = []
    source_counts = []
    for source in SOURCES + google_news_rss_sources():
        if source["type"] == "rss":
            fetched = fetch_rss(source)
        else:
            fetched = fetch_html(source)
        source_counts.append((source["name"], len(fetched)))
        all_items.extend(fetched)
        time.sleep(1)  # 禮貌性間隔，避免對來源網站造成負擔

    print("[INFO] ===== 各來源抓取筆數 =====", file=sys.stderr)
    for name, count in source_counts:
        print(f"[INFO]   {name}: {count} 筆", file=sys.stderr)
    print("[INFO] ===========================", file=sys.stderr)

    return all_items


# ---------------------------------------------------------------------------
# 時間篩選：越南時間前一日 07:00:00 ~ 當日 06:59:59
# ---------------------------------------------------------------------------

def get_time_window(now_vn):
    end = now_vn.replace(hour=6, minute=59, second=59, microsecond=0)
    if now_vn.hour < 7:
        start = (end - timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    else:
        start = now_vn.replace(hour=7, minute=0, second=0, microsecond=0) - timedelta(days=1)
        end = now_vn.replace(hour=6, minute=59, second=59, microsecond=0)
    return start, end


def filter_by_time(items, start, end):
    filtered = []
    matched_by_time = 0
    kept_without_time = 0
    for item in items:
        pub = item.get("published")
        if pub is None:
            # 抓不到精確發布時間的來源，先保留讓 Gemini 依內容判斷是否為近期新聞。
            filtered.append(item)
            kept_without_time += 1
            continue
        if start <= pub <= end:
            filtered.append(item)
            matched_by_time += 1
    print(
        f"[INFO] 時間篩選明細：{matched_by_time} 筆時間落在區間內、"
        f"{kept_without_time} 筆因缺少精確時間被容錯保留",
        file=sys.stderr,
    )
    return filtered


# ---------------------------------------------------------------------------
# 去重（第一階段：網址去重）
# ---------------------------------------------------------------------------

def dedupe_by_url(items):
    seen = set()
    result = []
    for item in items:
        key = item["link"].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# 去重（第二階段：Gemini 語意去重）
# 多個來源（原始網站 + Google News 轉載 + 不同媒體報導同一事件）常出現
# 標題文字不同、但描述同一則新聞的情況，用語意判斷合併只保留一則。
# ---------------------------------------------------------------------------

def _clean_json_block(text):
    return re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()


def _call_gemini_with_retry(prompt, api_key, max_retries=3):
    """呼叫 Gemini API，遇到暫時性錯誤（429 速率限制、503 服務忙碌）時，
    依錯誤訊息中建議的等待秒數（或預設遞增秒數）自動重試，
    降低因短暫流量尖峰而整批資料流失的機率。
    注意：若是「當日額度已用完」（RESOURCE_EXHAUSTED 且無法在短時間恢復），
    重試無法解決問題，仍會在重試次數用盡後回傳 None。
    """
    from google import genai

    client = genai.Client(api_key=api_key)

    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
            return response.text
        except Exception as e:
            err_text = str(e)
            is_rate_limit = "429" in err_text or "RESOURCE_EXHAUSTED" in err_text
            is_transient = "503" in err_text or "UNAVAILABLE" in err_text

            if attempt == max_retries or not (is_rate_limit or is_transient):
                print(f"[ERROR] Gemini API 呼叫失敗（第 {attempt} 次嘗試）: {e}", file=sys.stderr)
                return None

            # 嘗試從錯誤訊息中解析 Google 建議的等待秒數（retryDelay），找不到則用遞增預設值
            wait_seconds = 10 * attempt
            m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+)", err_text)
            if m:
                wait_seconds = int(m.group(1)) + 2  # 額外加 2 秒緩衝

            print(
                f"[WARN] Gemini API 暫時性錯誤（第 {attempt}/{max_retries} 次嘗試），"
                f"{wait_seconds} 秒後重試: {e}",
                file=sys.stderr,
            )
            time.sleep(wait_seconds)

    return None


# ---------------------------------------------------------------------------
# Gemini API：去重 + 翻譯 + 分類（合併為單一次呼叫，降低每日請求次數）
# ---------------------------------------------------------------------------
#
# 先前版本將「語意去重」與「翻譯＋分類」拆成兩階段、且分類階段又依 15 筆一批
# 分批呼叫，單次執行合計可能耗用 8 次以上的 API 請求。Gemini 的上下文視窗
# 大到可以一次容納數百則新聞標題，因此改為「單一 prompt」同時完成去重、
# 翻譯、分類三件事，也避免分類規則文字被重複傳送多次而浪費 token。
#
# DEDUPE_CLASSIFY_BATCH_SIZE 的設定是「低呼叫次數」與「判斷精準度」的折衷：
# 批次設太大（例如一次塞 300 筆），單一 prompt 內要同時比對的項目過多，
# 模型在語意去重的兩兩比對、以及分類判斷的細緻度上都可能因資訊量過大而下降，
# 且大型 JSON 陣列的輸出格式也較容易出錯。批次設太小則會回到原本 8 次以上
# 呼叫的問題。實務取中間值：以一般每日 100~150 筆的規模估算，
# 50 筆一批約可將呼叫次數壓在 3 次以內，同時讓模型在合理的比對範圍內
# 維持判斷品質。
DEDUPE_CLASSIFY_BATCH_SIZE = 50

CATEGORY_RULES = """
判斷角度：請以「台灣、越南、寮國外交部門」會關注的事項為標準，而非單純規模大小。
凡具有「外交意涵、經貿政策意涵、或領事僑務意涵」的新聞，即使不是六位越南高層本人的活動、
不是巨額投資案，也應納入對應類別。單純的地方生活瑣事、與外交/經貿/領事事務無關的個案，
才歸類為 discard。

【重要】下方每個類別列出的項目是「常見範例」，用來說明判斷的方向與尺度，並非窮舉清單。
國際局勢與新聞型態會持續演變，可能出現範例中未列出、但同樣具有外交/經貿/領事意涵的
新型態事件（例如新的區域安全議題、新的經濟制裁工具、新的跨國犯罪型態、新的外交場域等）。
請理解每個類別「背後的判斷原則」，並將原則類推套用到範例未涵蓋的情況，
而不是機械式比對「是否完全符合列出的某一條」。判斷標準是：
「一位負責越南/寮國業務的外交部官員，是否會想看到這則新聞」。

【01 政治】判斷原則：外交互動、政治情勢、區域安全、對台立場相關。常見範例包括但不限於：
- 越南/寮國黨政高層（不限六位點名對象，任何部會、省市層級官員亦可）之外交互動：
  出訪、接待外賓、簽署協議、聯合聲明、國際會議發言、建交/斷交動態。
- 具全國性意涵的高層人事異動、貪腐調查、政策宣示。
- 南海爭端、邊界爭議、區域安全動態（含美中在越南/寮國周邊之角力）。
- 對台灣的官方或非官方立場表態（涉台言論、一中政策相關表態、雙邊關係動向）。
- 涉及人權、宗教自由、新聞自由之國際報告或事件。
- 軍事外交：軍演、軍購、國防合作協議、軍艦/軍機互訪。
- 東協（ASEAN）相關外交活動、輪值主席事務、區域峰會。
- 外國專家學者對越南/寮國政治情勢之評論。
- 中國（或其他大國）駐越南/寮國大使館之正式活動。

【02 經濟】判斷原則：經貿政策、供應鏈地緣政治、對外資影響相關。常見範例包括但不限於：
- 自由貿易協定談判、簽署或生效動態（CPTPP、EVFTA、RCEP 等）。
- 貿易制裁、關稅爭端、反傾銷調查（尤其涉及台灣或影響台商供應鏈者）。
- 供應鏈轉移動態：台商或國際廠商因應地緣政治、去風險化而調整越南/寮國產能佈局。
- 半導體、關鍵礦產、能源（LNG、再生能源、核電）等具地緣政治意義之產業政策。
- 大型基礎建設（港口、鐵路、公路、電力）之外資參與或地緣政治意涵。
- 影響外商投資環境的重大政策或法規變動（不限具名企業個案）。
- 具政策意涵的總體經濟數據發布或官方經濟宣示。
- 外國專家學者對越南/寮國經濟情勢之評論。

【03 其他】判斷原則：領事保護、僑務、跨境社會安全相關。常見範例包括但不限於：
- 【最優先關注】東南亞跨國詐騙集團、人口販運相關新聞，尤其涉及台灣人受害、救援、
  遣返，或發生於寮國、緬甸等邊境地區的詐騙園區議題——即使是個案也應納入，
  這是當前高度關注的領事保護議題。
- 涉及台灣人的重大事件：不限犯罪案件，亦包含意外事故、需要領事協助或撤僑之情況、
  重大旅遊警示。
- 天災、公共衛生危機（傳染病爆發等）具跨境影響、或當地台灣人／外國人聚居地受影響者。
- 影響外國人整體權益的重大法規、簽證、居留政策變動。
- 越南勞工／新住民赴台相關政策動向或社會矚目事件（含官員貪腐情事）。
- 越南、寮國主辦之國際級文化、藝術、體育競賽或活動，或越南、寮國人在國際上取得之
  重大成就。
- 湄公河流域跨境環境議題（水壩、乾旱等具跨境外交意涵者）。
- 寮國全國矚目之重大事件。

【排除範例（歸類為 discard）】：
純地方生活瑣事、與外交／經貿／領事事務無明顯關聯的個案，例如：動物闖入店家、
單一機構的例行業務統計、地方性非國際級的紀念活動、未涉台灣人或外國人的一般社會案件、
未達跨境或政策層級的單一意外事故。
"""


def build_combined_prompt(items):
    numbered = "\n".join(
        f'{i+1}. 「{it["title"]}」（來源：{it["source"]}）'
        for i, it in enumerate(items)
    )
    return f"""你是一位外交部新聞資訊編輯，請對以下新聞清單依序完成三項任務。
清單可能有數十則之多，請逐一仔細判斷每一則，不要因為清單較長就簡化或跳過判斷邏輯——
去重比對、翻譯品質、分類準確度都必須維持一致的嚴謹程度，不因項目數量而打折扣。

【任務一：去重】找出所有屬於「同一新聞事件」的重複報導（例如同一則消息被不同媒體轉載、
或用不同標題描述同一件事），每組只保留一則代表性項目（優先保留非「Google News 轉載」
的原始出處；若整組都是或都不是轉載，保留編號最小的一則）。不確定是否重複時，
不要視為重複（寧可漏判，不要誤判）。

【任務二：翻譯】針對「保留下來」的每一則新聞，將標題翻譯／轉寫為繁體中文，
語氣維持新聞標題的簡潔客觀風格。

【任務三：分類】任務是從中篩選出「對台灣、越南、寮國外交部門具有參考價值」的新聞——
包括外交互動、經貿政策、供應鏈地緣政治、領事僑務保護等面向，而不是只挑選規模最大的頭條。
依照下列分類規則，將每則「保留下來」的新聞歸類為 "01"（政治）、"02"（經濟）、
"03"（其他）其中之一。若屬於純地方生活瑣事、與外交／經貿／領事事務無明顯關聯，
歸類為 "discard"。

分類規則：
{CATEGORY_RULES}

新聞清單：
{numbered}

請「只」回傳一個 JSON 陣列，不要加入任何說明文字或 Markdown 語法（不要用 ```json 包裹）。
陣列只需包含「去重後保留、且分類不為 discard」的項目，格式為：
{{"index": 原始編號(數字), "title_zh": "翻譯後的繁體中文標題", "category": "01/02/03"}}
"""


def dedupe_translate_classify(items, api_key):
    """單一 Gemini 呼叫同時完成：語意去重、標題翻譯、分類。
    回傳最終應寫入摘要檔的新聞清單。
    """
    if not items:
        return []

    output = []
    for i in range(0, len(items), DEDUPE_CLASSIFY_BATCH_SIZE):
        batch = items[i:i + DEDUPE_CLASSIFY_BATCH_SIZE]
        prompt = build_combined_prompt(batch)
        raw_text = _call_gemini_with_retry(prompt, api_key)

        if raw_text is None:
            print(
                f"[ERROR] 本批 {len(batch)} 筆新聞因 Gemini API 呼叫失敗而略過"
                f"（常見原因：當日免費額度已用完，需等待額度重置或升級付費方案）。",
                file=sys.stderr,
            )
            continue

        try:
            parsed = json.loads(_clean_json_block(raw_text))
        except Exception as e:
            print(f"[ERROR] Gemini 回傳內容解析失敗: {e}", file=sys.stderr)
            continue

        for entry in parsed:
            try:
                idx = int(entry["index"]) - 1
            except (KeyError, ValueError, TypeError):
                continue
            if not (0 <= idx < len(batch)):
                continue
            category = entry.get("category", "").strip()
            if category not in ("01", "02", "03"):
                continue
            item = batch[idx]
            output.append({
                "title": entry.get("title_zh", "").strip() or item["title"],
                "link": item["link"],
                "source": item["source"],
                "category": category,
                "published": item["published"].isoformat() if item.get("published") else None,
            })

    return output


# ---------------------------------------------------------------------------
# 輸出
# ---------------------------------------------------------------------------

def write_daily_json(target_date: date, news_items, generated_at_vn):
    os.makedirs(DATA_DIR, exist_ok=True)
    filename = target_date.isoformat() + ".json"
    path = os.path.join(DATA_DIR, filename)

    grouped = {"01": [], "02": [], "03": []}
    for item in news_items:
        grouped[item["category"]].append({
            "title": item["title"],
            "link": item["link"],
            "source": item["source"],
            "published": item["published"],
        })

    payload = {
        "date": target_date.isoformat(),
        "generated_at": generated_at_vn.isoformat(),
        "categories": {
            "01": grouped["01"],
            "02": grouped["02"],
            "03": grouped["03"],
        },
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[INFO] 已寫入 {path}（政治 {len(grouped['01'])} / 經濟 {len(grouped['02'])} / 其他 {len(grouped['03'])} 則）")
    return filename


def update_index(target_date: date):
    index_path = os.path.join(DATA_DIR, "index.json")
    dates = []
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                dates = json.load(f).get("dates", [])
        except Exception:
            dates = []

    date_str = target_date.isoformat()
    if date_str not in dates:
        dates.append(date_str)
    dates = sorted(set(dates))

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump({"dates": dates, "latest": dates[-1]}, f, ensure_ascii=False, indent=2)

    print(f"[INFO] 已更新 index.json，目前共 {len(dates)} 個日期，最新日期：{dates[-1]}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[ERROR] 找不到環境變數 GEMINI_API_KEY，請先設定後再執行。", file=sys.stderr)
        sys.exit(1)

    now_vn = datetime.now(VN_TZ)
    start, end = get_time_window(now_vn)
    target_date = now_vn.date()

    print(f"[INFO] 執行時間（越南時區）: {now_vn.isoformat()}")
    print(f"[INFO] 新聞篩選區間: {start.isoformat()} ~ {end.isoformat()}")

    raw_items = collect_all_items()
    print(f"[INFO] 抓取到原始新聞筆數: {len(raw_items)}")

    enrich_yuenan_published_times(raw_items)

    time_filtered = filter_by_time(raw_items, start, end)
    print(f"[INFO] 時間篩選後筆數: {len(time_filtered)}")

    url_deduped = dedupe_by_url(time_filtered)
    print(f"[INFO] 網址去重後筆數: {len(url_deduped)}")

    if not url_deduped:
        print("[INFO] 本次無符合時間區間之新聞，仍會輸出空白摘要檔以維持前端日期可查。")

    classified = dedupe_translate_classify(url_deduped, api_key)
    print(f"[INFO] Gemini 去重＋分類後保留筆數: {len(classified)}")

    write_daily_json(target_date, classified, now_vn)
    update_index(target_date)


if __name__ == "__main__":
    main()
