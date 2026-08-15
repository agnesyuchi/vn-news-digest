#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py — 越南/寮國新聞每日摘要產生器

流程：
  1. 從各新聞來源抓取「標題 + 連結 + 發布時間（若有）」
  2. 篩選出落在「越南時間前一日 07:00:00 ~ 當日 06:59:59」區間內的新聞
  3. 呼叫 Google AI Studio 的 Gemini API，批次進行「翻譯為繁體中文標題」+「分類（01政治/02經濟/03其他/discard）」
  4. 將結果寫入 data/YYYY-MM-DD.json，並更新 data/index.json（可用日期清單）

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

import pytz
import requests
import feedparser
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 基本設定
# ---------------------------------------------------------------------------

VN_TZ = pytz.timezone("Asia/Ho_Chi_Minh")  # UTC+7，寮國與越南同時區
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
GEMINI_MODEL = "gemini-1.5-flash"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# 關鍵字（用於 Google News RSS 搜尋，以及 CNA 網站內搜尋）
KEYWORDS = ["越南", "寮國"]

# ---------------------------------------------------------------------------
# 資料來源設定
# ---------------------------------------------------------------------------
# type = "rss"  : 直接用 feedparser 解析
# type = "html" : 用 requests + BeautifulSoup 解析，需自行依實際網站結構調整
#                 list_selector / title_selector / link_selector 為 CSS selector
#
# 重要：yuenan.com、CNA 搜尋結果頁的實際 HTML 結構會隨網站改版而變動，
#       下方 selector 僅為「範例寫法」，部署前請務必打開瀏覽器「檢查元素」
#       確認實際 class/id 名稱，並修改對應 selector。

SOURCES = [
    {
        "name": "越南投資 (yuenan.com)",
        "type": "html",
        "list_url": "https://yuenan.com/news/",
        # 每則新聞卡片的容器
        "item_selector": "article, .post, .news-item",
        # 相對於 item 容器內的標題與連結
        "title_selector": "h2, h3, .entry-title",
        "link_selector": "a",
        "time_selector": "time, .date, .post-date",
    },
    {
        "name": "VietnamPlus 中文網",
        "type": "rss",
        # VietnamPlus 多語版通常提供 RSS，實際路徑請以官網公告為準，
        # 若中文版無獨立 RSS，可改用其英文/越南文版 RSS 後仍以標題原文送入 Gemini 翻譯。
        "feed_url": "https://zh.vietnamplus.vn/rss/home.rss",
    },
    {
        "name": "中央通訊社 (CNA) - 越南",
        "type": "html",
        "list_url": "https://www.cna.com.tw/search/hasco.aspx?q=%E8%B6%8A%E5%8D%97",
        "item_selector": "li.item, .searchList li",
        "title_selector": "h2, .title",
        "link_selector": "a",
        "time_selector": "time, .date",
    },
    {
        "name": "中央通訊社 (CNA) - 寮國",
        "type": "html",
        "list_url": "https://www.cna.com.tw/search/hasco.aspx?q=%E5%AF%AE%E5%9C%8B",
        "item_selector": "li.item, .searchList li",
        "title_selector": "h2, .title",
        "link_selector": "a",
        "time_selector": "time, .date",
    },
]

# Google News RSS：依關鍵字動態組出搜尋 RSS 網址
def google_news_rss_sources():
    sources = []
    for kw in KEYWORDS:
        sources.append({
            "name": f"Google News - {kw}",
            "type": "rss",
            "feed_url": (
                f"https://news.google.com/rss/search?q={kw}"
                f"&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
            ),
        })
    return sources


# ---------------------------------------------------------------------------
# 抓取函式
# ---------------------------------------------------------------------------

def fetch_rss(source):
    """解析 RSS/Atom feed，回傳 [{title, link, published, source}, ...]"""
    items = []
    try:
        feed = feedparser.parse(source["feed_url"])
        for entry in feed.entries:
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "").strip()
            published_dt = None
            for time_field in ("published_parsed", "updated_parsed"):
                tstruct = getattr(entry, time_field, None)
                if tstruct:
                    published_dt = datetime(*tstruct[:6], tzinfo=pytz.UTC).astimezone(VN_TZ)
                    break
            if title and link:
                items.append({
                    "title": title,
                    "link": link,
                    "published": published_dt,
                    "source": source["name"],
                })
    except Exception as e:
        print(f"[WARN] RSS 抓取失敗 ({source['name']}): {e}", file=sys.stderr)
    return items


