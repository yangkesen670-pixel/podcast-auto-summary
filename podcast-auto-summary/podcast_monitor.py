#!/usr/bin/env python3
"""
股癌深度推演系統 — Claude Sonnet 4.6 雙重稽核
=============================================
RSS 監控 → 下載音檔 → Whisper 轉文字 → Claude 雙重稽核萃取 → Email(.txt/.md附件) + Telegram
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
import re
import feedparser
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
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
# 股癌專屬 System Prompt — Claude Sonnet 4.6（EP643 深度格式 + 數字鐵律）
# ============================================================
EXTRACT_PROMPT = """你是一位資深的「科技產業分析師」與「美台股全球操盤手」。
請閱讀這份《股癌 Gooaye》的 Podcast 逐字稿，過濾掉開頭結尾的業配與生活閒聊，將核心的總經、產業鏈與個股觀點，萃取成高密度的投資報告。

【擷取原則】：
1. 挖出因果關係 (Know-Why)：不要只說「看好蘋果」，必須寫出「為什麼看好？」（例如：利用毛利優勢在記憶體漲價時搶佔市占率）。
2. 綁定供應鏈邏輯：只要主委把「美股巨頭（如 NVDA, AAPL）」與「台股代工/零組件廠」連在一起講，必須完整保留這個上下游的推演路徑。
3. 具體化財報與數據：將含糊的「財報不錯」，還原成逐字稿中的「具體指引、毛利率變化或市場預期的落差」。
4. 保留主委獨特比喻：主委常使用生動的「幹話或比喻」來解釋複雜的市場心理（如：FOMO、左巴右巴），請連同上下文情境一起保留，作為心法警語。
5. 【鐵律】絕對不可省略任何具體數字（如：15-20%現金、VIX 40、4倍槓桿、毛利率70%+、漲幅50%、下滑10-20%）、點位、百分比、倍數，以及特殊公司代號（如 GROK、Alchip）。數字是操作的靈魂，省略數字等於廢話。

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
*(記錄主委對於「目前不要做什麼事」的警告，例如對槓桿、追高的看法，並附上具體情境與數字)*

## 五、經典語錄與情境還原 💬
*(列出金句，並務必在一旁補充主委說這句話時的「市場情境」，避免斷章取義)*

【輸出要求】：
- 用繁體中文
- 盡可能詳細，目標 2000-4000 字
- 每個觀點都要有因果邏輯，不要只列結論
- 台股附數字代號（如 2330），美股附英文代號（如 NVDA）
- 不要遺漏任何投資相關的觀點
- 所有具體數字必須完整保留，不可用模糊詞彙取代"""

# ============================================================
# 稽核 Prompt — 第二輪：揉合補漏（不是列清單）
# ============================================================
AUDIT_PROMPT = """你是一位嚴格的財經內容稽核員。
請比對「原始逐字稿」和「第一輪萃取報告」，找出報告中遺漏的重要資訊（例如：隱藏的公司代號、產業心理博弈細節、市場分歧觀點、生動的幹話比喻、具體數字如百分比/點位/倍數）。

