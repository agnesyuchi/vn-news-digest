import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any
import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from rapidfuzz import fuzz

# ==========================================
# 設定與常數定義
# ==========================================
GEMINI_MODEL = "gemini-2.0-flash"

# 設定越南時間 (UTC+7)
VN_TZ = timezone(timedelta(hours=7))
TODAY_STR = datetime.now(VN_TZ).strftime("%Y-%m-%d")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}

# ==========================================
# 1. 各管道新聞抓取模組
# ==========================================

def fetch_cna_rss() -> List[Dict[str, str]]:
    """ 管道 1: 中央社 (CNA) 官方 RSS """
    print("[1/5] 抓取 中央社 (CNA) RSS...")
    articles = []
    # 中央社即時新聞 RSS
    rss_url = "https://feeds.feedburner.com/rsscna/realtime"
    
    try:
        resp = requests.get(rss_url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content, "xml")
            for item in soup.find_all("item"):
                title = item.title.text.strip() if item.title else ""
                link = item.link.text.strip() if item.link else ""
                pub_date = item.pubDate.text.strip() if item.pubDate else ""
                
                # 篩選標題包含越南相關關鍵字的新聞
                if any(kw in title for kw in ["越南", "河內", "胡志明", "越共"]):
                    articles.append({
                        "title": title,
                        "url": link,
                        "source": "中央社 (CNA)",
                        "pub_date": pub_date
                    })
    except Exception as e:
        print(f"抓取 中央社 RSS 失敗: {e}")
        
    print(f" -> 中央社 RSS 抓取到 {len(articles)} 篇相關新聞")
    return articles


def fetch_yuenan_rss() -> List[Dict[str, str]]:
    """ 管道 2: yuenan.com 官方 RSS """
    print("[1/5] 抓取 yuenan.com RSS...")
    articles = []
    rss_url = "https://yuenan.com/feed/"
    
    try:
        resp = requests.get(rss_url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content, "xml")
            for item in soup.find_all("item"):
                title = item.title.text.strip() if item.title else ""
                link = item.link.text.strip() if item.link else ""
                pub_date = item.pubDate.text.strip() if item.pubDate else ""
                
                if title and link:
                    articles.append({
                        "title": title,
                        "url": link,
                        "source": "yuenan.com",
                        "pub_date": pub_date
                    })
    except Exception as e:
        print(f"抓取 yuenan.com RSS 失敗: {e}")
        
    print(f" -> yuenan.com RSS 抓取到 {len(articles)} 篇新聞")
    return articles


def fetch_google_news_keywords() -> List[Dict[str, str]]:
    """ 管道 3: Google News 廣域關鍵字搜尋 """
    print("[1/5] 抓取 Google News (關鍵字搜尋)...")
    articles = []
    # 使用包含越南經貿與政經的動態搜尋關鍵字
    query = "越南 (財經 OR 經濟 OR 投資 OR 政治 OR 供應鏈)"
    rss_url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    
    try:
        resp = requests.get(rss_url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content, "xml")
            for item in soup.find_all("item"):
                title = item.title.text.strip() if item.title else ""
                link = item.link.text.strip() if item.link else ""
                pub_date = item.pubDate.text.strip() if item.pubDate else ""
                
                title_clean = re.sub(r"\s*-\s*.*$", "", title)
                if title_clean and link:
                    articles.append({
                        "title": title_clean,
                        "url": link,
                        "source": "Google News",
                        "pub_date": pub_date
                    })
    except Exception as e:
        print(f"抓取 Google News 關鍵字失敗: {e}")
        
    print(f" -> Google News 抓取到 {len(articles)} 篇新聞")
    return articles


def fetch_vietnamplus_zh() -> List[Dict[str, str]]:
    """ 管道 4: Vietnam+ 中文網 (維持現狀) """
    print("[1/5] 直接爬取 Vietnam+ 中文網 (zh.vietnamplus.vn)...")
    articles = []
    url = "https://zh.vietnamplus.vn/"
    
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content, "html.parser")
            seen_urls = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                title = a.get_text(strip=True)
                
                if href.endswith(".vnp") and len(title) > 8:
                    full_url = href if href.startswith("http") else f"https://zh.vietnamplus.vn{href}"
                    if full_url not in seen_urls:
                        seen_urls.add(full_url)
                        articles.append({
                            "title": title,
                            "url": full_url,
                            "source": "Vietnam+ 中文網",
                            "pub_date": TODAY_STR
                        })
    except Exception as e:
        print(f"抓取 Vietnam+ 中文網失敗: {e}")
        
    print(f" -> Vietnam+ 中文網抓取到 {len(articles)} 篇新聞")
    return articles

# ==========================================
# 2. 本地標題與網址去重模組
# ==========================================

def deduplicate_articles(articles: List[Dict[str, str]], similarity_threshold: float = 75.0) -> List[Dict[str, str]]:
    """ 使用 rapidfuzz 進行本地標題相似度比對與 URL 去重 """
    print("[2/5] 執行本地新聞去重與過濾...")
    unique_articles = []
    seen_urls = set()
    
    for item in articles:
        url = item["url"]
        title = item["title"]
        
        if url in seen_urls:
            continue
            
        is_duplicate = False
        for existing in unique_articles:
            similarity = fuzz.token_set_ratio(title, existing["title"])
            if similarity >= similarity_threshold:
                is_duplicate = True
                break
                
        if not is_duplicate:
            seen_urls.add(url)
            unique_articles.append(item)
            
    print(f" -> 本地去重完成：原始總數 {len(articles)} 篇，初步過濾剩餘 {len(unique_articles)} 篇")
    return unique_articles

