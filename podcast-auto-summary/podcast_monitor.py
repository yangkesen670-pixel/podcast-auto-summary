#!/usr/bin/env python3
"""
Podcast Auto Monitor + Transcribe + Summarize
=============================================
自動監控 Apple Podcast RSS → 下載音檔 → 壓縮 → Whisper 轉文字 → AI 摘要 → Email + Telegram 推送
"""

import os
import sys
import json
import time
import hashlib
import smtplib
import argparse
import tempfile
import subprocess
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from openai import OpenAI

# ============================================================
# 設定區
# ============================================================
RSS_URL = os.environ.get("PODCAST_RSS_URL", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_RECIPIENT = os.environ.get("EMAIL_RECIPIENT", "")
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "gpt-4o-mini")
CHECK_HOURS = int(os.environ.get("CHECK_HOURS", "24"))
PODCAST_LANG = os.environ.get("PODCAST_LANG", "zh")
PROCESSED_FILE = os.environ.get("PROCESSED_FILE", "processed_episodes.json")

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

📝 行動建議
（基於這集內容，給聽眾 1-2 個可以實際執行的建議）

注意：重點控制在 3-5 個，整體 300-500 字，適合手機快速瀏覽。"""


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def load_processed():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, "r") as f:
            return json.load(f)
    return []


def save_processed(processed_list):
    with open(PROCESSED_FILE, "w") as f:
        json.dump(processed_list, f, indent=2)


def get_episode_id(entry):
    if hasattr(entry, "id") and entry.id:
        return entry.id
    raw = f"{entry.get('title', '')}-{entry.get('published', '')}".encode()
    return hashlib.md5(raw).hexdigest()


# ============================================================
# 步驟 1：檢查 RSS 新集數
# ============================================================
def check_new_episodes(force=False):
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
        if ep_id in processed and not force:
            continue

        if hasattr(entry, "published_parsed") and entry.published_parsed and not force:
            pub_time = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=CHECK_HOURS)
            if pub_time < cutoff:
                continue

        audio_url = None
        if hasattr(entry, "enclosures") and entry.enclosures:
            for enc in entry.enclosures:
                if enc.get("type", "").startswith("audio") or enc.get("href", "").endswith((".mp3", ".m4a", ".wav")):
                    audio_url = enc.get("href") or enc.get("url")
                    break
        if not audio_url:
            for link in entry.get("links", []):
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

    if force and not new_episodes and feed.entries:
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
    log(f"正在下載音檔...")
    response = requests.get(audio_url, stream=True, timeout=300)
    response.raise_for_status()
    with open(output_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
    log(f"  ✅ 下載完成: {file_size_mb:.1f} MB")
    return output_path


# ============================================================
# 步驟 3：壓縮 + 分段 + Whisper 轉錄
# ============================================================
def compress_audio(input_path, output_path):
    """壓縮音檔到低 bitrate，大幅縮小檔案"""
    log("  正在壓縮音檔...")
    cmd = [
        "ffmpeg", "-i", input_path,
        "-b:a", "32k",      # 極低 bitrate（語音夠用）
        "-ac", "1",          # 單聲道
        "-ar", "16000",      # 16kHz（Whisper 最佳）
        "-map", "a",         # 只保留音訊
        output_path, "-y"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if os.path.exists(output_path):
        new_size = os.path.getsize(output_path) / (1024 * 1024)
        log(f"  ✅ 壓縮完成: {new_size:.1f} MB")
        return True
    log(f"  ❌ 壓縮失敗: {result.stderr[:200]}")
    return False


def split_audio(input_path, tmpdir, chunk_minutes=10):
    """把音檔切成多段"""
    log("  正在分段...")
    # 取得音檔總長度
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True
    )
    total_duration = float(result.stdout.strip())
    chunk_seconds = chunk_minutes * 60
    chunks = []
    start = 0
    idx = 0

    while start < total_duration:
        chunk_path = os.path.join(tmpdir, f"chunk_{idx:03d}.mp3")
        cmd = [
            "ffmpeg", "-i", input_path,
            "-ss", str(start), "-t", str(chunk_seconds),
            "-b:a", "32k", "-ac", "1", "-ar", "16000",
            chunk_path, "-y"
        ]
        subprocess.run(cmd, capture_output=True, text=True)
        if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
            chunks.append(chunk_path)
        start += chunk_seconds
        idx += 1

    log(f"  ✅ 分成 {len(chunks)} 段")
    return chunks


def transcribe_audio(audio_path):
    """下載後先壓縮，再用 Whisper API 轉錄"""
    log("正在轉錄...")
    client = OpenAI(api_key=OPENAI_API_KEY)

    with tempfile.TemporaryDirectory() as tmpdir:
        # 先壓縮
        compressed_path = os.path.join(tmpdir, "compressed.mp3")
        compress_ok = compress_audio(audio_path, compressed_path)

        if compress_ok:
            work_path = compressed_path
        else:
            work_path = audio_path

        file_size_mb = os.path.getsize(work_path) / (1024 * 1024)

        # 小於 25MB：直接上傳
        if file_size_mb <= 24:
            log(f"  檔案 {file_size_mb:.1f} MB，直接上傳")
            with open(work_path, "rb") as f:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    language=PODCAST_LANG,
                    response_format="text",
                )
            log(f"  ✅ 轉錄完成: {len(transcript)} 字")
            return transcript

        # 大於 25MB：分段上傳
        log(f"  檔案 {file_size_mb:.1f} MB，需要分段處理")
        chunks = split_audio(work_path, tmpdir)
        full_transcript = []

        for i, chunk_path in enumerate(chunks):
            chunk_size = os.path.getsize(chunk_path) / (1024 * 1024)
            log(f"  轉錄第 {i+1}/{len(chunks)} 段 ({chunk_size:.1f} MB)...")
            with open(chunk_path, "rb") as f:
                text = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    language=PODCAST_LANG,
                    response_format="text",
                )
            full_transcript.append(text)
            time.sleep(0.5)

        transcript = "\n\n".join(full_transcript)
        log(f"  ✅ 分段轉錄完成: {len(transcript)} 字")
        return transcript


# ============================================================
# 步驟 4：AI 生成摘要
# ============================================================
def generate_summary(transcript, episode_title):
    log(f"正在生成摘要（模型: {SUMMARY_MODEL}）...")
    client = OpenAI(api_key=OPENAI_API_KEY)

    max_chars = 60000
    if len(transcript) > max_chars:
        log(f"  ⚠️ 逐字稿 {len(transcript)} 字，截斷至 {max_chars} 字")
        head = transcript[:max_chars // 2]
        tail = transcript[-(max_chars // 2):]
        transcript = head + "\n\n[...中間部分省略...]\n\n" + tail

    response = client.chat.completions.create(
        model=SUMMARY_MODEL,
        messages=[
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": f"節目標題：{episode_title}\n\n逐字稿：\n\n{transcript}"},
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
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT]):
        log("  ⚠️ Email 設定不完整，跳過")
        return False

    log("正在寄送 Email...")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎙️ Podcast 更新：{episode['title']}"
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECIPIENT

    text_body = f"""Podcast 新集數摘要
{'='*40}
📻 節目：{episode['title']}
📅 發佈：{episode['published']}
🔗 連結：{episode['link']}
{'─'*40}

