#!/usr/bin/env python3
"""
Podcast Auto Monitor + Transcribe + Summarize
=============================================
自動監控 Apple Podcast RSS → 下載音檔 → Whisper 轉文字 → AI 摘要 → Email + Telegram 推送

用法：
  python podcast_monitor.py              # 正常執行（檢查新集數）
  python podcast_monitor.py --force      # 強制處理最新一集（跳過時間檢查，用於測試）
  python podcast_monitor.py --test-notify # 只測試 Email + Telegram 通知
"""

import os
import sys
import json
import time
import hashlib
import smtplib
import argparse
import tempfile
import requests
import feedparser
from pathlib import Path
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from openai import OpenAI

# ============================================================
# 設定區：所有敏感資訊都從環境變數讀取
# ============================================================

# Podcast RSS Feed URL
RSS_URL = os.environ.get("PODCAST_RSS_URL", "")

# OpenAI API（用於 Whisper 轉錄 + GPT 摘要）
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Email 設定（Gmail）
EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")  # Gmail App Password
EMAIL_RECIPIENT = os.environ.get("EMAIL_RECIPIENT", "")
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))

# Telegram Bot 設定
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# 摘要用的 AI 模型（可選 gpt-4o、gpt-4o-mini、gpt-3.5-turbo）
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "gpt-4o-mini")

# 檢查新集數的時間窗口（小時）
CHECK_HOURS = int(os.environ.get("CHECK_HOURS", "24"))

# Podcast 語言（zh = 中文，en = 英文，ja = 日文）
PODCAST_LANG = os.environ.get("PODCAST_LANG", "zh")

# 已處理集數的記錄檔（避免重複處理）
PROCESSED_FILE = os.environ.get("PROCESSED_FILE", "processed_episodes.json")


# ============================================================
# 摘要 Prompt
# ============================================================

SUMMARY_PROMPT = """你是一位專業的 Podcast 摘要助手。請根據以下 Podcast 逐字稿，用繁體中文產出結構化摘要。

請按照以下格式輸出：

📌 一句話總結
（用 30 字以內概括這集的核心主題）

🔑 核心重點
1. （第一個重點，1-2 句話說明）
2. （第二個重點）
3. （第三個重點）
4. （第四個重點，如果有的話）
5. （第五個重點，如果有的話）

💡 金句 / 精彩觀點
- （值得記住的觀點或金句）
- （如果有第二個）

📝 行動建議
（基於這集內容，給聽眾 1-2 個可以實際執行的建議）

---
注意事項：
- 重點控制在 3-5 個，不要太多
- 每個重點要具體，不要太抽象
- 金句如果沒有就省略這個區塊
- 整體控制在 300-500 字以內
- 格式要清晰，適合在手機上快速瀏覽
"""


# ============================================================
# 工具函式
# ============================================================

