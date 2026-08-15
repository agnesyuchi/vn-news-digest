# 越寮觀察 — 越南/寮國每日新聞摘要網站

完全免費的自動化新聞摘要網站：GitHub Actions 每日定時抓取新聞 → 呼叫 Google Gemini API 翻譯與分類 → 自動發布到 GitHub Pages。

以下是從零開始的完整設定步驟，即使沒有寫程式經驗也可以照著做。

---

## 檔案結構總覽

```
vn-news-digest/
├── main.py                      # 抓取 + AI 翻譯分類的主程式
├── requirements.txt              # Python 套件清單
├── index.html                    # 網頁主頁
├── style.css                     # 網頁樣式
├── script.js                     # 網頁互動邏輯
├── data/
│   ├── index.json                # 可用日期清單（程式自動維護）
│   └── 2026-08-15.json           # 範例資料（示範用，可刪除）
├── .github/
│   └── workflows/
│       └── daily_build.yml       # GitHub Actions 排程設定
└── README.md                     # 本說明文件
```

---

## 第一步：註冊 GitHub 帳號並建立 Repository

1. 前往 [github.com](https://github.com) 註冊帳號（已有帳號可跳過）。
2. 登入後點右上角 **+** → **New repository**。
3. Repository name 填寫例如 `vn-news-digest`。
4. **務必選擇 Public**（Private repo 的 GitHub Pages 在免費方案有較多限制，Public 最單純）。
5. 不要勾選「Add a README file」（因為我們自己準備好了），直接點 **Create repository**。

---

## 第二步：把專案檔案上傳到 GitHub

最簡單的方式是直接在網頁上傳，不需要安裝任何軟體：

1. 進入剛建立的 repo 頁面，點 **uploading an existing file**（或 Add file → Upload files）。
2. 把 `main.py`、`requirements.txt`、`index.html`、`style.css`、`script.js`、`README.md` 拖曳上傳。
3. **`.github/workflows/daily_build.yml` 這個檔案比較特殊**，因為資料夾名稱開頭是 `.`，網頁上傳介面通常可以直接把整個資料夾拖進去（會自動建立巢狀路徑）。若拖曳失敗，改用「第二步（進階）」的 Git 指令方式。
4. `data/index.json` 與 `data/2026-08-15.json` 也一併上傳（範例資料可以先放著，之後會被每日排程自動更新覆蓋）。
5. 上傳完成後，在下方填寫 commit message（例如「初始上傳」），點 **Commit changes**。

### 第二步（進階）：使用 Git 指令上傳（推薦，較不易出錯）

如果你的電腦有安裝 Git，這個方式比網頁拖曳更可靠：

```bash
cd vn-news-digest
git init
git add .
git commit -m "初始上傳"
git branch -M main
git remote add origin https://github.com/你的帳號/vn-news-digest.git
git push -u origin main
```

---

## 第三步：取得 Google AI Studio 的 Gemini API Key（免費）

1. 前往 [Google AI Studio](https://aistudio.google.com/)，用 Google 帳號登入。
2. 左側選單找到 **Get API key**（或 API Keys）。
3. 點 **Create API key**，選擇一個 Google Cloud 專案（沒有的話會引導你自動建立一個）。
4. 建立完成後會顯示一串金鑰字串，**先複製起來，稍後會用到**（離開頁面後不會再完整顯示，如需要可重新產生）。
5. 免費額度依 Google 當前方案而定，一般個人測試/每日一次的用量足夠使用；若之後遇到額度超限，可以在 main.py 中調整批次大小（`batch_size`）或減少抓取來源數量。

---

## 第四步：把 API Key 存入 GitHub Secrets（重要：不要寫在程式碼裡）

1. 回到你的 GitHub repo 頁面，點上方 **Settings**。
2. 左側選單找到 **Secrets and variables** → **Actions**。
3. 點 **New repository secret**。
4. Name 填寫：`GEMINI_API_KEY`（必須完全一致，因為 workflow 檔案裡是用這個名稱讀取）。
5. Value 貼上你剛剛複製的 Gemini API 金鑰。
6. 點 **Add secret** 完成。

這樣 API 金鑰就會安全地存在 GitHub 後台，不會出現在程式碼或公開頁面上。

---

## 第五步：啟用並測試 GitHub Actions 排程

1. 點 repo 上方的 **Actions** 分頁。
2. 如果是第一次使用，GitHub 可能會顯示提示，點 **I understand my workflows, go ahead and enable them**。
3. 左側應該會看到一個名為 **Daily Vietnam/Laos News Digest** 的 workflow。
4. 點進去後，右側會有 **Run workflow** 按鈕（因為我們在 yml 裡加了 `workflow_dispatch`，允許手動執行），點下去先手動跑一次測試，不用等到隔天排程時間。
5. 執行過程中可以點進去看即時 log，確認：
   - 有沒有成功抓到新聞（`[INFO] 抓取到原始新聞筆數: ...`）
   - Gemini API 呼叫有沒有出錯
   - 最後是否成功 commit 新的 JSON 檔回 repo
6. **若第一次執行失敗是正常的**，最常見原因是「HTML 版面的 CSS selector 需要依實際網站結構調整」（見下方「重要注意事項」）。

排程本身已設定為 **每天 UTC 00:00（越南時間 07:00）自動執行**，不需要額外操作，之後會自動持續更新。

---

## 第六步：啟用 GitHub Pages

1. 在 repo 的 **Settings** 頁面，左側選單找到 **Pages**。
2. 在 **Build and deployment** 區塊：
   - Source 選擇 **Deploy from a branch**
   - Branch 選擇 **main**，資料夾選擇 **/ (root)**
3. 點 **Save**。
4. 等待約 1 分鐘，重新整理頁面，上方會出現一個網址，格式類似：
   `https://你的帳號.github.io/vn-news-digest/`
5. 打開這個網址，就能看到你的新聞摘要網站了。

---

## 第七步：確認整體流程是否成功運作

- 打開網站，確認畫面正常顯示範例新聞卡片（政治/經濟/其他三欄）。
- 手動觸發一次 Actions（第五步），跑完後回到網站重新整理，確認畫面內容有沒有更新成真實抓取的新聞。
- 用手機打開網址，確認版面有正常縮成單欄、日期選擇器方便點擊。

---

## 重要注意事項（部署前務必確認）

### 1. HTML 爬蟲的 CSS selector 需要依實際網站調整

`main.py` 裡 `SOURCES` 設定中，`type: "html"` 的來源（越南投資 yuenan.com、中央社搜尋頁）所使用的 `item_selector` / `title_selector` / `link_selector` 是**範例寫法**，不同網站的實際 HTML 結構不同。請依以下方式確認並調整：

1. 用瀏覽器打開該新聞列表頁。
2. 在任一則新聞標題上按右鍵 → **檢查（Inspect）**。
3. 觀察該則新聞外層容器的 HTML 標籤與 `class` 名稱（例如 `<div class="news-item">`），把這個名稱填入 `main.py` 對應的 selector 設定中。
4. 若不熟悉 CSS selector，可以把「該新聞條目的完整 HTML 片段」提供給我，我可以協助寫出正確的 selector。

### 2. RSS 網址請以官網公告為準

`main.py` 中 VietnamPlus 的 RSS 網址（`zh.vietnamplus.vn/rss/home.rss`）為常見命名慣例的推測寫法，實際路徑請至該網站頁尾或原始碼中確認是否有 `<link rel="alternate" type="application/rss+xml">` 標籤，取得正確網址後替換。

### 3. Google News RSS 已可直接使用

`https://news.google.com/rss/search?q=關鍵字&hl=zh-TW&gl=TW&ceid=TW:zh-Hant` 是 Google 官方標準格式，通常不需要調整即可運作。

### 4. 時間篩選的容錯設計

部分來源網站的列表頁可能沒有清楚標示發布時間，這種情況下 `main.py` 會保留該則新聞讓 Gemini 依內容自行判斷是否為近期新聞，而不會直接捨棄。若要更嚴謹地依時間篩選，建議之後針對這些來源另外找出詳細頁面中的發布時間標籤並更新 `time_selector`。

### 5. 免費額度與費用

- GitHub Actions：Public repo 完全免費、無限制次數（每日一次排程遠低於免費額度）。
- GitHub Pages：完全免費。
- Google AI Studio Gemini API：有免費額度，個人測試與每日一次的用量通常足夠；若之後有大量新聞來源或想提高抓取頻率，建議留意 Google AI Studio 後台的用量儀表板。

### 6. 法律與網站條款

抓取前請留意各新聞來源的 `robots.txt` 與使用條款，本專案設計上僅擷取「標題 + 連結」並導流回原網站，不轉載全文內容。

---

## 之後如果想要調整

- **新增/移除新聞來源**：編輯 `main.py` 中的 `SOURCES` 清單。
- **調整分類規則**：編輯 `main.py` 中的 `CATEGORY_RULES` 字串。
- **調整排程時間**：編輯 `.github/workflows/daily_build.yml` 中的 `cron` 設定（時間為 UTC）。
- **調整網頁樣式**：編輯 `style.css`（顏色變數集中在檔案開頭的 `:root` 區塊，方便統一調整）。