{summary}

{'─'*40}
🤖 由 GitHub Actions 自動生成"""

    html_body = f"""<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#333;">
<div style="background:linear-gradient(135deg,#667eea,#764ba2);padding:20px;border-radius:12px;color:white;">
<h2 style="margin:0;">🎙️ Podcast 新集數</h2>
<p style="margin:8px 0 0;opacity:0.9;">{episode['title']}</p></div>
<div style="background:#f8f9fa;padding:12px 16px;border-radius:8px;margin:20px 0;font-size:14px;">
<p style="margin:4px 0;">📅 {episode['published']}</p>
<p style="margin:4px 0;">🔗 <a href="{episode['link']}" style="color:#667eea;">收聽連結</a></p></div>
<div style="line-height:1.8;white-space:pre-wrap;">{summary}</div>
<hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
<p style="font-size:12px;color:#999;text-align:center;">🤖 GitHub Actions 自動生成</p>
</body></html>"""

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
    if not all([TG_BOT_TOKEN, TG_CHAT_ID]):
        log("  ⚠️ Telegram 設定不完整，跳過")
        return False

    log("正在發送 Telegram...")

    # 用純文字模式避免 Markdown 格式問題
    message = (
        f"🎙️ Podcast 更新\n\n"
        f"📻 {episode['title']}\n"
        f"📅 {episode['published']}\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"{summary}\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🔗 {episode['link']}"
    )

    # Telegram 限制 4096 字
    if len(message) > 4096:
        message = message[:4090] + "\n..."

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=30)
        if resp.status_code == 200:
            log("  ✅ Telegram 發送成功")
            return True
        else:
            log(f"  ❌ Telegram 發送失敗: {resp.status_code} - {resp.text[:200]}")
            return False
    except Exception as e:
        log(f"  ❌ Telegram 發送失敗: {e}")
        return False


# ============================================================
# 主流程
# ============================================================
def process_episode(episode):
    log(f"\n{'='*60}")
    log(f"處理集數: {episode['title']}")
    log(f"{'='*60}")

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_ext = ".m4a" if ".m4a" in episode["audio_url"] else ".mp3"
        audio_path = os.path.join(tmpdir, f"episode{audio_ext}")
        download_audio(episode["audio_url"], audio_path)
        transcript = transcribe_audio(audio_path)
        summary = generate_summary(transcript, episode["title"])

    send_email(episode, summary)
    send_telegram(episode, summary)

    processed = load_processed()
    processed.append(episode["id"])
    if len(processed) > 100:
        processed = processed[-100:]
    save_processed(processed)

    log(f"✅ 集數處理完成: {episode['title']}\n")
    return summary


def test_notifications():
    log("🧪 測試通知功能...")
    test_episode = {
        "title": "測試通知 - Podcast Auto Summary",
        "published": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "link": "https://github.com",
        "description": "這是一則測試通知",
    }
    test_summary = "📌 一句話總結\n這是一則測試通知，確認推送功能正常。\n\n🔑 核心重點\n1. Email 通知測試\n2. Telegram 推送測試\n\n📝 如果你收到這則通知，代表設定成功！"

    email_ok = send_email(test_episode, test_summary)
    tg_ok = send_telegram(test_episode, test_summary)
    return email_ok or tg_ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--test-notify", action="store_true")
    args = parser.parse_args()

    log("🎙️ Podcast Auto Summary 啟動")

    if args.test_notify:
        success = test_notifications()
        sys.exit(0 if success else 1)

    if not RSS_URL:
        log("❌ 請設定 PODCAST_RSS_URL")
        sys.exit(1)
    if not OPENAI_API_KEY:
        log("❌ 請設定 OPENAI_API_KEY")
        sys.exit(1)

    new_episodes = check_new_episodes(force=args.force)
    if not new_episodes:
        log("沒有新集數，結束。")
        sys.exit(0)

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
