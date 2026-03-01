# 🎙️ Podcast Auto Summary

> 全自動監控 Apple Podcast → 語音轉文字 → AI 摘要 → Email + Telegram 推送
>
> **零成本運行**：GitHub Actions 免費額度 + OpenAI API 每月約 NT$30

```
每天自動觸發（台灣 08:00 / 20:00）
        ↓
  抓 RSS Feed 檢查新集數
        ↓
    有新集數？
    ↙        ↘
  沒有        有
  結束      下載音訊 MP3
              ↓
        Whisper API 轉文字
              ↓
        GPT 生成摘要
              ↓
        ┌─────┴─────┐
      寄信          Telegram Bot
      Gmail         發送通知
```

---

## 🚀 5 分鐘快速設定

### Step 1：Fork 這個 Repo

點右上角 **Fork** → 建立你自己的副本

### Step 2：找到 Podcast 的 RSS Feed URL

每個 Apple Podcast 節目都有一個 RSS Feed，這是自動化的入口。

**方法 A（最簡單）：**
1. 前往 https://getrssfeed.com/
2. 貼上 Apple Podcasts 的節目連結
3. 取得 RSS URL

**方法 B：Apple Lookup API**
1. 找到節目的 Apple Podcasts 連結，格式像：
   `https://podcasts.apple.com/tw/podcast/節目名稱/id1234567890`
2. 複製 `id` 後面的數字（如 `1234567890`）
3. 瀏覽器打開：`https://itunes.apple.com/lookup?id=1234567890&entity=podcast`
4. 在 JSON 回傳中找 `feedUrl` 欄位

**方法 C：Podcast Index**
1. 前往 https://podcastindex.org/
2. 搜尋節目名稱 → 取得 RSS Feed

### Step 3：申請 OpenAI API Key

1. 前往 https://platform.openai.com/api-keys
2. 建立新的 API Key
3. 儲值 $5 美元（約用 3-6 個月）

### Step 4：建立 Telegram Bot

1. 在 Telegram 搜尋 **@BotFather**
2. 輸入 `/newbot` → 按照指示建立 → 取得 **Bot Token**
3. 搜尋 **@userinfobot** → 發送任意訊息 → 取得你的 **Chat ID**
4. 重要：先發一則訊息給你的 Bot（隨便打個 hi），否則 Bot 無法主動發訊息給你

### Step 5：設定 Gmail App Password

1. 前往 https://myaccount.google.com/security
2. 開啟**兩步驟驗證**（如果還沒開）
3. 在兩步驟驗證頁面最下方，找到 **應用程式密碼**
4. 新增一個應用程式密碼（名稱隨意，如 `podcast-bot`）
5. 複製產生的 16 位密碼

### Step 6：設定 GitHub Secrets

進入你 Fork 的 Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

逐一新增以下 Secrets：

| Secret 名稱 | 值 | 必要 |
|---|---|---|
| `PODCAST_RSS_URL` | Podcast 的 RSS Feed URL | ✅ 必要 |
| `OPENAI_API_KEY` | OpenAI API Key | ✅ 必要 |
| `EMAIL_SENDER` | 你的 Gmail 地址 | 📧 寄信用 |
| `EMAIL_PASSWORD` | Gmail App Password（16位） | 📧 寄信用 |
| `EMAIL_RECIPIENT` | 收件人 Email（可以跟寄件人一樣） | 📧 寄信用 |
| `TG_BOT_TOKEN` | Telegram Bot Token | 📱 TG用 |
| `TG_CHAT_ID` | 你的 Telegram Chat ID | 📱 TG用 |
| `PODCAST_LANG` | `zh`（中文）或 `en`（英文） | 選填，預設 zh |
| `SUMMARY_MODEL` | `gpt-4o-mini`（便宜）或 `gpt-4o`（更好） | 選填 |

### Step 7：啟動 & 測試

1. 進入 Repo → **Actions** 頁籤
2. 點左側的 **🎙️ Podcast Auto Summary**
3. 點右側 **Run workflow** → 勾選 **test_notify** → 點 **Run**
4. 等待執行完成，檢查你的 Email 和 Telegram 是否收到測試訊息
5. 確認成功後，再跑一次，勾選 **force** 來測試完整流程

設定完成！之後每天會自動檢查兩次 🎉

---

## 📁 專案結構

```
podcast-auto-summary/
├── .github/
│   └── workflows/
│       └── podcast_summary.yml   # GitHub Actions 排程設定
├── podcast_monitor.py            # 主程式
├── requirements.txt              # Python 套件
├── processed_episodes.json       # 已處理記錄（自動生成）
└── README.md                     # 本文件
```

---

## ⚙️ 自訂設定

### 修改檢查頻率

編輯 `.github/workflows/podcast_summary.yml` 中的 cron：

```yaml
schedule:
  # 每 6 小時檢查一次
  - cron: '0 */6 * * *'

  # 只在週三和週六檢查（配合更新時間）
  - cron: '0 12 * * 3,6'

  # 每天台灣時間晚上 9 點
  - cron: '0 13 * * *'
```

> ⏰ Cron 用 UTC 時間，台灣 = UTC+8
> 台灣 08:00 = UTC 00:00 / 台灣 20:00 = UTC 12:00

### 修改摘要風格

編輯 `podcast_monitor.py` 中的 `SUMMARY_PROMPT` 變數，例如：

```python
SUMMARY_PROMPT = """請用簡短的條列式列出這集 Podcast 的 3 個重點，每點不超過 20 字。"""
```

### 同時追蹤多個 Podcast

在 Secrets 中設定多個 RSS URL（用逗號分隔），然後修改程式碼：

```python
rss_urls = RSS_URL.split(",")
for url in rss_urls:
    # 對每個 URL 執行檢查...
```

---

## 💰 費用估算

| 項目 | 費用 |
|---|---|
| GitHub Actions | 免費（每月 2,000 分鐘） |
| OpenAI Whisper | ~$0.36/集（60分鐘音檔） |
| GPT-4o-mini 摘要 | ~$0.01/集 |
| Telegram | 免費 |
| Gmail | 免費 |
| **每月（一週 2 集）** | **約 $3 美金 ≈ NT$100** |

> 💡 用 `gpt-4o-mini` 摘要品質已經很好，比 `gpt-4o` 便宜 30 倍

---

## 🔧 疑難排解

### Q: GitHub Actions 沒有自動執行？
A: 確認你的 Repo 有 **Actions** 啟用（Settings → Actions → General → Allow all actions）。
   注意：Fork 的 Repo 預設可能會暫停 scheduled workflows，進入 Actions 頁面點 **Enable** 即可。

### Q: Email 寄不出去？
A: 確認你用的是 **App Password**（16位），不是你的 Gmail 登入密碼。且 Gmail 帳號需開啟兩步驟驗證。

### Q: Telegram 收不到訊息？
A: 確認你有先發一則訊息給 Bot（在 Telegram 中找到你的 Bot 並發送 `/start` 或任意文字）。

### Q: 音檔太大，轉錄失敗？
A: 程式會自動壓縮和分段處理。如果還是失敗，檢查 GitHub Actions 的 log 看錯誤訊息。

### Q: 摘要品質不好？
A: 嘗試在 Secrets 中把 `SUMMARY_MODEL` 改成 `gpt-4o`，或調整 `SUMMARY_PROMPT`。

### Q: 想用 Claude API 替代 GPT？
A: 安裝 `anthropic` 套件，修改 `generate_summary()` 函式改用 Claude API。

---

## 📜 License

MIT License - 自由使用、修改、分發。