【任務要求】：
不要列出補充清單！請直接將你發現漏掉的資訊，「原地無縫揉合」進原本的第一輪報告中對應的段落。
請輸出「經過補漏後的完整最終版報告」，維持第一輪報告的標題架構與表格格式。
如果沒有發現重大遺漏，也請直接輸出原本的第一輪報告。
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
# 步驟 3：分段 + Whisper 轉錄（不壓縮，保持原始品質）
# ============================================================
def split_audio_lossless(input_path, tmpdir, chunk_minutes=8):
    """不壓縮分段，保持原始音質（每段 8 分鐘確保 < 25MB）"""
    log("  正在分段（保持原始品質）...")
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
    ext = os.path.splitext(input_path)[1] or ".mp3"

    while start < total_duration:
        chunk_path = os.path.join(tmpdir, f"chunk_{idx:03d}{ext}")
        cmd = [
            "ffmpeg", "-i", input_path,
            "-ss", str(start), "-t", str(chunk_seconds),
            "-c", "copy",
            chunk_path, "-y"
        ]
        subprocess.run(cmd, capture_output=True, text=True)
        if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
            chunk_mb = os.path.getsize(chunk_path) / (1024 * 1024)
            if chunk_mb > 24:
                log(f"  ⚠️ 段 {idx} 仍有 {chunk_mb:.1f}MB，輕度壓縮...")
                compressed_path = os.path.join(tmpdir, f"chunk_{idx:03d}_c.mp3")
                cmd2 = [
                    "ffmpeg", "-i", chunk_path,
                    "-b:a", "128k", "-ac", "1",
                    compressed_path, "-y"
                ]
                subprocess.run(cmd2, capture_output=True, text=True)
                if os.path.exists(compressed_path):
                    chunks.append(compressed_path)
                else:
                    chunks.append(chunk_path)
            else:
                chunks.append(chunk_path)
        start += chunk_seconds
        idx += 1

    log(f"  ✅ 分成 {len(chunks)} 段（原始品質）")
    return chunks


def transcribe_audio(audio_path):
    log("正在轉錄（Whisper）...")
    client = OpenAI(api_key=OPENAI_API_KEY)
    file_size_mb = os.path.getsize(audio_path) / (1024 * 1024)

    with tempfile.TemporaryDirectory() as tmpdir:
        if file_size_mb <= 24:
            log(f"  檔案 {file_size_mb:.1f} MB，直接上傳（原始品質）")
            with open(audio_path, "rb") as f:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    language=PODCAST_LANG,
                    response_format="text",
                )
            log(f"  ✅ 轉錄完成: {len(transcript)} 字")
            return transcript

        log(f"  檔案 {file_size_mb:.1f} MB，分段處理（保持原始品質）")
        chunks = split_audio_lossless(audio_path, tmpdir)
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


def check_transcript_quality(transcript):
    """檢查逐字稿是否有 Whisper 幻覺（大量重複文字）"""
    if len(transcript) < 500:
        log("  ⚠️ 逐字稿太短，可能轉錄失敗")
        return False

    chunk_size = 50
    text_chunks = [transcript[i:i+chunk_size] for i in range(0, len(transcript), chunk_size)]
    if len(text_chunks) < 10:
        return True

    from collections import Counter
    counter = Counter(text_chunks)
    most_common_count = counter.most_common(1)[0][1]
    repeat_ratio = most_common_count / len(text_chunks)

    if repeat_ratio > 0.3:
        log(f"  ⚠️ 偵測到 Whisper 幻覺！重複率 {repeat_ratio:.0%}，逐字稿品質不佳")
        return False
    return True


# ============================================================
# 步驟 4：Claude Sonnet 4.6 雙重稽核摘要（揉合式）
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
        model="claude-sonnet-4-6",
        max_tokens=8000,
        system=EXTRACT_PROMPT,
        messages=[
            {"role": "user", "content": f"節目標題：{episode_title}\n\n以下是完整逐字稿，請仔細閱讀後產出詳細的結構化報告，不要遺漏任何投資相關的觀點：\n\n{transcript}"}
        ],
    )
    report = extract_response.content[0].text
    log(f"  ✅ 第一輪完成: {len(report)} 字")

    # ========== 第二輪：揉合補漏（直接輸出完整最終版）==========
    log("  🔍 第二輪：稽核揉合...")
    audit_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=10000,
        system=AUDIT_PROMPT,
        messages=[
            {"role": "user", "content": f"【原始逐字稿】：\n{transcript}\n\n---\n\n【第一輪萃取報告】：\n{report}\n\n請比對以上兩者，將遺漏的資訊原地揉合進報告中，輸出完整的最終版。"}
        ],
    )
    final_report = audit_response.content[0].text
    log(f"  ✅ 最終報告（揉合版）: {len(final_report)} 字")
    return final_report