def log(msg):
    """帶時間戳的日誌"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def load_processed():
    """讀取已處理的集數 ID"""
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, "r") as f:
            return json.load(f)
    return []


def save_processed(processed_list):
    """儲存已處理的集數 ID"""
    with open(PROCESSED_FILE, "w") as f:
        json.dump(processed_list, f, indent=2)


def get_episode_id(entry):
    """產生集數的唯一 ID（用 guid 或 title+date 的 hash）"""
    if hasattr(entry, "id") and entry.id:
        return entry.id
    raw = f"{entry.get('title', '')}-{entry.get('published', '')}".encode()
    return hashlib.md5(raw).hexdigest()


# ============================================================
# 步驟 1：檢查 RSS 新集數
# ============================================================

def check_new_episodes(force=False):
    """檢查 RSS Feed 是否有新集數"""
    log(f"正在檢查 RSS Feed: {RSS_URL}")
    feed = feedparser.parse(RSS_URL)

    if feed.bozo and not feed.entries:
        log(f"❌ RSS 解析錯誤: {feed.bozo_exception}")
        return []

    log(f"找到 {len(feed.entries)} 集")

    processed = load_processed()
    new_episodes = []

    for entry in feed.entries:
        ep_id = get_episode_id(entry)

        # 跳過已處理的
        if ep_id in processed and not force:
            continue

        # 檢查發布時間
        if hasattr(entry, "published_parsed") and entry.published_parsed and not force:
            pub_time = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=CHECK_HOURS)
            if pub_time < cutoff:
                continue

        # 取得音檔 URL
        audio_url = None
        if hasattr(entry, "enclosures") and entry.enclosures:
            for enc in entry.enclosures:
                if enc.get("type", "").startswith("audio") or enc.get("href", "").endswith((".mp3", ".m4a", ".wav")):
                    audio_url = enc.get("href") or enc.get("url")
                    break

        if not audio_url:
            # 有些 RSS 用 link 直接指向音檔
            links = entry.get("links", [])
            for link in links:
                if link.get("type", "").startswith("audio"):
                    audio_url = link.get("href")
                    break

        if audio_url:
            new_episodes.append({
                "id": ep_id,
                "title": entry.get("title", "未知標題"),
                "published": entry.get("published", "未知日期"),
                "audio_url": audio_url,
                "link": entry.get("link", ""),
                "description": entry.get("summary", "")[:500],
            })
            log(f"  🆕 新集數: {entry.get('title', '未知')}")
        else:
            log(f"  ⚠️ 找不到音檔連結: {entry.get('title', '未知')}")

    if force and not new_episodes and feed.entries:
        # --force 模式：取最新一集
        entry = feed.entries[0]
        audio_url = None
        if hasattr(entry, "enclosures") and entry.enclosures:
            for enc in entry.enclosures:
                if enc.get("type", "").startswith("audio") or enc.get("href", "").endswith((".mp3", ".m4a", ".wav")):
                    audio_url = enc.get("href") or enc.get("url")
                    break
        if audio_url:
            new_episodes.append({
                "id": get_episode_id(entry),
                "title": entry.get("title", "未知標題"),
                "published": entry.get("published", "未知日期"),
                "audio_url": audio_url,
                "link": entry.get("link", ""),
                "description": entry.get("summary", "")[:500],
            })
            log(f"  🔄 [強制模式] 處理最新集數: {entry.get('title', '未知')}")

    log(f"共 {len(new_episodes)} 集新集數需要處理")
    return new_episodes


# ============================================================
# 步驟 2：下載音檔
# ============================================================

def download_audio(audio_url, output_path):
    """下載 Podcast 音檔"""
    log(f"正在下載音檔...")
    log(f"  URL: {audio_url[:100]}...")

    response = requests.get(audio_url, stream=True, timeout=300)
    response.raise_for_status()

    total_size = int(response.headers.get("content-length", 0))
    downloaded = 0

    with open(output_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
            downloaded += len(chunk)

    file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
    log(f"  ✅ 下載完成: {file_size_mb:.1f} MB")
    return output_path


# ============================================================
# 步驟 3：Whisper 語音轉文字
# ============================================================

def transcribe_audio(audio_path):
    """使用 OpenAI Whisper API 轉錄音檔"""
    log("正在使用 Whisper 轉錄...")

    client = OpenAI(api_key=OPENAI_API_KEY)
    file_size_mb = os.path.getsize(audio_path) / (1024 * 1024)

    # OpenAI Whisper API 限制 25MB
    if file_size_mb > 25:
        log(f"  ⚠️ 檔案 {file_size_mb:.1f}MB 超過 25MB 限制，嘗試壓縮...")
        compressed_path = audio_path + ".compressed.mp3"
        os.system(f'ffmpeg -i "{audio_path}" -b:a 48k -ac 1 -ar 16000 "{compressed_path}" -y 2>/dev/null')
        if os.path.exists(compressed_path):
            new_size = os.path.getsize(compressed_path) / (1024 * 1024)
            log(f"  壓縮後: {new_size:.1f} MB")
            audio_path = compressed_path
        else:
            log("  ❌ 壓縮失敗，嘗試用原檔上傳")

    # 如果壓縮後還是太大，分段處理
    if os.path.getsize(audio_path) / (1024 * 1024) > 25:
        return transcribe_audio_chunked(audio_path, client)

    with open(audio_path, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language=PODCAST_LANG,
            response_format="text",
        )

    word_count = len(transcript)
    log(f"  ✅ 轉錄完成: {word_count} 字")
    return transcript


def transcribe_audio_chunked(audio_path, client):
    """分段轉錄大檔案"""
    log("  使用分段轉錄模式...")
    import subprocess

    # 取得音檔長度
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        capture_output=True, text=True
    )
    total_duration = float(result.stdout.strip())
    chunk_duration = 600  # 10 分鐘一段
    chunks = []

    with tempfile.TemporaryDirectory() as tmpdir:
        start = 0
        chunk_idx = 0
        while start < total_duration:
            chunk_path = os.path.join(tmpdir, f"chunk_{chunk_idx:03d}.mp3")
            os.system(
                f'ffmpeg -i "{audio_path}" -ss {start} -t {chunk_duration} '
                f'-b:a 48k -ac 1 -ar 16000 "{chunk_path}" -y 2>/dev/null'
            )
            if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
                chunks.append(chunk_path)
            start += chunk_duration
            chunk_idx += 1

        log(f"  分成 {len(chunks)} 段處理")

        full_transcript = []
        for i, chunk_path in enumerate(chunks):
            log(f"  轉錄第 {i+1}/{len(chunks)} 段...")
            with open(chunk_path, "rb") as f:
                text = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    language=PODCAST_LANG,
                    response_format="text",
                )
            full_transcript.append(text)
            time.sleep(1)  # 避免 rate limit

    transcript = "\n\n".join(full_transcript)
    log(f"  ✅ 分段轉錄完成: {len(transcript)} 字")
    return transcript


# ============================================================
# 步驟 4：AI 生成摘要
# ============================================================

def generate_summary(transcript, episode_title):
    """使用 GPT 生成摘要"""
    log(f"正在生成摘要（模型: {SUMMARY_MODEL}）...")

    client = OpenAI(api_key=OPENAI_API_KEY)

    # 如果逐字稿太長，先截斷（GPT-4o 支援 128K tokens，但成本考量截斷）
    max_chars = 60000  # 約 20K tokens
    if len(transcript) > max_chars:
        log(f"  ⚠️ 逐字稿 {len(transcript)} 字，截斷至 {max_chars} 字")
        # 保留開頭和結尾
        head = transcript[:max_chars // 2]
        tail = transcript[-(max_chars // 2):]
        transcript = head + "\n\n[...中間部分省略...]\n\n" + tail

    response = client.chat.completions.create(
        model=SUMMARY_MODEL,
        messages=[
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": f"節目標題：{episode_title}\n\n以下是逐字稿：\n\n{transcript}"},
        ],
        temperature=0.3,
        max_tokens=2000,
    )

    summary = response.choices[0].message.content
    log(f"  ✅ 摘要生成完成: {len(summary)} 字")
    return summary


# ============================================================
# 步驟 5a：寄 Email
# ============================================================

def send_email(episode, summary):
    """透過 Gmail SMTP 寄送摘要"""
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT]):
        log("  ⚠️ Email 設定不完整，跳過")
        return False

    log("正在寄送 Email...")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎙️ Podcast 更新：{episode['title']}"
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECIPIENT

    # 純文字版
    text_body = f"""
