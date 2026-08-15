(function () {
  "use strict";

  const dateInput = document.getElementById("date-input");
  const updatedText = document.getElementById("updated-text");
  const loadingEl = document.getElementById("loading");
  const emptyEl = document.getElementById("empty");
  const sectionsEl = document.getElementById("sections");
  const themeToggle = document.getElementById("theme-toggle");

  const CATEGORY_CARD_CLASS = {
    "01": "cat-political-card",
    "02": "cat-economic-card",
    "03": "cat-other-card",
  };

  // ---------------------------------------------------------------
  // 主題（深色 / 淺色）
  // 依系統偏好決定初始主題；不使用瀏覽器儲存機制，重新整理後會回到系統預設。
  // ---------------------------------------------------------------
  function initTheme() {
    const prefersLight = window.matchMedia("(prefers-color-scheme: light)").matches;
    if (prefersLight) {
      document.documentElement.setAttribute("data-theme", "light");
    }
    updateToggleLabel();
  }

  function updateToggleLabel() {
    const isLight = document.documentElement.getAttribute("data-theme") === "light";
    themeToggle.textContent = isLight ? "◑" : "◐";
    themeToggle.setAttribute(
      "aria-label",
      isLight ? "切換為深色模式" : "切換為淺色模式"
    );
  }

  themeToggle.addEventListener("click", function () {
    const isLight = document.documentElement.getAttribute("data-theme") === "light";
    if (isLight) {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.setAttribute("data-theme", "light");
    }
    updateToggleLabel();
  });

  // ---------------------------------------------------------------
  // 資料載入
  // ---------------------------------------------------------------

  function showState(state) {
    // state: "loading" | "empty" | "data"
    loadingEl.hidden = state !== "loading";
    emptyEl.hidden = state !== "empty";
    sectionsEl.hidden = state !== "data";
  }

  function formatDateTime(isoString) {
    if (!isoString) return "未知";
    try {
      const d = new Date(isoString);
      return d.toLocaleString("zh-TW", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch (e) {
      return isoString;
    }
  }

  function buildCard(item, category) {
    const li = document.createElement("li");
    li.className = "news-card " + (CATEGORY_CARD_CLASS[category] || "");

    const a = document.createElement("a");
    a.href = item.link;
    a.target = "_blank";
    a.rel = "noopener noreferrer";

    const title = document.createElement("p");
    title.className = "news-title";
    title.textContent = item.title;

    const meta = document.createElement("div");
    meta.className = "news-meta";

    const sourceSpan = document.createElement("span");
    sourceSpan.textContent = item.source || "未知來源";
    meta.appendChild(sourceSpan);

    if (item.published) {
      const dot = document.createElement("span");
      dot.className = "dot";
      dot.textContent = "·";
      meta.appendChild(dot);

      const timeSpan = document.createElement("span");
      timeSpan.textContent = formatDateTime(item.published);
      meta.appendChild(timeSpan);
    }

    a.appendChild(title);
    a.appendChild(meta);
    li.appendChild(a);
    return li;
  }

  function renderDigest(payload) {
    const lists = {
      "01": document.getElementById("list-01"),
      "02": document.getElementById("list-02"),
      "03": document.getElementById("list-03"),
    };

    Object.keys(lists).forEach((cat) => {
      lists[cat].innerHTML = "";
    });

    let total = 0;
    Object.keys(lists).forEach((cat) => {
      const items = (payload.categories && payload.categories[cat]) || [];
      total += items.length;
      items.forEach((item) => {
        lists[cat].appendChild(buildCard(item, cat));
      });
    });

    updatedText.textContent = "資料最後更新：" + formatDateTime(payload.generated_at);

    if (total === 0) {
      showState("empty");
    } else {
      showState("data");
    }
  }

  function loadDigest(dateStr) {
    showState("loading");
    fetch("data/" + dateStr + ".json", { cache: "no-store" })
      .then((res) => {
        if (!res.ok) throw new Error("找不到當日資料");
        return res.json();
      })
      .then((payload) => {
        renderDigest(payload);
      })
      .catch(() => {
        updatedText.textContent = "資料最後更新：無資料";
        showState("empty");
      });
  }

  // ---------------------------------------------------------------
  // 初始化：讀取 index.json 取得可用日期範圍，預設載入最新一日
  // ---------------------------------------------------------------

  function init() {
    initTheme();
    showState("loading");

    fetch("data/index.json", { cache: "no-store" })
      .then((res) => {
        if (!res.ok) throw new Error("尚無 index.json");
        return res.json();
      })
      .then((indexData) => {
        const dates = indexData.dates || [];
        const latest = indexData.latest || dates[dates.length - 1];

        if (dates.length > 0) {
          dateInput.min = dates[0];
          dateInput.max = dates[dates.length - 1];
        }

        if (latest) {
          dateInput.value = latest;
          loadDigest(latest);
        } else {
          showState("empty");
        }
      })
      .catch(() => {
        // 尚未有任何資料檔（例如剛部署、Actions 尚未第一次執行）
        const today = new Date().toISOString().slice(0, 10);
        dateInput.value = today;
        updatedText.textContent = "資料最後更新：尚無資料";
        showState("empty");
      });
  }

  dateInput.addEventListener("change", function () {
    if (dateInput.value) {
      loadDigest(dateInput.value);
    }
  });

  init();
})();
