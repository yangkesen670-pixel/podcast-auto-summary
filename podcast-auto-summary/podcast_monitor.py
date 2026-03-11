#!/usr/bin/env python3
"""
Podcast Auto Monitor + Transcribe + Summarize
=============================================
自動監控 Apple Podcast RSS → 下載音檔 → 壓縮 → Whisper 轉文字 → Claude 雙重稽核摘要 → Email + Telegram 推送
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
import anthropic

# ============================================================
# 設定區
# ============================================================
RSS_URL = os.environ.get("PODCAST_RSS_URL", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_RECIPIENT = os.environ.get("EMAIL_RECIPIENT", "")
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
CHECK_HOURS = int(os.environ.get("CHECK_HOURS", "24"))
PODCAST_LANG = os.environ.get("PODCAST_LANG", "zh")
PROCESSED_FILE = os.environ.get("PROCESSED_FILE", "processed_episodes.json")

# ============================================================
# 股癌專屬 System Prompt — Claude Sonnet 4.6
# ============================================================
EXTRACT_PROMPT = """你是一位資深的「科技產業分析師」與「美台股全球操盤手」。
請閱讀這份《股癌 Gooaye》的 Podcast 逐字稿，過濾掉開頭結尾的業配與生活閒聊，將核心的總經、產業鏈與個股觀點，萃取成高密度的投資報告。

【擷取原則】：
1. 挖出因果關係 (Know-Why)：不要只說「看好蘋果」，必須寫出「為什麼看好？」（例如：利用毛利優勢在記憶體漲價時搶佔市占率）。
2. 綁定供應鏈邏輯：只要主委把「美股巨頭（如 NVDA, AAPL）」與「台股代工/零組件廠」連在一起講，必須完整保留這個上下游的推演路徑。
3. 具體化財報與數據：將含糊的「財報不錯」，還原成逐字稿中的「具體指引、毛利率變化或市場預期的落差」。
4. 保留主委獨特比喻：主委常使用生動的「幹話或比喻」來解釋複雜的市場心理（如：FOMO、左巴右巴），請連同上下文情境一起保留，作為心法警語。

【必須輸出的結構報告】：

# 股癌 Gooaye｜產業與市場深度推演

## 一、宏觀經濟與大盤風向 (Macro & Sentiment)
*(記錄對美股輪動、通膨、降息預期或整體市場情緒的判斷，每個觀點都要附上主委的推演邏輯)*

## 二、核心產業鏈推演 (Supply Chain Logic) 🔗
*(最重要模塊：寫出主委看好的產業趨勢，以及美股龍頭如何帶動台股概念股，必須寫出完整的邏輯故事，從上游到下游的推演路徑)*

## 三、個股觀察與財報解析 (Stock Deep-Dive)
*(用表格呈現提及的公司：包含「公司代號」、「近期動態/財報表現」、「主委觀點(看多/看空/中立)」、「關鍵支撐邏輯」)*

| 公司代號 | 近期動態/財報表現 | 主委觀點 | 關鍵支撐邏輯 |
|---------|----------------|---------|-----------|

## 四、交易心法與避險警告 ⚠️
*(記錄主委對於「目前不要做什麼事」的警告，例如對槓桿、追高的看法，並附上具體情境)*

## 五、經典語錄與情境還原 💬
*(列出金句，並務必在一旁補充主委說這句話時的「市場情境」，避免斷章取義)*

【輸出要求】：
- 用繁體中文
- 盡可能詳細，目標 2000-4000 字
- 每個觀點都要有因果邏輯，不要只列結論
- 台股附數字代號（如 2330），美股附英文代號（如 NVDA）
- 不要遺漏任何投資相關的觀點"""

# ============================================================
# 稽核 Prompt — 第二輪檢查遺漏
# ============================================================
AUDIT_PROMPT = """你是一位嚴格的財經內容稽核員。
你的任務是比對「原始逐字稿」和「已萃取的報告」，找出報告中遺漏的重要資訊。

請逐一檢查以下項目：
1. 有沒有提到的公司/股票被遺漏了？（即使只是一筆帶過）
2. 有沒有總經數據或政策事件被遺漏了？
3. 有沒有主委的重要觀點或分析邏輯被簡化或遺漏了？
4. 有沒有供應鏈上下游的推演被截斷了？
5. 有沒有主委的經典比喻或幹話被遺漏了？
6. 有沒有風險警告或避險建議被遺漏了？

如果發現遺漏，請直接輸出補充內容，用以下格式：

## 📋 稽核補充

### 遺漏的公司/標的
- （列出被遺漏的）

### 遺漏的觀點/分析
- （列出被遺漏的，附上完整邏輯）