# ============================================================
# 步驟 5a：寄 Email（含 .txt 和 .md 附件）
# ============================================================
def send_email(episode, summary):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT]):
        log("  ⚠️ Email 設定不完整，跳過")
        return False

    log("正在寄送 Email...")
    recipients = [r.strip() for r in EMAIL_RECIPIENT.split(",")]

    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"🎙️ 股癌深度推演：{episode['title']}"
    msg["From"] = EMAIL_SENDER
    msg["To"] = ", ".join(recipients)

    # === 內文 ===
    body_part = MIMEMultipart("alternative")

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

    body_part.attach(MIMEText(text_body, "plain", "utf-8"))
    body_part.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(body_part)

    # === 附件 ===
    title_safe = episode["title"].replace("/", "-").replace("\\", "-").replace("|", "-").replace(" ", "")
    date_str = datetime.now().strftime("%Y-%m-%d")

    # .txt 附件
    txt_content = f"股癌 Gooaye｜產業與市場深度推演\n{'='*50}\n📻 {episode['title']}\n📅 {episode['published']}\n🔗 {episode['link']}\n{'='*50}\n\n{summary}\n\n{'='*50}\n🤖 GitHub Actions + Claude Sonnet 4.6 自動生成\n⚠️ 僅為節目內容記錄，非投資建議\n"
    txt_attachment = MIMEBase("application", "octet-stream")
    txt_attachment.set_payload(txt_content.encode("utf-8"))
    encoders.encode_base64(txt_attachment)
    txt_attachment.add_header("Content-Disposition", "attachment", filename=("utf-8", "", f"{title_safe}_{date_str}_股癌摘要.txt"))
    msg.attach(txt_attachment)

    # .md 附件
    md_attachment = MIMEBase("application", "octet-stream")
    md_attachment.set_payload(summary.encode("utf-8"))
    encoders.encode_base64(md_attachment)
    md_attachment.add_header("Content-Disposition", "attachment", filename=("utf-8", "", f"{title_safe}_{date_str}_股癌摘要.md"))
    msg.attach(md_attachment)

    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, recipients, msg.as_string())
        log(f"  ✅ Email 寄送成功（含 .txt 和 .md 附件）")
        return True
    except Exception as e:
        log(f"  ❌ Email 寄送失敗: {e}")
        return False