Podcast 新集數摘要
{'='*40}

📻 節目：{episode['title']}
📅 發佈：{episode['published']}
🔗 連結：{episode['link']}

{'─'*40}

{summary}

{'─'*40}
🤖 由 GitHub Actions 自動生成
    """.strip()

    # HTML 版（更好的排版）
    html_body = f"""
    <html>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #333;">
        <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 20px; border-radius: 12px; color: white; margin-bottom: 20px;">
            <h2 style="margin: 0;">🎙️ Podcast 新集數</h2>
            <p style="margin: 8px 0 0 0; opacity: 0.9;">{episode['title']}</p>
        </div>

        <div style="background: #f8f9fa; padding: 12px 16px; border-radius: 8px; margin-bottom: 20px; font-size: 14px;">
            <p style="margin: 4px 0;">📅 {episode['published']}</p>
            <p style="margin: 4px 0;">🔗 <a href="{episode['link']}" style="color: #667eea;">收聽連結</a></p>
        </div>

        <div style="line-height: 1.8; white-space: pre-wrap;">{summary}</div>

        <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">
        <p style="font-size: 12px; color: #999; text-align: center;">
            🤖 由 GitHub Actions 自動生成 | {datetime.now().strftime('%Y-%m-%d %H:%M')}
        </p>
    </body>
    </html>
    """

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        log("  ✅ Email 寄送成功")
        return True
    except Exception as e:
        log(f"  ❌ Email 寄送失敗: {e}")
        return False


# ============================================================
# 步驟 5b：發送 Telegram
# ============================================================

def send_telegram(episode, summary):
    """透過 Telegram Bot 發送摘要"""
    if not all([TG_BOT_TOKEN, TG_CHAT_ID]):
        log("  ⚠️ Telegram 設定不完整，跳過")
        return False

    log("正在發送 Telegram...")

    # Telegram 訊息長度限制 4096 字元
    header = (
        f"🎙️ *Podcast 更新*\n\n"
        f"📻 *{escape_markdown(episode['title'])}*\n"
        f"📅 {escape_markdown(episode['published'])}\n"
        f"━━━━━━━━━━━━━━━━\n\n"
    )
    footer = (
        f"\n\n━━━━━━━━━━━━━━━━\n"
        f"🔗 [收聽連結]({episode['link']})\n"
        f"🤖 _GitHub Actions 自動生成_"
    )

    # 摘要可能太長，需要截斷
    max_summary_len = 4096 - len(header) - len(footer) - 100
    summary_text = escape_markdown(summary)
    if len(summary_text) > max_summary_len:
        summary_text = summary_text[:max_summary_len] + "\n\n_(摘要已截斷)_"

    message = header + summary_text + footer

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=30)
        if resp.status_code == 200:
            log("  ✅ Telegram 發送成功")
            return True
        else:
            log(f"  ❌ Telegram 發送失敗: {resp.status_code} - {resp.text}")
            # 如果 Markdown 解析失敗，嘗試純文字
            payload["parse_mode"] = None
            payload["text"] = f"🎙️ Podcast 更新\n\n📻 {episode['title']}\n📅 {episode['published']}\n\n{summary}\n\n🔗 {episode['link']}"
            resp2 = requests.post(url, json=payload, timeout=30)
            if resp2.status_code == 200:
                log("  ✅ Telegram 發送成功（純文字模式）")
                return True
            return False
    except Exception as e:
        log(f"  ❌ Telegram 發送失敗: {e}")
        return False


def escape_markdown(text):
    """簡易 Markdown 轉義（Telegram MarkdownV1）"""
    # 只轉義可能造成問題的字元，保留我們自己的格式
    for char in ["_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"]:
        text = text.replace(char, f"\\{char}")
    return text


# ============================================================
# 主流程
# ============================================================

def process_episode(episode):
    """處理單一集數的完整流程"""
    log(f"\n{'='*60}")
    log(f"處理集數: {episode['title']}")
    log(f"{'='*60}")

    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. 下載音檔
        audio_ext = ".mp3"
        if ".m4a" in episode["audio_url"]:
            audio_ext = ".m4a"
        audio_path = os.path.join(tmpdir, f"episode{audio_ext}")
        download_audio(episode["audio_url"], audio_path)

        # 2. 轉錄
        transcript = transcribe_audio(audio_path)

        # 3. 生成摘要
        summary = generate_summary(transcript, episode["title"])

    # 4. 推送通知
    email_ok = send_email(episode, summary)
    tg_ok = send_telegram(episode, summary)

    if not email_ok and not tg_ok:
        log("⚠️ Email 和 Telegram 都未成功發送！")

    # 5. 記錄已處理
    processed = load_processed()
    processed.append(episode["id"])
    # 只保留最近 100 筆
    if len(processed) > 100:
        processed = processed[-100:]
    save_processed(processed)

    log(f"✅ 集數處理完成: {episode['title']}\n")
    return summary


def test_notifications():
    """測試 Email + Telegram 通知"""
    log("🧪 測試通知功能...")

    test_episode = {
        "title": "測試通知 - Podcast Auto Summary",
        "published": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "link": "https://github.com",
        "description": "這是一則測試通知",
    }

    test_summary = """📌 一句話總結