### 遺漏的金句/比喻
- （列出被遺漏的，附上情境）

如果沒有重大遺漏，請回覆「✅ 稽核通過，無重大遺漏」。
用繁體中文回覆。"""


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
# 步驟 1：檢查 RSS 新集數（強制模式只處理最新一集）
# ============================================================
def check_new_episodes(force=False):
    log(f"正在檢查 RSS Feed: {RSS_URL}")
    feed = feedparser.parse(RSS_URL)

    if feed.bozo and not feed.entries:
        log(f"❌ RSS 解析錯誤: {feed.bozo_exception}")
        return []

    log(f"找到 {len(feed.entries)} 集")

    if force:
        entry = feed.entries[0]
        audio_url = None
        if hasattr(entry, "enclosures") and entry.enclosures:
            for enc in entry.enclosures:
                if enc.get("type", "").startswith("audio") or enc.get("href", "").endswith((".mp3", ".m4a", ".wav")):
                    audio_url = enc.get("href") or enc.get("url")
                    break
        if audio_url:
            log(f"  🔄 [強制模式] 處理最新集數: {entry.get('title', '未知')}")
            return [{
                "id": get_episode_id(entry),
                "title": entry.get("title", "未知標題"),
                "published": entry.get("published", "未知日期"),
                "audio_url": audio_url,
                "link": entry.get("link", ""),
                "description": entry.get("summary", "")[:500],
            }]
        log("❌ 找不到音檔連結")
        return []

    processed = load_processed()
    new_episodes = []

    for entry in feed.entries:
        ep_id = get_episode_id(entry)
        if ep_id in processed:
            continue

        if hasattr(entry, "published_parsed") and entry.published_parsed:
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
    log("  正在壓縮音檔...")
    cmd = [
        "ffmpeg", "-i", input_path,
        "-b:a", "32k",
        "-ac", "1",
        "-ar", "16000",
        "-map", "a",
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
    log("  正在分段...")
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
    log("正在轉錄（Whisper）...")
    client = OpenAI(api_key=OPENAI_API_KEY)

    with tempfile.TemporaryDirectory() as tmpdir:
        compressed_path = os.path.join(tmpdir, "compressed.mp3")
        compress_ok = compress_audio(audio_path, compressed_path)

        if compress_ok:
            work_path = compressed_path
        else:
            work_path = audio_path

        file_size_mb = os.path.getsize(work_path) / (1024 * 1024)

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
# 步驟 4：Claude Sonnet 4.6 雙重稽核摘要
# ============================================================
def generate_summary(transcript, episode_title):
    log("正在生成摘要（Claude Sonnet 4.6 雙重稽核）...")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    max_chars = 100000
    if len(transcript) > max_chars:
        log(f"  ⚠️ 逐字稿 {len(transcript)} 字，截斷至 {max_chars} 字")
        head = transcript[:max_chars // 2]
        tail = transcript[-(max_chars // 2):]
        transcript = head + "\n\n[...中間部分省略...]\n\n" + tail

    # ========== 第一輪：萃取報告 ==========
    log("  📝 第一輪：萃取報告...")
    extract_response = client.messages.create(
        model="claude-sonnet-4-6-20250514",
        max_tokens=8000,
        system=EXTRACT_PROMPT,
        messages=[
            {"role": "user", "content": f"節目標題：{episode_title}\n\n以下是完整逐字稿，請仔細閱讀後產出詳細的結構化報告，不要遺漏任何投資相關的觀點：\n\n{transcript}"}
        ],
    )
    report = extract_response.content[0].text
    log(f"  ✅ 第一輪完成: {len(report)} 字")

    # ========== 第二輪：稽核補漏 ==========
    log("  🔍 第二輪：稽核補漏...")
    audit_response = client.messages.create(
        model="claude-sonnet-4-6-20250514",
        max_tokens=4000,
        system=AUDIT_PROMPT,
        messages=[
            {"role": "user", "content": f"【原始逐字稿】：\n{transcript}\n\n---\n\n【已萃取的報告】：\n{report}\n\n請比對以上兩者，找出報告中遺漏的重要資訊。"}
        ],
    )
    audit_result = audit_response.content[0].text
    log(f"  ✅ 第二輪完成: {len(audit_result)} 字")

    # ========== 合併結果 ==========
    if "稽核通過" in audit_result:
        final_report = report + "\n\n---\n✅ 雙重稽核通過，無重大遺漏"
    else:
        final_report = report + "\n\n---\n" + audit_result

    log(f"  ✅ 最終報告: {len(final_report)} 字")
    return final_report


# ============================================================
# 步驟 5a：寄 Email
# ============================================================
def send_email(episode, summary):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT]):
        log("  ⚠️ Email 設定不完整，跳過")
        return False

    log("正在寄送 Email...")
    recipients = [r.strip() for r in EMAIL_RECIPIENT.split(",")]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎙️ 股癌深度推演：{episode['title']}"
    msg["From"] = EMAIL_SENDER
    msg["To"] = ", ".join(recipients)

    text_body = f"""股癌 Gooaye｜產業與市場深度推演
{'='*50}
📻 {episode['title']}
📅 {episode['published']}
🔗 {episode['link']}
🤖 Claude Sonnet 4.6 雙重稽核萃取
{'─'*50}

