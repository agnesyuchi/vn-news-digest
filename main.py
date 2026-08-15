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
        "item_selector": "article, .post, .news-item",
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
      3.
