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

SUMMARY_PROMPT = """你是一位專業的台股美股投資分析速記員，專門為「股癌 Gooaye」Podcast 做超詳細的逐點記錄。
這個節目由謝孟恭（主委）主持，內容涵蓋台股、美股、ETF、總經、產業趨勢、個股分析等。
節目前面通常會有廣告業配，請忽略廣告內容，只記錄投資相關的部分。

⚠️ 最重要的規則：你必須把主委說的每一個投資觀點、每一檔提到的股票、每一個分析邏輯，全部逐條記錄下來。
不要歸納、不要合併、不要省略。把逐字稿中所有跟投資有關的資訊，一條一條地列出來。
你的輸出長度沒有上限，請盡可能詳細。目標至少 2000 字以上。

請根據逐字稿，用繁體中文產出以下記錄：

📌 本集主題（一句話，30字內）

📊 市場觀點 & 總經趨勢（一字不漏，全部記錄）
把主委提到的每一句跟市場、總經有關的話都記錄下來，不要歸納，逐條列出：
- 對大盤的看法（台股加權指數、美股三大指數的走勢判斷，他說了什麼就記什麼）
- 提到的每一個總經數據（CPI、PPI、非農就業、PMI、GDP、失業率、零售銷售等）以及他對這些數據的完整解讀
- 聯準會相關（利率決議、點陣圖、鴿派/鷹派言論、降息/升息預期、縮表、QT），他怎麼說就怎麼記
- 美元指數、台幣匯率、日圓、人民幣的走勢及他認為的影響
- 公債殖利率（2年期、10年期）、殖利率曲線倒掛/正常化
- 資金流向（外資買賣超金額、融資融券變化、法人動態、散戶開戶數）
- 國際政治經濟事件（中美關係、關稅、地緣政治、俄烏、中東、選舉等）
- 原物料（石油、黃金、銅、天然氣等）價格走勢及他的看法
- 台股和美股的相對強弱比較
- 他對目前市場情緒（恐懼/貪婪/觀望）的判斷
每一個點都要獨立列出，用他原本的分析邏輯和說法來記錄，3-5 句話說明。
他提到 15 個市場觀點就列 15 條，不要合併。

🏷️ 提到的所有個股 / 產業 / ETF（逐一列出，一個都不能漏）
把逐字稿中出現的 **每一檔** 股票、每一個產業、每一檔 ETF 都列出來：
- 股票名稱＋代號（台股附數字代號如 2330，美股附英文代號如 NVDA）
- 主委對它的態度（看多/看空/觀望/中性/純提及）
- 他提到這檔股票時說了什麼（完整記錄他的分析邏輯、提到的數據、本益比、營收、毛利率、市佔率等）
- 如果他有比較不同股票，把比較的內容也記下來

即使主委只是隨口提到一個股票名字，也要列出來標註「純提及」。
如果提到 20 檔股票，就列 20 條。不要合併、不要省略。

🔑 核心觀點與分析（逐條記錄，這是最重要的部分）
把主委說的 **每一個** 觀點都獨立列出來，包括：
- 對特定產業鏈的分析（上游、中游、下游各自的狀況）
- 對個別公司的深入分析（商業模式、競爭優勢、護城河）
- 市場走勢的判斷邏輯（為什麼他認為會漲/會跌/會盤整）
- 投資策略建議（何時進場、何時出場、停損停利、部位配置）
- 對新聞事件的解讀（他怎麼看這個新聞、對市場有什麼影響）
- 對其他 KOL/分析師/媒體/法人報告的評論
- 對散戶行為的觀察和提醒
- 產業趨勢的長期看法（AI、半導體、電動車、能源等）
- 任何類比、故事、歷史案例
- 投資心態和哲學的分享

每個觀點用 3-5 句話完整記錄，保留主委的分析脈絡和邏輯推演。
有 10 個觀點就列 10 條，有 20 個就列 20 條。絕對不要合併或省略。

⚠️ 風險提醒 & 警示
- 主委提到的每一個風險因素
- 他提醒聽眾要小心的事情
- 他看空或建議避開的標的，以及完整原因
- 他對市場過熱、泡沫、追高的警告

💡 金句 / 精彩觀點（5句以上）
- 把主委說的有記憶點的話都記下來
- 包括投資哲學、人生觀、對市場的幽默評論

📅 時事關聯
- 這集內容跟近期哪些新聞事件有關
- 主委認為這些事件未來會如何發展

絕對規則：
- 完全忽略廣告、業配、商品推薦、個人生活閒聊
- 股票一定要附上代號
- 區分「主委自己的觀點」和「他引用/轉述他人的觀點」
- 每一個投資相關的觀點都要記錄，零遺漏
- 輸出長度不設限，2000-4000 字都可以，重點是完整
- 這不是投資建議，僅為節目內容記錄"""


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
        max_tokens=16000,
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
    recipients = [r.strip() for r in EMAIL_RECIPIENT.split(",")]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎙️ 股癌摘要：{episode['title']}"
    msg["From"] = EMAIL_SENDER
    msg["To"] = ", ".join(recipients)

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