{summary}

{'─'*50}
🤖 GitHub Actions 自動生成 | 僅為節目內容記錄，非投資建議"""

    html_summary = summary.replace('\n', '<br>')
    html_body = f"""<html><body style="font-family:'Microsoft JhengHei',sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#333;">
<div style="background:linear-gradient(135deg,#0f0c29,#302b63,#24243e);padding:24px;border-radius:12px;color:white;">
<h2 style="margin:0;">🎙️ 股癌 Gooaye｜深度推演</h2>
<p style="margin:8px 0 0;opacity:0.9;">{episode['title']}</p>
<p style="margin:4px 0 0;opacity:0.7;font-size:13px;">Claude Sonnet 4.6 雙重稽核萃取</p></div>
<div style="background:#f8f9fa;padding:12px 16px;border-radius:8px;margin:20px 0;font-size:14px;">
<p style="margin:4px 0;">📅 {episode['published']}</p>
<p style="margin:4px 0;">🔗 <a href="{episode['link']}" style="color:#4a90d9;">收聽連結</a></p></div>
<div style="line-height:1.9;font-size:15px;">{html_summary}</div>
<hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
<p style="font-size:11px;color:#999;text-align:center;">🤖 GitHub Actions + Claude Sonnet 4.6 自動生成 | ⚠️ 僅為節目內容記錄，非投資建議</p>
</body></html>"""

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, recipients, msg.as_string())
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

    header = (
        f"🎙️ 股癌 Gooaye｜深度推演\n\n"
        f"📻 {episode['title']}\n"
        f"📅 {episode['published']}\n"
        f"🤖 Claude Sonnet 4.6 雙重稽核\n"
        f"━━━━━━━━━━━━━━━━\n\n"
    )
    footer = (
        f"\n\n━━━━━━━━━━━━━━━━\n"
        f"🔗 {episode['link']}\n"
        f"⚠️ 僅為節目內容記錄，非投資建議"
    )

    message = header + summary + footer
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

    if len(message) <= 4096:
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
                log(f"  ❌ Telegram 發送失敗: {resp.status_code}")
                return False
        except Exception as e:
            log(f"  ❌ Telegram 發送失敗: {e}")
            return False
    else:
        log("  摘要較長，分段發送 Telegram...")
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": header.strip(), "disable_web_page_preview": True}, timeout=30)
        time.sleep(0.5)

        chunk_size = 4000
        for i in range(0, len(summary), chunk_size):
            chunk = summary[i:i+chunk_size]
            requests.post(url, json={"chat_id": TG_CHAT_ID, "text": chunk, "disable_web_page_preview": True}, timeout=30)
            time.sleep(0.5)

        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": footer.strip(), "disable_web_page_preview": True}, timeout=30)
        log("  ✅ Telegram 分段發送成功")
        return True


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
        "title": "測試通知 - 股癌深度推演系統",
        "published": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "link": "https://github.com",
        "description": "這是一則測試通知",
    }
    test_summary = "# 股癌 Gooaye｜產業與市場深度推演\n\n## 一、宏觀經濟與大盤風向\n- 測試中\n\n## 二、核心產業鏈推演\n- 測試中\n\n## 三、個股觀察\n- 無（測試用）\n\n✅ 如果你收到這則通知，代表 Claude Sonnet 4.6 雙重稽核系統設定成功！"

    email_ok = send_email(test_episode, test_summary)
    tg_ok = send_telegram(test_episode, test_summary)
    return email_ok or tg_ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--test-notify", action="store_true")
    args = parser.parse_args()

    log("🎙️ 股癌深度推演系統啟動（Claude Sonnet 4.6 雙重稽核）")

    if args.test_notify:
        success = test_notifications()
        sys.exit(0 if success else 1)

    if not RSS_URL:
        log("❌ 請設定 PODCAST_RSS_URL")
        sys.exit(1)
    if not OPENAI_API_KEY:
        log("❌ 請設定 OPENAI_API_KEY（Whisper 轉錄用）")
        sys.exit(1)
    if not ANTHROPIC_API_KEY:
        log("❌ 請設定 ANTHROPIC_API_KEY（Claude 摘要用）")
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