# ============================================================
# 步驟 5b：發送 Telegram（HTML 安全解析 + 智慧分段 + 致命錯誤阻斷）
# ============================================================
def send_telegram(episode, summary):
    if not all([TG_BOT_TOKEN, TG_CHAT_ID]):
        log("  ⚠️ Telegram 設定不完整，跳過")
        return False

    log("正在發送 Telegram...")

    def format_telegram_html(text):
        """將 Markdown 轉為 Telegram 安全的 HTML"""
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = re.sub(r'^#+ (.*?)$', r'<b>\1</b>', text, flags=re.MULTILINE)
        text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
        table_pattern = re.compile(r'(^\|.*\|(?: *\r?\n\|.*\|)*)', re.MULTILINE)
        text = table_pattern.sub(r'<pre>\1</pre>', text)
        return text

    html_summary = format_telegram_html(summary)

    header = (
        f"<b>🎙️ 股癌 Gooaye｜深度推演</b>\n\n"
        f"📻 {episode['title']}\n"
        f"📅 {episode['published']}\n"
        f"🤖 Claude Sonnet 4.6 雙重稽核\n"
        f"━━━━━━━━━━━━━━━━\n\n"
    )
    footer = (
        f"\n\n━━━━━━━━━━━━━━━━\n"
        f"🔗 <a href='{episode['link']}'>收聽連結</a>\n"
        f"⚠️ 僅為節目內容記錄，非投資建議"
    )

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

    # 按段落分割，確保表格和標題不被腰斬
    paragraphs = html_summary.split('\n\n')
    chunks = []
    current_chunk = header

    for p in paragraphs:
        if len(p) > 3800:
            sub_parts = [p[i:i+3800] for i in range(0, len(p), 3800)]
            for sp in sub_parts:
                if len(current_chunk) + len(sp) + 2 <= 4000:
                    current_chunk += sp + "\n\n"
                else:
                    chunks.append(current_chunk.strip())
                    current_chunk = sp + "\n\n"
            continue

        if len(current_chunk) + len(p) + 2 <= 4000:
            current_chunk += p + "\n\n"
        else:
            chunks.append(current_chunk.strip())
            current_chunk = p + "\n\n"

    current_chunk += footer
    if len(current_chunk) > 4000:
        current_chunk = current_chunk.replace(footer, "").strip()
        chunks.append(current_chunk)
        chunks.append(footer.strip())
    else:
        chunks.append(current_chunk.strip())

    success = True
    for chunk in chunks:
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code != 200:
                # 攔截 401 或 404 等致命錯誤，直接中斷不再重試
                if resp.status_code in [401, 404]:
                    log(f"  ❌ Telegram 致命錯誤 ({resp.status_code})：Token 或 Chat ID 無效，中止後續段落發送。")
                    success = False
                    break
                # 若為 400 語法錯誤，才觸發純文字降級
                elif resp.status_code == 400:
                    log(f"  ⚠️ HTML 模式失敗 (400)，降級純文字重送...")
                    plain = re.sub(r'<[^>]+>', '', chunk)
                    payload_fb = {"chat_id": TG_CHAT_ID, "text": plain, "disable_web_page_preview": True}
                    resp2 = requests.post(url, json=payload_fb, timeout=30)
                    if resp2.status_code != 200:
                        log(f"  ❌ Telegram 降級發送依然失敗: {resp2.status_code}")
                        success = False
                else:
                    log(f"  ❌ Telegram 發送失敗: {resp.status_code}")
                    success = False
        except Exception as e:
            log(f"  ❌ Telegram 發送發生異常: {e}，中止後續段落發送。")
            success = False
            break  # 網路斷線等嚴重異常，直接中斷
        time.sleep(1)

    if success:
        log("  ✅ Telegram 發送成功")
    return success


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

        if not check_transcript_quality(transcript):
            log("  ⚠️ 逐字稿品質不佳，跳過本集（不發送、不扣費）")
            log("  💡 可能原因：Whisper 產生幻覺文字")
            return None

        summary = generate_summary(transcript, episode["title"])

    send_email(episode, summary)
    send_telegram(episode, summary)

    processed = load_processed()
    processed.append(episode["id"])
    if len(processed) > 200:
        processed = processed[-200:]
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
    test_summary = """# 股癌 Gooaye｜產業與市場深度推演

## 一、宏觀經濟與大盤風向 (Macro & Sentiment)

### 1. 測試項目：VIX 波動率操作邏輯
- VIX > 40 時，事後回頭看幾乎都是好買點
- 現金水位維持 15-20%

## 二、核心產業鏈推演 (Supply Chain Logic) 🔗
- LPU V4 有機會在台積電 (2330) N3 製程 + CoWoS-R 封裝生產

## 三、個股觀察與財報解析 (Stock Deep-Dive)

| 公司代號 | 近期動態/財報表現 | 主委觀點 | 關鍵支撐邏輯 |
|---------|----------------|---------|-----------|
| NVDA | GTC 大會在即 | ✅ 看多 | 4x P/E 相對低位 |
| 2330 | LPU V4 代工傳聞 | ✅ 看多 | 從三星搶單 |
| MU | 毛利率突破 70%+ | ✅ 看多 | 記憶體漲價週期 |

## 四、交易心法與避險警告 ⚠️
- 拒絕任何形式的槓桿操作
- 以 3月2日 股價作為強弱分水嶺

## 五、經典語錄與情境還原 💬
- 「捏住你的軟蛋去買。」 (情境：VIX 突破 40 恐慌極點時)
- 「梁靜茹給的勇氣少掉一半。」 (情境：伊朗新領袖消息打亂抄底計畫)

✅ 如果你收到這則通知（含 .txt 和 .md 附件），代表系統設定成功！"""

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