# ==========================================
# 3. Gemini 語意重疊判斷與三大分類模組
# ==========================================

def process_with_gemini(articles: List[Dict[str, str]]) -> Dict[str, Any]:
    """ 透過 Gemini 2.0 Flash 進行深層語意去重、事件聚類，並強制劃分為 3 個分類 """
    print("[3/5] 送交 Gemini 進行語意分析、事件去重與 3 大類別劃分...")
    
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("錯誤：找不到 GEMINI_API_KEY 環境變數。")
        sys.exit(1)
        
    client = genai.Client(api_key=api_key)
    articles_input_text = json.dumps(articles, ensure_ascii=False, indent=2)
    
    prompt = f"""
你是一位專業的越南情勢與經貿新聞主編。請針對以下傳入的所有新聞列表進行語意分析與整理：

新聞列表 (JSON):
{articles_input_text}

任務要求：
1. **語意去重與事件聚類**：
   - 仔細比對文章內容，若多篇新聞報導的是『同一個事件或議題』，請合併為一篇精華報導，絕不要出現重複事件。
2. **精選與翻譯**：
   - 保留最具代表性、價值的 6 至 15 篇新聞。
   - 標題（title）：改寫為簡潔流暢的繁體中文標題。
   - 摘要（summary）：撰寫 60-120 字的繁體中文重點摘要。
3. **限定『三個分類』**：
   請將每一篇新聞精準歸類至以下【三個分類之一】，不可出現其他分類名稱：
   - **政治**（包含：政府政策、高層動態、外交關係、法律法規、時政要聞）
   - **經濟**（包含：總體經濟、金融、貿易、產業供應鏈、投資、房地產、股市）
   - **其他**（包含：社會新聞、文化旅遊、科技創新、民生、教育、勞工）

請嚴格輸出符合以下 JSON 格式數據，不要包含任何 Markdown 標籤（如 ```json）：

{{
  "date": "{TODAY_STR}",
  "total_count": 0,
  "articles": [
    {{
      "title": "繁體中文標題",
      "summary": "繁體中文摘要說明...",
      "category": "政治",
      "url": "原始新聞連結",
      "source": "原始新聞來源"
    }}
  ]
}}
"""

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2
            )
        )
        
        raw_text = response.text.strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:]
        if raw_text.startswith("```"):
            raw_text = raw_text[3:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]
            
        result_json = json.loads(raw_text.strip())
        
        # 統計各類別數量
        articles_list = result_json.get("articles", [])
        result_json["total_count"] = len(articles_list)
        
        pol_count = sum(1 for a in articles_list if a.get("category") == "政治")
        eco_count = sum(1 for a in articles_list if a.get("category") == "經濟")
        oth_count = sum(1 for a in articles_list if a.get("category") == "其他")
        
        print(f" -> AI 處理成功！共保留 {len(articles_list)} 篇 (政治: {pol_count} / 經濟: {eco_count} / 其他: {oth_count})")
        return result_json
        
    except Exception as e:
        print(f"Gemini API 處理失敗: {e}")
        return {
            "date": TODAY_STR,
            "total_count": 0,
            "articles": []
        }

# ==========================================
# 4. 檔案儲存與索引更新模組
# ==========================================

def save_data_and_update_index(data: Dict[str, Any]):
    """ 儲存每日 JSON 並更新 index.json """
    print("[4/5] 寫入 JSON 檔案與更新 index.json...")
    os.makedirs("data", exist_ok=True)
    
    daily_file = f"data/{TODAY_STR}.json"
    with open(daily_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f" -> 已儲存每日資料至: {daily_file}")
    
    index_file = "data/index.json"
    available_dates = []
    
    if os.path.exists(index_file):
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                index_data = json.load(f)
                available_dates = index_data.get("dates", [])
        except Exception:
            available_dates = []
            
    if TODAY_STR not in available_dates:
        available_dates.append(TODAY_STR)
        
    available_dates.sort(reverse=True)
    
    with open(index_file, "w", encoding="utf-8") as f:
        json.dump({"dates": available_dates}, f, ensure_ascii=False, indent=2)
        
    print(f" -> 已更新 index.json，目前資料庫共有 {len(available_dates)} 天紀錄")

# ==========================================
# 主執行流程
# ==========================================

def main():
    print(f"=== 開始執行越南新聞彙整排程 [{TODAY_STR}] ===")
    
    # 1. 四大管道並行抓取
    cna = fetch_cna_rss()
    yuenan = fetch_yuenan_rss()
    gnews = fetch_google_news_keywords()
    vnplus = fetch_vietnamplus_zh()
    
    raw_articles = cna + yuenan + gnews + vnplus
    
    if not raw_articles:
        print("未抓取到任何新聞，流程結束。")
        return

    # 2. 本地字面相似度去重
    cleaned_articles = deduplicate_articles(raw_articles, similarity_threshold=75.0)
    
    # 3. Gemini 深層語意去重與「政治、經濟、其他」三分法
    final_data = process_with_gemini(cleaned_articles)
    
    # 4. 寫入 JSON 檔與索引檔
    save_data_and_update_index(final_data)
    
    print("=== 所有程序執行完成 ===")

if __name__ == "__main__":
    main()
