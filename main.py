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
GEMINI_MODEL = "gemini-3.5-flash"
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
        "feed_url": "https://zh.vietnamplus.vn/rss/home.rss",
    },
]

SITE_RESTRICTED_SOURCES = [
    ("yuenan.com", "越南投資"),
    ("cna.com.tw", "中央通訊社 CNA"),
    ("zh.vietnamplus.vn", "VietnamPlus 中文網"),
]


def google_news_rss_sources():
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


def _extract_google_news_origin(entry, fallback_title):
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
        if origin and tail == origin:
            cleaned_title = head.strip()

    return origin, cleaned_title


def fetch_rss(source):
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


def _fetch_page_html(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            return resp.text, "requests"
        print(f"[INFO] requests 取得 {url} 狀態碼 {resp.status_code}，改嘗試 curl_cffi", file=sys.stderr)
    except Exception as e:
        print(f"[INFO] requests 抓取 {url} 失敗（{e}），改嘗試 curl_cffi", file=sys.stderr)

    try:
        from curl_cffi import requests as curl_requests
        resp = curl_requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=20)
        if resp.status_code == 200:
            return resp.text, "curl_cffi"
        print(f"[INFO] curl_cffi 取得 {url} 狀態碼 {resp.status_code}，改嘗試 Playwright", file=sys.stderr)
    except Exception as e:
        print(f"[INFO] curl_cffi 抓取 {url} 失敗（{e}），改嘗試 Playwright", file=sys.stderr)

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, timeout=30000, wait_until="networkidle")
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
        item_content = h3.find_parent("div", class_="item-content")
        if item_content:
            item_meta = item_content.find_next_sibling("div", class_="item-meta")
            if item_meta:
                date_el = item_meta.select_one(".item-meta-li.date") or item_meta.select_one(".date")
                if date_el:
                    published_dt = _parse_relative_chinese_time(date_el.get_text(strip=True), reference_dt)

        items.append({
            "title": title,
            "link": link,
            "published": published_dt,
            "source": source_name,
        })

    return items, soup


def parse_generic_listing(soup, source):
    """通用 HTML 解析邏輯（適用於未指定專屬 parser 的來源）。"""
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
        print(
            f"[WARN] {source['name']} 頁面成功取得，但解析到 0 筆，"
            f"可能是網頁結構與目前的解析邏輯不符。",
            file=sys.stderr,
        )
        _debug_dump_html_snippet(source["name"], soup)

    return items


def _debug_dump_html_snippet(source_name, soup):
    print(f"[DEBUG] ===== {source_name} 頁面結構除錯資訊開始 =====", file=sys.stderr)

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
        time.sleep(1)

    print("[INFO] ===== 各來源抓取筆數 =====", file=sys.stderr)
    for name, count in source_counts:
        print(f"[INFO]   {name}: {count} 筆", file=sys.stderr)
    print("[INFO] ===========================", file=sys.stderr)

    return all_items


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
    for item in items:
        pub = item.get("published")
        if pub is None:
            filtered.append(item)
            continue
        if start <= pub <= end:
            filtered.append(item)
    return filtered


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


def _clean_json_block(text):
    return re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()


def build_dedupe_prompt(items):
    numbered = "\n".join(
        f'{i+1}. 「{it["title"]}」（來源：{it["source"]}）'
        for i, it in enumerate(items)
    )
    return f"""以下是今天蒐集到的越南／寮國相關新聞標題清單，其中可能包含多篇「描述同一則新聞事件」
的重複報導（例如同一則消息被不同媒體轉載、或用不同標題描述同一件事）。

請找出所有屬於同一事件的重複項目，將它們的編號分組。判斷標準是「新聞事件本身是否相同」，
而非文字是否完全一樣（例如標題用詞不同、詳略不同，但描述同一事件，仍算重複）。
不確定是否重複時，請不要分在同一組（寧可漏判，不要誤判）。

新聞清單：
{numbered}

請「只」回傳一個 JSON 物件，不要加入任何說明文字或 Markdown 語法，格式如下：
{{"duplicate_groups": [[編號, 編號, ...], [編號, 編號, ...]]}}
只需列出「有重複」的分組（每組至少 2 個編號），沒有重複的項目不必列出。
若完全沒有重複項目，回傳 {{"duplicate_groups": []}}。
"""


