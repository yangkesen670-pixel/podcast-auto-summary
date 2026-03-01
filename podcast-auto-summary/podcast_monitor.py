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

SUMMARY_PROMPT = """你是一位專業的台股美股投資分析助手，專門為「股癌 Gooaye」Podcast 做詳細摘要。
這個節目由謝孟恭（主委）主持，內容涵蓋台股、美股、ETF、總經、產業趨勢、個股分析等。
節目前面通常會有廣告業配，請忽略廣告內容，只摘要投資相關的部分。

你的目標是產出一份 **極其詳細、完整** 的摘要，讓沒聽節目的人也能掌握所有重點。
摘要長度目標：**1000～2000 字**，寧可太長也絕對不要太短。

請根據逐字稿，用繁體中文產出以下摘要：

📌 本集主題（一句話，30字內）

📊 市場觀點 & 總經趨勢（請詳細展開，每個點用 2-3 句話說明）
- 主委對目前大盤/市場的整體看法和判斷邏輯
- 提到的重要總經數據、政策、國際事件，以及他的解讀
- 聯準會、利率、通膨、GDP、就業數據等相關資訊（如果有提到）
- 美元、匯率、債券、原物料等相關走勢（如果有提到）
- 台股和美股的相對強弱和資金流向（如果有提到）

🏷️ 提到的所有個股 / 產業 / ETF（全部列出，一個都不能少）
針對每一個被提到的標的，列出：
- 股票名稱＋代號（如：台積電 2330、NVIDIA NVDA、0050 等）
- 主委對它的看法（看多/看空/觀望/純提及）
- 詳細說明原因或邏輯（2-3句話，包括他提到的數據、估值、產業地位等）
即使只是一筆帶過的股票也要列出來。如果提到 10 檔以上，全部都要列。

🔑 核心觀點與分析（這是最重要的部分，請鉅細靡遺地列出）
把主委在這集中提到的 **所有** 重要觀點、分析、邏輯都列出來，包括但不限於：
- 對特定產業鏈的完整分析（上游、中游、下游）
- 對市場走勢的判斷和他的理由
- 投資策略或操作邏輯（例如：何時買、何時賣、部位大小）
- 對新聞事件的深度解讀
- 對其他分析師/媒體/YouTuber 觀點的評論或反駁
- 對散戶常見錯誤的提醒
- 對產業趨勢的長期看法
- 任何有趣的類比、故事或案例分析
每個觀點用 2-3 句話詳細說明，有幾個就寫幾個，**絕對不要省略或合併**。

⚠️ 風險提醒 & 警示（詳細列出）
- 主委提到的所有風險因素，以及他認為風險有多大
- 他提醒聽眾注意的事項
- 他看空或建議避開的標的，以及原因
- 他認為市場目前存在的泡沫或過熱現象

💡 金句 / 精彩觀點（3-5句）
- 主委說的讓人印象深刻的話
- 有啟發性的投資哲學或心態建議

📅 時事關聯
- 這集內容跟近期哪些新聞事件有關
- 這些事件對投資人的實際影響

注意事項：
- 重點放在「投資相關內容」，完全忽略廣告、業配、商品推薦
- 股票請盡量附上代號（台股附數字代號，美股附英文代號）
- 區分「主委自己的觀點」和「他引用/轉述他人的觀點」
- **不要省略任何投資相關的觀點，寧可多寫也絕對不要漏掉**
- **摘要目標 1000-2000 字，每個區塊都要有實質內容，不要只寫一兩行就帶過**
- 這不是投資建議，僅為節目內容摘要"""


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
        # 強制模式：只處理最新一集
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

    # 一般模式：只處理 24 小時內的新集數
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
    log("正在轉錄...")
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
# 步驟 4：AI 生成摘要（用更大的 token 上限）
# ============================================================
def generate_summary(transcript, episode_title):
    log(f"正在生成摘要（模型: {SUMMARY_MODEL}）...")
    client = OpenAI(api_key=OPENAI_API_KEY)

    max_chars = 80000
    if len(transcript) > max_chars:
        log(f"  ⚠️ 逐字稿 {len(transcript)} 字，截斷至 {max_chars} 字")
        head = transcript[:max_chars // 2]
        tail = transcript[-(max_chars // 2):]
        transcript = head + "\n\n[...中間部分省略...]\n\n" + tail

    response = client.chat.completions.create(
        model=SUMMARY_MODEL,
        messages=[
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": f"節目標題：{episode_title}\n\n以下是完整逐字稿，請仔細閱讀後產出詳細摘要，不要遺漏任何投資相關的觀點：\n\n{transcript}"},
        ],
        temperature=0.2,
        max_tokens=8000,
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
    msg["Subject"] = f"🎙️ 股癌摘要：{episode['title']}"
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECIPIENT

    text_body = f"""股癌 Podcast 摘要
{'='*40}
📻 {episode['title']}
📅 {episode['published']}
🔗 {episode['link']}
{'─'*40}

{summary}

{'─'*40}
🤖 GitHub Actions 自動生成 | 僅為節目摘要，非投資建議"""

    html_body = f"""<html><body style="font-family:sans-serif;max-width:650px;margin:0 auto;padding:20px;color:#333;">
<div style="background:linear-gradient(135deg,#1a1a2e,#16213e);padding:20px;border-radius:12px;color:white;">
<h2 style="margin:0;">🎙️ 股癌 Podcast 摘要</h2>
<p style="margin:8px 0 0;opacity:0.9;">{episode['title']}</p></div>
<div style="background:#f8f9fa;padding:12px 16px;border-radius:8px;margin:20px 0;font-size:14px;">
<p style="margin:4px 0;">📅 {episode['published']}</p>
<p style="margin:4px 0;">🔗 <a href="{episode['link']}" style="color:#4a90d9;">收聽連結</a></p></div>
<div style="line-height:1.9;white-space:pre-wrap;font-size:15px;">{summary}</div>
<hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
<p style="font-size:11px;color:#999;text-align:center;">🤖 GitHub Actions 自動生成 | ⚠️ 僅為節目內容摘要，非投資建議</p>
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

    header = (
        f"🎙️ 股癌 Podcast 摘要\n\n"
        f"📻 {episode['title']}\n"
        f"📅 {episode['published']}\n"
        f"━━━━━━━━━━━━━━━━\n\n"
    )
    footer = (
        f"\n\n━━━━━━━━━━━━━━━━\n"
        f"🔗 {episode['link']}\n"
        f"⚠️ 僅為節目摘要，非投資建議"
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
        "title": "測試通知 - 股癌 Podcast 摘要",
        "published": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "link": "https://github.com",
        "description": "這是一則測試通知",
    }
    test_summary = "📌 本集主題\n這是一則測試通知，確認推送功能正常。\n\n📊 市場觀點\n- 測試中\n\n🏷️ 提到的個股\n- 無（測試用）\n\n🔑 核心觀點\n1. Email 通知測試\n2. Telegram 推送測試\n\n📝 如果你收到這則通知，代表設定成功！"

    email_ok = send_email(test_episode, test_summary)
    tg_ok = send_telegram(test_episode, test_summary)
    return email_ok or tg_ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--test-notify", action="store_true")
    args = parser.parse_args()

    log("🎙️ 股癌 Podcast Auto Summary 啟動")

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