這是一則測試通知，確認 Email 和 Telegram 推送功能正常運作。

🔑 核心重點
1. Email 通知功能測試
2. Telegram Bot 推送測試
3. 格式渲染測試

💡 金句
- 自動化是生產力的最佳夥伴

📝 行動建議
如果你收到這則通知，代表設定成功！"""

    email_ok = send_email(test_episode, test_summary)
    tg_ok = send_telegram(test_episode, test_summary)

    if email_ok:
        log("✅ Email 測試通過")
    if tg_ok:
        log("✅ Telegram 測試通過")

    return email_ok or tg_ok


def main():
    parser = argparse.ArgumentParser(description="Podcast Auto Monitor + Transcribe + Summarize")
    parser.add_argument("--force", action="store_true", help="強制處理最新一集（跳過時間檢查）")
    parser.add_argument("--test-notify", action="store_true", help="只測試通知功能")
    args = parser.parse_args()

    log("🎙️ Podcast Auto Summary 啟動")
    log(f"  RSS URL: {RSS_URL[:60]}..." if RSS_URL else "  ⚠️ 未設定 RSS_URL")

    # 驗證必要設定
    if args.test_notify:
        success = test_notifications()
        sys.exit(0 if success else 1)

    if not RSS_URL:
        log("❌ 請設定 PODCAST_RSS_URL 環境變數")
        sys.exit(1)

    if not OPENAI_API_KEY:
        log("❌ 請設定 OPENAI_API_KEY 環境變數")
        sys.exit(1)

    # 檢查新集數
    new_episodes = check_new_episodes(force=args.force)

    if not new_episodes:
        log("沒有新集數，結束。")
        sys.exit(0)

    # 處理每一集
    for episode in new_episodes:
        try:
            process_episode(episode)
        except Exception as e:
            log(f"❌ 處理失敗: {episode['title']}")
            log(f"  錯誤: {e}")
            import traceback
            traceback.print_exc()

    log("🏁 所有新集數處理完成！")


if __name__ == "__main__":
    main()