def semantic_dedupe(items, api_key):
    if len(items) <= 1:
        return items

    from google import genai
    client = genai.Client(api_key=api_key)
    prompt = build_dedupe_prompt(items)

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        parsed = json.loads(_clean_json_block(response.text))
        groups = parsed.get("duplicate_groups", [])
    except Exception as e:
        print(f"[WARN] 語意去重呼叫或解析失敗，本次略過語意去重: {e}", file=sys.stderr)
        return items

    to_remove = set()
    for group in groups:
        valid_indices = sorted({i - 1 for i in group if isinstance(i, int) and 1 <= i <= len(items)})
        if len(valid_indices) < 2:
            continue
        keep_idx = None
        for idx in valid_indices:
            if "Google News 轉載" not in items[idx]["source"]:
                keep_idx = idx
                break
        if keep_idx is None:
            keep_idx = valid_indices[0]
        for idx in valid_indices:
            if idx != keep_idx:
                to_remove.add(idx)

    deduped = [item for i, item in enumerate(items) if i not in to_remove]
    print(f"[INFO] 語意去重：{len(items)} 筆 → {len(deduped)} 筆（移除 {len(to_remove)} 筆重複報導）")
    return deduped


CATEGORY_RULES = """
【01 政治】越南黨政高層動態（國家主席蘇林、總理黎明興、國會主席陳青敏、外交部長黎懷中、
公安部長梁三光、國防部長潘文江）之出訪/接待外賓/人事異動/紀律處分；外國專家對越南政治之評論；
中國駐越南大使在越南之活動；寮國黨政高層動態、出訪與接待。

【02 經濟】越南重要經濟政策、總體經濟數據（進出口、貿易順逆差、車市銷售）；越南與外國之經濟合作
與論壇；越南政府重大經濟投資（交通/半導體/高科技）；越南重要公司、台商、國際廠商在越投資動態；
外國專家對越南經濟之評論；寮國重大經濟政策。

【03 其他】越南重大民生新聞、涉及外國人新規定或全國關注事件；台越雙邊重要新聞（航線開通/取消、
重大災難、刑事案件）；越南勞工赴台相關重要新聞；越南舉辦之國際文教/體育活動或重大成就；
寮國全國矚目事件，或涉及台灣人之詐騙、犯罪及刑事案件。

若新聞內容與越南、寮國均無明顯關聯，或明顯與上述三類主題都不符，請回傳 category 為 "discard"。
"""


def build_classify_prompt(batch):
    numbered = "\n".join(
        f'{i+1}. 標題原文：「{it["title"]}」（來源：{it["source"]}）'
        for i, it in enumerate(batch)
    )
    return f"""你是一個新聞編輯助理，請針對以下每一則新聞，完成兩件事：
(a) 將標題翻譯／轉寫為「繁體中文」，語氣維持新聞標題的簡潔客觀風格；
(b) 依照下列分類規則，判斷這則新聞屬於 "01"（政治）、"02"（經濟）、"03"（其他），
    若都不符合則歸類為 "discard"。

分類規則：
{CATEGORY_RULES}

新聞清單：
{numbered}

請「只」回傳一個 JSON 陣列，不要加入任何說明文字或 Markdown 語法（不要用 ```json 包裹）。
陣列中每個元素格式為：
{{"index": 原始編號(數字), "title_zh": "翻譯後的繁體中文標題", "category": "01/02/03/discard"}}
陣列順序需與輸入清單一致，且每則輸入都必須有對應輸出。
"""


def call_gemini_classify_batch(batch, api_key):
    from google import genai

    client = genai.Client(api_key=api_key)
    prompt = build_classify_prompt(batch)

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        parsed = json.loads(_clean_json_block(response.text))
        result = {}
        for entry in parsed:
            idx = int(entry["index"]) - 1
            result[idx] = {
                "title_zh": entry.get("title_zh", "").strip(),
                "category": entry.get("category", "discard").strip(),
            }
        return result
    except Exception as e:
        print(f"[ERROR] Gemini 分類 API 呼叫或解析失敗: {e}", file=sys.stderr)
        return {}


def classify_and_translate(items, api_key, batch_size=15):
    output = []
    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        results = call_gemini_classify_batch(batch, api_key)
        for local_idx, item in enumerate(batch):
            info = results.get(local_idx)
            if not info or info["category"] == "discard":
                continue
            if info["category"] not in ("01", "02", "03"):
                continue
            output.append({
                "title": info["title_zh"] or item["title"],
                "link": item["link"],
                "source": item["source"],
                "category": info["category"],
                "published": item["published"].isoformat() if item.get("published") else None,
            })
        time.sleep(1)
    return output


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

    time_filtered = filter_by_time(raw_items, start, end)
    print(f"[INFO] 時間篩選後筆數: {len(time_filtered)}")

    url_deduped = dedupe_by_url(time_filtered)
    print(f"[INFO] 網址去重後筆數: {len(url_deduped)}")

    if not url_deduped:
        print("[INFO] 本次無符合時間區間之新聞，仍會輸出空白摘要檔以維持前端日期可查。")

    semantically_deduped = semantic_dedupe(url_deduped, api_key)

    classified = classify_and_translate(semantically_deduped, api_key)
    print(f"[INFO] Gemini 分類後保留筆數: {len(classified)}")

    write_daily_json(target_date, classified, now_vn)
    update_index(target_date)


if __name__ == "__main__":
    main()