def fetch_html(source):
    """用 requests + BeautifulSoup 解析新聞列表頁。
    無法取得精確發布時間時，published 設為 None，
    後續會改用「本次執行時間」做寬鬆判斷（詳見 filter_by_time 的容錯邏輯）。
    """
    items = []
    try:
        resp = requests.get(source["list_url"], headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        nodes = soup.select(source["item_selector"])
        for node in nodes:
            title_el = node.select_one(source["title_selector"])
            link_el = node.select_one(source["link_selector"])
            if not title_el or not link_el:
                continue
            title = title_el.get_text(strip=True)
            link = link_el.get("href", "").strip()
            if link and not link.startswith("http"):
                # 補上網域，避免相對路徑連結失效
                from urllib.parse import urljoin
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
    except Exception as e:
        print(f"[WARN] HTML 抓取失敗 ({source['name']}): {e}", file=sys.stderr)
    return items


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


def collect_all_items():
    all_items = []
    for source in SOURCES + google_news_rss_sources():
        if source["type"] == "rss":
            all_items.extend(fetch_rss(source))
        else:
            all_items.extend(fetch_html(source))
        time.sleep(1)  # 禮貌性間隔，避免對來源網站造成負擔
    return all_items


# ---------------------------------------------------------------------------
# 時間篩選：越南時間前一日 07:00:00 ~ 當日 06:59:59
# ---------------------------------------------------------------------------

def get_time_window(now_vn):
    end = now_vn.replace(hour=6, minute=59, second=59, microsecond=0)
    if now_vn.hour < 7:
        # 若執行時間早於當日 07:00（理論上排程固定在 07:00 執行，此為保險判斷）
        end = end
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
            # 抓不到精確發布時間的來源（多半是 HTML 版型未提供時間標籤），
            # 先保留讓 Gemini 依內容判斷是否為近期新聞；
            # 若要更嚴謹，建議之後補齊各來源的時間 selector。
            filtered.append(item)
            continue
        if start <= pub <= end:
            filtered.append(item)
    return filtered


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------

def dedupe(items):
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
# Gemini API：批次翻譯 + 分類
# ---------------------------------------------------------------------------

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


def build_prompt(batch):
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


def call_gemini_batch(batch, api_key):
    """呼叫 Gemini API 進行批次翻譯與分類，回傳 dict: index -> {title_zh, category}"""
    from google import genai

    client = genai.Client(api_key=api_key)
    prompt = build_prompt(batch)

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        raw_text = response.text.strip()
        # 保險起見，去除可能出現的 Markdown code fence
        raw_text = re.sub(r"^```(json)?|```$", "", raw_text, flags=re.MULTILINE).strip()
        parsed = json.loads(raw_text)
        result = {}
        for entry in parsed:
            idx = int(entry["index"]) - 1
            result[idx] = {
                "title_zh": entry.get("title_zh", "").strip(),
                "category": entry.get("category", "discard").strip(),
            }
        return result
    except Exception as e:
        print(f"[ERROR] Gemini API 呼叫或解析失敗: {e}", file=sys.stderr)
        return {}


def classify_and_translate(items, api_key, batch_size=15):
    """將 items 分批送入 Gemini，回傳篩選/翻譯/分類後的新聞清單。"""
    output = []
    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        results = call_gemini_batch(batch, api_key)
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
        time.sleep(1)  # 批次間稍作停頓，降低 API 速率限制風險
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

    time_filtered = filter_by_time(raw_items, start, end)
    print(f"[INFO] 時間篩選後筆數: {len(time_filtered)}")

    deduped = dedupe(time_filtered)
    print(f"[INFO] 去重後筆數: {len(deduped)}")

    if not deduped:
        print("[INFO] 本次無符合時間區間之新聞，仍會輸出空白摘要檔以維持前端日期可查。")

    classified = classify_and_translate(deduped, api_key)
    print(f"[INFO] Gemini 分類後保留筆數: {len(classified)}")

    write_daily_json(target_date, classified, now_vn)
    update_index(target_date)


if __name__ == "__main__":
    main()
