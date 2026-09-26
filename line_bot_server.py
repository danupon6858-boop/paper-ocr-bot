import http.server
import json
import hmac
import hashlib
import base64
import urllib.request
import urllib.error
import urllib.parse
import re
import os
import threading
import time
import html as html_lib
from typing import List, Dict, Optional, Tuple
from config import LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, OWNER_USER_ID
import ocr_engine
from validator import OCRValidator
import database
import query_service
import image_cropper

PORT = int(os.environ.get("PORT", 8080))
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://paper-ocr-bot.onrender.com")

def verify_line_signature(body_bytes: bytes, signature: str) -> bool:
    if not signature:
        return False
    gen_sig = base64.b64encode(
        hmac.new(LINE_CHANNEL_SECRET.encode('utf-8'), body_bytes, hashlib.sha256).digest()
    ).decode('utf-8')
    return hmac.compare_digest(gen_sig, signature)

def reply_line_messages(reply_token: str, messages: list) -> bool:
    if not reply_token:
        return False
    url = "https://api.line.me/v2/bot/message/reply"
    payload = {
        "replyToken": reply_token,
        "messages": messages
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
        }
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return True
    except urllib.error.HTTPError as e:
        err_body = e.read().decode('utf-8', errors='ignore') if hasattr(e, 'read') else ''
        print(f"Error sending LINE reply messages: HTTP {e.code} - {err_body}")
        return False
    except Exception as e:
        print(f"Error sending LINE reply messages: {e}")
        return False

def reply_line_message(reply_token: str, text: str, quick_reply_items: list = None) -> bool:
    msg_obj = {"type": "text", "text": text}
    if quick_reply_items:
        msg_obj["quickReply"] = {
            "items": [
                {"type": "action", "action": {"type": "message", "label": label, "text": text_val}}
                for label, text_val in quick_reply_items
            ]
        }
    return reply_line_messages(reply_token, [msg_obj])

def push_line_messages(to_user_id: str, messages: list) -> bool:
    url = "https://api.line.me/v2/bot/message/push"
    payload = {
        "to": to_user_id,
        "messages": messages
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
        }
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return True
    except urllib.error.HTTPError as e:
        err_body = e.read().decode('utf-8', errors='ignore') if hasattr(e, 'read') else ''
        print(f"Error sending LINE push messages: HTTP {e.code} - {err_body}")
        return False
    except Exception as e:
        print(f"Error sending LINE push messages: {e}")
        return False

def push_line_message(to_user_id: str, text: str, quick_reply_items: list = None) -> bool:
    msg_obj = {"type": "text", "text": text}
    if quick_reply_items:
        msg_obj["quickReply"] = {
            "items": [
                {"type": "action", "action": {"type": "message", "label": label, "text": text_val}}
                for label, text_val in quick_reply_items
            ]
        }
    return push_line_messages(to_user_id, [msg_obj])

def deliver_messages(user_id: str, reply_token: str, messages: list) -> bool:
    """Delivers messages via replyToken first (free & unlimited). If expired or failed, falls back to push."""
    if reply_token:
        ok = reply_line_messages(reply_token, messages)
        if ok:
            return True
        print(f"reply_token delivery failed for {user_id}, falling back to push API...")
    return push_line_messages(user_id, messages)

def deliver_message(user_id: str, reply_token: str, text: str, quick_reply_items: list = None) -> bool:
    msg_obj = {"type": "text", "text": text}
    if quick_reply_items:
        msg_obj["quickReply"] = {
            "items": [
                {"type": "action", "action": {"type": "message", "label": label, "text": text_val}}
                for label, text_val in quick_reply_items
            ]
        }
    return deliver_messages(user_id, reply_token, [msg_obj])

def deliver_flex_message(user_id: str, reply_token: str, flex_obj: dict, quick_reply_items: list = None) -> bool:
    """Delivers a LINE Flex Message bubble, with optional Quick Reply items attached."""
    msg_obj = dict(flex_obj)
    if quick_reply_items:
        msg_obj["quickReply"] = {
            "items": [
                {"type": "action", "action": {"type": "message", "label": label, "text": text_val}}
                for label, text_val in quick_reply_items
            ]
        }
    return deliver_messages(user_id, reply_token, [msg_obj])

# ==================== MULTI-IMAGE CONCURRENCY & QUEUE ====================
_user_locks = {}
_user_locks_mutex = threading.Lock()

def get_user_lock(user_id: str) -> threading.Lock:
    """Per-user lock ensuring atomic sheet ID allocation and pending scan creation."""
    with _user_locks_mutex:
        if user_id not in _user_locks:
            _user_locks[user_id] = threading.Lock()
        return _user_locks[user_id]

_user_active_jobs = {}
_user_jobs_mutex = threading.Lock()

def build_flex_ocr_card(
    scan_id: int,
    sheet_id: str,
    emp_name: str,
    worker_code: str,
    period_name: str,
    summary_badge: str,
    clean_text: str,
    uncertain_count: int = 0,
    unclear_block: str = "",
    pending_count: int = 1,
    batch_tag: str = "",
    snippet_url: str = "",
    snippet_label: str = ""
) -> dict:
    """Builds a rich LINE Flex Message card with permanent [ยืนยัน] and [ยกเลิก] buttons inside the card bubble."""
    display_text = clean_text
    if len(display_text) > 1800:
        lines = display_text.splitlines()
        truncated = lines[:35]
        truncated.append(f"... (มีต่ออีก {len(lines) - 35} บรรทัด ตรวจสอบเต็มได้บนเว็บ)")
        display_text = "\n".join(truncated)

    body_contents = [
        {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#f0fdf4" if uncertain_count == 0 else "#fffbeb",
            "cornerRadius": "8px",
            "paddingAll": "8px",
            "contents": [
                {
                    "type": "text",
                    "text": summary_badge,
                    "size": "xs",
                    "weight": "bold",
                    "color": "#16a34a" if uncertain_count == 0 else "#d97706",
                    "wrap": True
                }
            ]
        },
        {"type": "separator", "margin": "md"},
        {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#f8fafc",
            "cornerRadius": "8px",
            "paddingAll": "10px",
            "margin": "md",
            "contents": [
                {
                    "type": "text",
                    "text": display_text,
                    "size": "sm",
                    "color": "#1e293b",
                    "wrap": True
                }
            ]
        }
    ]

    if snippet_url:
        label_text = f"🔍 ลายมือจริงที่ AI ไม่มั่นใจ ({snippet_label}):" if snippet_label else "🔍 ภาพลายมือจริงจุดที่ AI ไม่มั่นใจ:"
        body_contents.append({
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#fffbeb",
            "cornerRadius": "8px",
            "paddingAll": "8px",
            "margin": "md",
            "contents": [
                {
                    "type": "text",
                    "text": label_text,
                    "size": "xs",
                    "weight": "bold",
                    "color": "#b45309",
                    "wrap": True
                },
                {
                    "type": "image",
                    "url": snippet_url,
                    "size": "full",
                    "aspectRatio": "20:9",
                    "aspectMode": "fit",
                    "margin": "sm"
                }
            ]
        })

    if unclear_block.strip():
        body_contents.append({
            "type": "text",
            "text": unclear_block.strip(),
            "size": "xs",
            "color": "#b45309",
            "wrap": True,
            "margin": "md"
        })

    if pending_count > 1:
        body_contents.append({
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#eff6ff",
            "cornerRadius": "6px",
            "paddingAll": "8px",
            "margin": "md",
            "contents": [
                {
                    "type": "text",
                    "text": f"💡 มีรายการรอยืนยัน {pending_count} ใบ (กดปุ่มด้านล่าง หรือพิมพ์ 'ยืนยันทั้งหมด')",
                    "size": "xxs",
                    "color": "#2563eb",
                    "wrap": True
                }
            ]
        })

    body_contents.append({
        "type": "text",
        "text": "• คัดลอกข้อความไปแก้แล้วส่งกลับ หรือกดปุ่มด้านล่างเพื่อยืนยัน",
        "size": "xxs",
        "color": "#94a3b8",
        "wrap": True,
        "margin": "sm"
    })

    header_title_items = [
        {
            "type": "text",
            "text": f"📋 ใบที่: {sheet_id}",
            "weight": "bold",
            "color": "#38bdf8",
            "size": "md",
            "flex": 3
        }
    ]
    if batch_tag:
        header_title_items.append({
            "type": "text",
            "text": str(batch_tag),
            "size": "xs",
            "color": "#94a3b8",
            "align": "end",
            "flex": 2
        })

    bubble = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#0f172a",
            "paddingAll": "14px",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": header_title_items
                },
                {
                    "type": "text",
                    "text": f"👤 {emp_name} ({worker_code}) | {period_name}",
                    "size": "xs",
                    "color": "#cbd5e1",
                    "margin": "xs"
                }
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "14px",
            "spacing": "sm",
            "contents": body_contents
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "paddingAll": "12px",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "spacing": "sm",
                    "contents": [
                        {
                            "type": "button",
                            "style": "primary",
                            "color": "#16a34a",
                            "height": "sm",
                            "action": {
                                "type": "message",
                                "label": "✅ ยืนยันใบนี้",
                                "text": f"ยืนยัน {scan_id}"
                            }
                        },
                        {
                            "type": "button",
                            "style": "secondary",
                            "color": "#dc2626",
                            "height": "sm",
                            "action": {
                                "type": "message",
                                "label": "❌ ยกเลิก",
                                "text": f"ยกเลิก {scan_id}"
                            }
                        }
                    ]
                },
                {
                    "type": "button",
                    "style": "link",
                    "height": "sm",
                    "action": {
                        "type": "uri",
                        "label": "✏️ ตรวจสอบ/แก้ไขบนเว็บ",
                        "uri": f"{BASE_URL}/edit/{scan_id}"
                    }
                }
            ]
        }
    }

    return {
        "type": "flex",
        "altText": f"📋 ข้อมูลใบที่ {sheet_id} (รอกดยืนยัน)",
        "contents": bubble
    }

def send_line_loading_indicator(chat_id: str, loading_seconds: int = 60):
    url = "https://api.line.me/v2/bot/chat/loading/start"
    payload = {
        "chatId": chat_id,
        "loadingSeconds": min(loading_seconds, 60)
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
        }
    )
    try:
        with urllib.request.urlopen(req) as resp:
            pass
    except Exception as e:
        print(f"Error sending loading indicator: {e}")

def get_line_profile(user_id: str) -> str:
    url = f"https://api.line.me/v2/bot/profile/{user_id}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("displayName", "ผู้ใช้งานใหม่")
    except Exception:
        return "ผู้ใช้งานใหม่"

def get_line_image_content(message_id: str) -> bytes:
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        err_b = e.read().decode('utf-8', errors='ignore') if hasattr(e, 'read') else ''
        raise RuntimeError(f"LINE Content API error ({e.code} {e.reason}): {err_b}")

def normalize_brace_groupings(columns: dict) -> dict:
    """
    Scans columns for contiguous runs of items that indicate bracket/brace sharing
    (e.g., 'ปีกการ่วมกับแถวอื่น', 'ใช้ปีกการ่วมกับแถวบน', 'ปีกกา') and normalizes their
    uncertain_note to 'ปีกการ่วมกัน [i]/[total]' (e.g. 'ปีกการ่วมกัน 1/10', 'ปีกการ่วมกัน 2/10').
    """
    if not isinstance(columns, dict):
        return columns
    keywords = ["ปีกกา", "วงเล็บ", "ร่วมกัน", "แถวบน", "แถวล่าง", "แถวอื่น"]
    for col_key in ["top", "bottom", "top_bottom"]:
        items = columns.get(col_key, [])
        if not isinstance(items, list) or len(items) < 2:
            continue
        n = len(items)
        i = 0
        while i < n:
            note = str(items[i].get("uncertain_note") or "").strip()
            is_brace = any(k in note for k in keywords)
            
            if is_brace:
                group_start = i
                # Check backward if previous item has the same set2 and either no note or brace note
                if i > 0:
                    prev_s2 = str(items[i-1].get("set2") or "").strip()
                    curr_s2 = str(items[i].get("set2") or "").strip()
                    prev_note = str(items[i-1].get("uncertain_note") or "").strip()
                    if prev_s2 == curr_s2 and (not prev_note or any(k in prev_note for k in keywords)):
                        if not ("/" in prev_note and "ปีกกา" in prev_note):
                            group_start = i - 1
                
                curr_s2 = str(items[group_start].get("set2") or "").strip()
                group_end = group_start
                while group_end + 1 < n:
                    next_s2 = str(items[group_end + 1].get("set2") or "").strip()
                    next_note = str(items[group_end + 1].get("uncertain_note") or "").strip()
                    if next_s2 == curr_s2 and (any(k in next_note for k in keywords) or any(k in str(items[group_end].get("uncertain_note") or "") for k in keywords)):
                        group_end += 1
                    else:
                        break
                
                group_len = group_end - group_start + 1
                if group_len > 1:
                    for idx, item_idx in enumerate(range(group_start, group_end + 1), start=1):
                        orig_note = items[item_idx].get("uncertain_note", "")
                        other_notes = [part for part in orig_note.split() if not any(k in part for k in keywords)]
                        other_text = f" ({' '.join(other_notes)})" if other_notes else ""
                        items[item_idx]["uncertain_note"] = f"ปีกการ่วมกัน {idx}/{group_len}{other_text}"
                i = group_end + 1
            else:
                i += 1
    return columns

def format_clean_editable_text(sheet_num: str, columns: dict) -> str:
    lines = [f"ใบที่ {sheet_num}"]
    
    for col_key, col_title in [("top", "[บน]"), ("bottom", "[ล่าง]"), ("top_bottom", "[บนล่าง]")]:
        items = columns.get(col_key, [])
        if items:
            lines.append(col_title)
            for itm in items:
                s1 = str(itm.get("set1") or "").strip()
                s3 = str(itm.get("set3") or "").strip()
                s3_part = f"{s3} " if s3 else ""
                s2 = str(itm.get("set2") or "").strip()
                confidence = str(itm.get("confidence") or "high").strip().lower()
                note = str(itm.get("uncertain_note") or "").strip()
                
                # Flag uncertain or question-mark items
                if confidence == "low" or "?" in s1 or "?" in s2 or note:
                    warn_tag = "⚠️ "
                    note_tag = f"  <-- ({note})" if note else ""
                else:
                    warn_tag = ""
                    note_tag = ""
                    
                lines.append(f"{warn_tag}{s1} = {s3_part}{s2}{note_tag}".strip())
            lines.append("")
            
    return "\n".join(lines).strip()

COL_MAP = {
    "top_bottom": [
        "บนล่าง", "บ-ล", "บล", "บ/ล", "บ.ล.", "บ.ล", "บน-ล่าง", "บน/ล่าง", "บนล", "both", "bl",
        "หมวดบนล่าง", "หมวด บ-ล", "หมวด บล", "หมวด บนล่าง"
    ],
    "top": [
        "บน", "บ", "top", "t", "หมวดบน", "หมวด บ", "หมวด บน"
    ],
    "bottom": [
        "ล่าง", "ล", "ล่", "bot", "bottom", "l", "หมวดล่าง", "หมวด ล", "หมวด ล่าง"
    ]
}

def detect_column_header(line: str) -> Optional[str]:
    clean = line.replace("[", "").replace("]", "").replace(":", "").replace("หมวด", "").strip().lower()
    clean_raw = line.replace("[", "").replace("]", "").replace(":", "").strip()
    
    # Check top_bottom first because it contains both 'บ' and 'ล'
    for alias in COL_MAP["top_bottom"]:
        a_clean = alias.replace("หมวด", "").strip().lower()
        if clean == a_clean or clean_raw == alias:
            return "top_bottom"
            
    for alias in COL_MAP["top"]:
        a_clean = alias.replace("หมวด", "").strip().lower()
        if clean == a_clean or clean_raw == alias:
            return "top"
            
    for alias in COL_MAP["bottom"]:
        a_clean = alias.replace("หมวด", "").strip().lower()
        if clean == a_clean or clean_raw == alias:
            return "bottom"
            
    return None

def parse_entries_from_line(line: str) -> list:
    """Intelligently tokenizes and extracts 1 or more entries from a line.
    
    Identifies set1 (2-4 digits/question marks), set3 ('ก3' or 'ก6'), and set2 (amount or NxN).
    Supports all common separators (=, -, :, /, whitespace, comma, semicolon).
    Safely ignores notes like <-- (...) and emojis.
    """
    line = line.strip()
    if not line:
        return []

    # Strip annotations like <-- (...) or (...) and warning emojis before tokenizing
    line = re.sub(r'<--.*$', '', line)
    line = re.sub(r'\(.*?\)', '', line)
    line = line.replace('⚠️', '').replace('❗', '').strip()

    # Normalize NxN formats (e.g. 120 X 120 -> 120x120)
    normalized = re.sub(r'([\d?]+)\s*[xX]\s*([\d?]+)', r'\1x\2', line)
    # Split stuck special codes (e.g. 401ก350 -> 401 ก3 50)
    normalized = re.sub(r'([\d?]+)(ก[36])', r'\1 \2 ', normalized)
    normalized = re.sub(r'(ก[36])([\d?]+)', r' \1 \2', normalized)

    sub_chunks = re.split(r'[,;]+', normalized)
    entries = []
    
    for chunk in sub_chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
            
        # Extract meaningful tokens: special codes, NxN, or numeric blocks (supporting '?')
        tokens = re.findall(r'(ก[36]|[\d?]+x[\d?]+|[\d?]+)', chunk)
        if not tokens:
            continue
            
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            # set1 must be 2-4 digits or '?' (e.g. 40?)
            if re.fullmatch(r'[\d?]{2,4}', tok):
                s1 = tok
                s3 = ""
                s2 = ""
                i += 1
                
                if i < len(tokens):
                    next_tok = tokens[i]
                    if next_tok in ("ก3", "ก6"):
                        s3 = next_tok
                        i += 1
                        if i < len(tokens) and re.fullmatch(r'[\d?]+', tokens[i]):
                            s2 = tokens[i]
                            i += 1
                    elif re.fullmatch(r'[\d?]+x[\d?]+', next_tok):
                        s2 = next_tok
                        i += 1
                    elif re.fullmatch(r'[\d?]+', next_tok):
                        s2 = next_tok
                        i += 1
                
                confidence = "low" if ("?" in s1 or "?" in s2) else "high"
                s3_part = f"{s3} " if s3 else ""
                raw = f"{s1} = {s3_part}{s2}".strip() if (s2 or s3) else s1
                entries.append({
                    "set1": s1,
                    "set2": s2,
                    "set3": s3,
                    "raw_text": raw,
                    "confidence": confidence,
                    "uncertain_note": ""
                })
            else:
                i += 1
                
    return entries

def parse_entry_line(line: str) -> Optional[dict]:
    """Parses a single entry line (returns the first valid entry found)."""
    entries = parse_entries_from_line(line)
    return entries[0] if entries else None

def parse_sheet_text(text: str) -> Optional[dict]:
    """Parses an entire sheet text block, supporting column abbreviations and multiple formats."""
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    if not lines:
        return None
        
    sheet_id = None
    current_col = "top"
    columns = {"top": [], "bottom": [], "top_bottom": []}
    has_entries = False
    
    # Patterns for lines to skip (separators, hints, badges, emojis-only)
    SKIP_PATTERNS = [
        r"^─+$",                        # ─────── separator line
        r"^[-─=*]{3,}$",               # --- or === separator
        r"^[📊📌📋✅✏️⚠️❌💡]\s",     # emoji-only hint lines
        r"^(✅|❌|⚠️|💡|📊|📌|📋|✏️)",  # leading emoji hint
        r"^ตรวจพบ:|^อ่านข้อมูลได้|^ผู้ส่ง:|^ข้อมูลนี้จะถูก|^จุดที่ AI",  # summary header lines
        r"^AI ไม่มั่นใจ|^หากตรวจสอบ|^หากถูกต้อง|^คัดลอก|^กดปุ่ม",     # hint text
    ]
    SKIP_RE = re.compile("|".join(SKIP_PATTERNS), re.UNICODE)
    
    for line in lines:
        # Skip separator/hint lines
        if SKIP_RE.search(line):
            continue

        # Flexible sheet header matching (e.g. ใบที่ 1, ใบ A-1, แผ่นที่ 2, No. 5, #3, เลขที่ 10)
        m_sheet = re.search(r'(?:ใบที่|ใบ|แผ่นที่|แผ่น|no\.?|#|เลขที่)\s*[:\-]?\s*([A-Za-z0-9\-_/]+)', line, re.IGNORECASE)
        if m_sheet:
            found_id = m_sheet.group(1).strip()
            if not sheet_id or (found_id and "-" in found_id):
                sheet_id = found_id
            continue
            
        detected_col = detect_column_header(line)
        if detected_col:
            current_col = detected_col
            continue
            
        entries = parse_entries_from_line(line)
        if entries:
            columns[current_col].extend(entries)
            has_entries = True
            
    if not has_entries:
        return None
        
    return {"sheet_id": sheet_id, "columns": columns}



def handle_delete_command(text: str, user_id: str, reply_token: str) -> bool:
    m = re.match(r"^ลบ\s+([A-Za-z0-9?]+)(?:[=\s].*)?$", text.strip())
    if not m:
        return False
    target_num = m.group(1).strip()
    
    scan = database.get_latest_pending_scan(user_id)
    if not scan:
        reply_line_message(reply_token, "⚠️ ไม่พบรายการที่รอยืนยันอยู่ในขณะนี้ครับ")
        return True
        
    # Read from ocr_json (clean format, no is_valid/errors keys)
    ocr_data = json.loads(scan["ocr_json"])
    cols = ocr_data.get("columns", {})
    
    # Check if set1 appears in multiple columns
    found_in = [c for c in ["top", "bottom", "top_bottom"]
                if any(itm.get("set1") == target_num for itm in cols.get(c, []))]
    if len(found_in) > 1:
        col_names = {"top": "บน", "bottom": "ล่าง", "top_bottom": "บนล่าง"}
        cols_str = " และ ".join(col_names[c] for c in found_in)
        reply_line_message(reply_token,
            f"⚠️ เลข {target_num} ปรากฏในหลายคอลัมน์ ({cols_str})\n"
            f"💡 กรุณาคัดลอกข้อความทั้งใบ แก้ไข แล้วส่งกลับมาแทนครับ")
        return True
    
    found = False
    new_cols = {"top": [], "bottom": [], "top_bottom": []}
    for c in ["top", "bottom", "top_bottom"]:
        for itm in cols.get(c, []):
            if itm.get("set1") == target_num and not found:
                found = True
                database.log_correction(
                    scan_id=scan["id"],
                    sheet_id=scan["sheet_id"],
                    period_id=scan["period_id"],
                    worker_code=scan["worker_code"],
                    action_type="DELETE",
                    old_set1=target_num,
                    old_set2=itm.get("set2", ""),
                    category=c,
                    note="ผู้ใช้สั่งลบรายการออก (False Positive)"
                )
                continue
            new_cols[c].append(itm)
            
    if not found:
        reply_line_message(reply_token, f"⚠️ ไม่พบเลข {target_num} ในใบที่ {scan['sheet_id']} ครับ")
        return True
        
    database.update_pending_scan_items(scan["id"], new_cols)
    clean_text = format_clean_editable_text(scan["sheet_id"], new_cols)
    
    msg_ack = f"🗑️ ลบเลข {target_num} ออกจากใบที่ {scan['sheet_id']} เรียบร้อยแล้วครับ!\n(หากถูกต้องแล้ว กด 'ยืนยัน' หรือคัดลอกข้อความด้านล่างไปแก้ไขต่อได้เลยครับ)"
    quick_replies = [("✅ ยืนยัน", f"ยืนยัน {scan['id']}"), ("❌ ยกเลิก", f"ยกเลิก {scan['id']}")]
    reply_line_message(reply_token, f"{msg_ack}\n\n{clean_text}", quick_replies)
    return True


def handle_edit_command(text: str, user_id: str, reply_token: str) -> bool:
    m = re.match(r"^แก้\s+([A-Za-z0-9?]+(?:[=\s][^\s]+)?)\s+เป็น\s+(.+)$", text.strip())
    if not m:
        return False
    old_target = m.group(1).strip()
    new_target = m.group(2).strip()
    
    scan = database.get_latest_pending_scan(user_id)
    if not scan:
        reply_line_message(reply_token, "⚠️ ไม่พบรายการที่รอยืนยันอยู่ในขณะนี้ครับ")
        return True
        
    # Read from ocr_json (clean format, no is_valid/errors keys)
    ocr_data = json.loads(scan["ocr_json"])
    cols = ocr_data.get("columns", {})
    
    old_s1 = old_target.split("=")[0].strip() if "=" in old_target else old_target.split()[0].strip()
    parsed_new = parse_entry_line(new_target)
    if not parsed_new:
        reply_line_message(reply_token, f"❌ รูปแบบเลขใหม่ '{new_target}' ไม่ถูกต้อง (เช่น 375 หรือ 375=36x36)")
        return True
    
    # Check if set1 appears in multiple columns
    found_in = [c for c in ["top", "bottom", "top_bottom"]
                if any(itm.get("set1") == old_s1 for itm in cols.get(c, []))]
    if len(found_in) > 1:
        col_names = {"top": "บน", "bottom": "ล่าง", "top_bottom": "บนล่าง"}
        cols_str = " และ ".join(col_names[c] for c in found_in)
        reply_line_message(reply_token,
            f"⚠️ เลข {old_s1} ปรากฏในหลายคอลัมน์ ({cols_str})\n"
            f"💡 กรุณาคัดลอกข้อความทั้งใบ แก้ไข แล้วส่งกลับมาแทนครับ")
        return True
        
    found = False
    new_cols = {"top": [], "bottom": [], "top_bottom": []}
    for c in ["top", "bottom", "top_bottom"]:
        for itm in cols.get(c, []):
            if itm.get("set1") == old_s1 and not found:
                found = True
                updated_itm = dict(itm)
                if parsed_new["set2"]:
                    updated_itm["set1"] = parsed_new["set1"]
                    updated_itm["set2"] = parsed_new["set2"]
                    updated_itm["set3"] = parsed_new["set3"]
                    updated_itm["raw_text"] = parsed_new["raw_text"]
                else:
                    updated_itm["set1"] = parsed_new["set1"]
                    s3 = updated_itm.get("set3", "")
                    s3_p = f"{s3} " if s3 else ""
                    s2 = updated_itm.get("set2", "")
                    updated_itm["raw_text"] = f"{updated_itm['set1']} = {s3_p}{s2}".strip()
                # Clear uncertainty flags once edited manually
                updated_itm["confidence"] = "high"
                updated_itm["uncertain_note"] = ""
                new_cols[c].append(updated_itm)
                
                database.log_correction(
                    scan_id=scan["id"],
                    sheet_id=scan["sheet_id"],
                    period_id=scan["period_id"],
                    worker_code=scan["worker_code"],
                    action_type="SINGLE_EDIT",
                    old_set1=old_s1,
                    new_set1=parsed_new["set1"],
                    old_set2=itm.get("set2", ""),
                    new_set2=parsed_new["set2"] or itm.get("set2", ""),
                    category=c,
                    note=f"แก้ไข {old_target} เป็น {new_target}"
                )
            else:
                new_cols[c].append(itm)

                
    if not found:
        reply_line_message(reply_token, f"⚠️ ไม่พบเลข {old_s1} ในใบที่ {scan['sheet_id']} ครับ")
        return True
        
    database.update_pending_scan_items(scan["id"], new_cols)
    clean_text = format_clean_editable_text(scan["sheet_id"], new_cols)
    msg_ack = f"✏️ แก้ไขเลข {old_s1} ในใบที่ {scan['sheet_id']} เรียบร้อยแล้วครับ!\n(หากถูกต้องแล้ว กด 'ยืนยัน' หรือคัดลอกข้อความด้านล่างไปแก้ไขต่อได้เลยครับ)"
    quick_replies = [("✅ ยืนยัน", f"ยืนยัน {scan['id']}"), ("❌ ยกเลิก", f"ยกเลิก {scan['id']}")]
    reply_line_message(reply_token, f"{msg_ack}\n\n{clean_text}", quick_replies)
    return True


def handle_image_message(message_id: str, reply_token: str, user_id: str, user_info: dict, job_idx: int = 1):
    # 1. Check Period Status
    active_p = database.get_active_period()
    if not active_p:
        with _user_jobs_mutex:
            _user_active_jobs[user_id] = max(0, _user_active_jobs.get(user_id, 1) - 1)
        msg = (
            "⛔️ ขออภัยครับ ขณะนี้ระบบปิดรับข้อมูล (ยังไม่เปิดงวดใหม่)\n"
            "กรุณารอเจ้าของเปิดงวดก่อนส่งรูปครับ\n\n"
            f"🌐 ตรวจสอบสถานะงวดได้ที่:\n{BASE_URL}"
        )
        deliver_message(user_id, reply_token, msg)
        return

    # Start 60s typing indicator (native animation in LINE chat)
    send_line_loading_indicator(user_id, 60)

    try:
        worker_code = user_info.get("worker_code", "A")
        emp_name = user_info.get("display_name", "พนักงาน")

        print(f"Downloading image #{job_idx} from {emp_name} ({worker_code})...")
        img_bytes = get_line_image_content(message_id)
        
        print(f"Processing OCR #{job_idx} with Gemini 3.6 Flash...")
        ocr_start_time = time.time()
        ocr_result = ocr_engine.extract_from_image(img_bytes)
        ocr_latency_ms = int((time.time() - ocr_start_time) * 1000)
        
        # Check if any data extracted
        raw_cols = ocr_result.get("columns", {}) if ocr_result else {}
        total_raw = sum(len(items) for items in raw_cols.values())
        if total_raw == 0:
            no_data_msg = (
                f"⚠️ ภาพนี้ (รูปที่ {job_idx}) ระบบอ่านตัวเลขไม่พบ หรือลายมือไม่ชัดเจนครับ\n"
                "─────────────────────────\n"
                "💡 คำแนะนำ:\n"
                "1. ตรวจสอบความสว่าง/ความคมชัด แล้วถ่ายรูปส่งใหม่อีกครั้งครับ 📷\n"
                "2. หรือพิมพ์ข้อความตัวเลขส่งเข้ามาทางแชทนี้ได้เลยครับ เช่น:\n"
                "ใบที่ 1\n"
                "[บน]\n"
                "401 = 120x120"
            )
            deliver_message(user_id, reply_token, no_data_msg)
            return

        print(f"Validating rules #{job_idx}...")
        normalize_brace_groupings(ocr_result.get("columns", {}))
        val_result = OCRValidator.validate_document(ocr_result)
        valid_cols = val_result.get("validated_columns", {})
        normalize_brace_groupings(valid_cols)
        
        header = ocr_result.get("header") if isinstance(ocr_result.get("header"), dict) else {}
        ocr_result["header"] = header
        raw_sheet_id = str(header.get("sheet_id") or "").strip()

        # Thread-safe Sheet ID resolution, image saving, duplicate check, and pending scan creation
        user_lock = get_user_lock(user_id)
        with user_lock:
            if raw_sheet_id and raw_sheet_id.lower() not in ["none", "null", "n/a", ""]:
                candidate_sheet_id = f"{worker_code}-{raw_sheet_id}" if not raw_sheet_id.startswith(f"{worker_code}-") else raw_sheet_id
            else:
                candidate_sheet_id = ""

            if not candidate_sheet_id or database.is_sheet_id_taken(active_p["id"], candidate_sheet_id):
                formatted_sheet_id = database.get_next_available_sheet_id(active_p["id"], worker_code)
                raw_sheet_id = formatted_sheet_id.split("-", 1)[1] if "-" in formatted_sheet_id else formatted_sheet_id
            else:
                formatted_sheet_id = candidate_sheet_id

            ocr_result["header"]["sheet_id"] = formatted_sheet_id
            ocr_result["header"]["customer_name"] = emp_name

            # Save uploaded image to disk for training archive and dashboard viewing
            image_path = database.save_uploaded_image(img_bytes, active_p["id"], formatted_sheet_id)
            if image_path:
                import gdrive_sync
                gdrive_sync.trigger_image_backup(image_path)

            # Check Duplicate Sheet
            is_dup, dup_msg = database.check_duplicate_sheet(
                active_p["id"], formatted_sheet_id, valid_cols
            )
            if is_dup:
                dup_reply = (
                    f"⛔️ [ตรวจพบกระดาษซ้ำแผ่นเดียวกัน!]\n"
                    f"─────────────────────────\n"
                    f"{dup_msg}\n\n"
                    f"💡 ระบบระงับการบันทึกใบนี้ เพื่อป้องกันตัวเลขเบิ้ลครับ"
                )
                deliver_message(user_id, reply_token, dup_reply)
                return

            # Identify first uncertain item with box_2d to crop snippet for LINE chat
            snippet_url = ""
            snippet_label = ""
            uncertain_item = None
            for col_k in ["top", "bottom", "top_bottom"]:
                for itm in valid_cols.get(col_k, []):
                    s1 = str(itm.get("set1") or "")
                    s2 = str(itm.get("set2") or "")
                    conf = str(itm.get("confidence") or "").lower()
                    u_note = str(itm.get("uncertain_note") or "")
                    box = itm.get("box_2d")
                    is_doubtful = (conf == "low" or "?" in s1 or "?" in s2 or (u_note and not u_note.startswith("ปีกการ่วมกัน")))
                    if is_doubtful and box and len(box) == 4:
                        uncertain_item = itm
                        snippet_label = f"{s1} = {s2}"
                        break
                if uncertain_item:
                    break

            # Create Pending Scan
            scan_id = database.create_pending_scan(
                user_id, worker_code, emp_name, active_p["id"], formatted_sheet_id, ocr_result, val_result,
                image_path=image_path, ocr_latency_ms=ocr_latency_ms
            )

            # Crop snippet for uncertain item
            if uncertain_item and image_path:
                try:
                    snippets_dir = os.path.join(database.UPLOADS_DIR, str(active_p["id"]), "snippets")
                    os.makedirs(snippets_dir, exist_ok=True)
                    snip_filename = f"snip_{scan_id}_0.jpg"
                    snip_file_path = os.path.join(snippets_dir, snip_filename)
                    if image_cropper.crop_snippet_from_bytes(img_bytes, uncertain_item["box_2d"], snip_file_path, source_path=image_path):
                        rel_path = f"{active_p['id']}/snippets/{snip_filename}"
                        snippet_url = f"{BASE_URL}/uploads/{rel_path}"
                        uncertain_item["snippet_path"] = rel_path
                except Exception as ce:
                    print(f"Error generating crop snippet: {ce}")

        top_items = valid_cols.get("top", [])
        bot_items = valid_cols.get("bottom", [])
        topbot_items = valid_cols.get("top_bottom", [])
        all_items = top_items + bot_items + topbot_items
        total_items = len(all_items)
        
        uncertain_count = sum(1 for itm in all_items if itm.get("confidence") == "low" or itm.get("uncertain_note") or "?" in str(itm.get("set1", "")) or "?" in str(itm.get("set2", "")))
        clear_count = total_items - uncertain_count
        
        if uncertain_count > 0:
            summary_badge = f"📊 ตรวจพบ: {total_items} ชุด (✅ ชัดเจน: {clear_count} | ⚠️ ไม่มั่นใจ: {uncertain_count} ชุด)"
            hint_block = (
                f"⚠️ มี {uncertain_count} ชุดที่ AI ไม่มั่นใจ (มีเครื่องหมาย ⚠️)\n"
                f"💡 คัดลอกข้อความไปแก้ตัวเลข หรือพิมพ์ 'แก้ [เลขเดิม] เป็น [เลขใหม่]' ได้เลยครับ\n"
                f"✅ หากตรวจสอบแล้วถูกต้อง: กดปุ่ม [ ยืนยัน ] ได้ทันทีครับ"
            )
        else:
            summary_badge = f"📊 รวมทั้งหมด: {total_items} ชุด (✅ ชัดเจนครบทุกชุด)"
            hint_block = (
                f"✅ หากถูกต้อง: กดปุ่ม [ ยืนยัน ] ด้านล่างได้เลยครับ\n"
                f"✏️ หากต้องการแก้ไข: คัดลอกข้อความนี้ไปแก้/ลบตัวเลข แล้วส่งกลับมาได้ทันทีครับ"
            )
            
        unclear_notes = ocr_result.get("unclear_notes", []) if isinstance(ocr_result, dict) else []
        unclear_block = ""
        if unclear_notes:
            unclear_block = "⚠️ จุดที่ AI สังเกตว่าไม่ชัดเจน:\n" + "\n".join(f"• {n}" for n in unclear_notes[:3]) + "\n─────────────────────────\n"

        clean_text = format_clean_editable_text(raw_sheet_id, valid_cols)
        
        # Check active pending scans count for this user
        all_pendings = database.get_all_pending_scans(user_id)
        pending_count = len(all_pendings)
        
        batch_tag = f"รูปที่ {job_idx}"

        quick_replies = [
            ("✅ ยืนยันใบนี้", f"ยืนยัน {scan_id}"),
            ("❌ ยกเลิก", f"ยกเลิก {scan_id}")
        ]
        if pending_count > 1:
            quick_replies.append(("✅ ยืนยันทั้งหมด", "ยืนยันทั้งหมด"))
            quick_replies.append(("❌ ยกเลิกทั้งหมด", "ยกเลิกทั้งหมด"))

        # Build permanent Flex Message card
        flex_card = build_flex_ocr_card(
            scan_id=scan_id,
            sheet_id=formatted_sheet_id,
            emp_name=emp_name,
            worker_code=worker_code,
            period_name=active_p["name"],
            summary_badge=summary_badge,
            clean_text=clean_text,
            uncertain_count=uncertain_count,
            unclear_block=unclear_block,
            pending_count=pending_count,
            batch_tag=batch_tag,
            snippet_url=snippet_url,
            snippet_label=snippet_label
        )

        sent_ok = deliver_flex_message(user_id, reply_token, flex_card, quick_replies)
        if not sent_ok:
            # Fallback to plain text message if Flex delivery encounters any issue
            combined_text = (
                f"📌 ข้อมูลนี้จะถูกบันทึกลงใน: {active_p['name']}\n"
                f"📋 อ่านข้อมูลได้ [ใบที่: {formatted_sheet_id}]\n"
                f"{summary_badge} | ผู้ส่ง: {emp_name} ({worker_code})\n"
                f"─────────────────────────\n"
                f"{clean_text}\n"
                f"─────────────────────────\n"
                f"{unclear_block}"
                f"{hint_block}"
            )
            deliver_message(user_id, reply_token, combined_text, quick_replies)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error handling image for {user_id}: {e}")
        err_type = type(e).__name__
        err_detail = f" ({err_type}: {str(e)[:100]})" if str(e) else f" ({err_type})"
        if "503" in str(e):
            err_msg = (
                f"⚠️ เซิร์ฟเวอร์ AI มีผู้ใช้งานหนาแน่นชั่วคราว{err_detail}\n"
                "─────────────────────────\n"
                "💡 ระบบโมเดล AI ของ Google กำลังประมวลผลคำขอปริมาณมากในขณะนี้\n\n"
                "คำแนะนำ:\n"
                "• กรุณารอสักครู่ (ประมาณ 10-15 วินาที) แล้วกดส่งรูปเดิมใหม่อีกครั้งครับ 📷"
            )
        else:
            err_msg = (
                f"⚠️ ภาพนี้อ่านยากหรือเกิดข้อผิดพลาดระหว่างประมวลผลครับ{err_detail}\n"
                "─────────────────────────\n"
                "💡 ระบบไม่สามารถอ่านตัวเลขได้ชัดเจนในรอบนี้\n\n"
                "คำแนะนำ:\n"
                "1. ถ่ายใหม่อีกครั้งในมุมตรง ให้เห็นตารางและตัวเลขชัดเจน 📷\n"
                "2. หรือพิมพ์ข้อความตัวเลขส่งเข้ามาทางแชทนี้ได้เลยครับ เช่น:\n"
                "ใบที่ 1\n"
                "[บน]\n"
                "401 = 120x120"
            )
        deliver_message(user_id, reply_token, err_msg)
    finally:
        with _user_jobs_mutex:
            _user_active_jobs[user_id] = max(0, _user_active_jobs.get(user_id, 1) - 1)
            remaining_jobs = _user_active_jobs[user_id]
        if remaining_jobs > 0:
            # Re-activate loading animation in LINE chat because previous message delivery cancelled it
            send_line_loading_indicator(user_id, 60)

def handle_owner_command(text: str, reply_token: str, user_id: str = "") -> bool:
    clean = text.strip()
    
    if clean in ["รออนุมัติ", "รายชื่อรออนุมัติ", "ผู้ใช้งาน", "สมาชิก"]:
        pendings = database.get_pending_users()
        if not pendings:
            reply_line_message(reply_token, "✅ ไม่มีผู้ใช้งานรอการอนุมัติในขณะนี้ครับ")
            return True
            
        p_text = f"📋 [รายชื่อรอการอนุมัติ: {len(pendings)} ท่าน]\n─────────────────────────\n"
        for idx, pu in enumerate(pendings[:5], 1):
            p_text += f"{idx}. {pu['display_name']} (ID: {pu['user_id'][:8]}...)\n"
            
        next_c = database.get_next_worker_code()
        latest_pu = pendings[0]
        q_replies = [
            (f"✅ อนุมัติ ({next_c})", f"อนุมัติ id:{latest_pu['user_id']} {next_c}"),
            ("✅ อนุมัติ (A)", f"อนุมัติ id:{latest_pu['user_id']} A"),
            ("✅ อนุมัติ (B)", f"อนุมัติ id:{latest_pu['user_id']} B"),
            ("⛔️ บล็อก", f"บล็อก id:{latest_pu['user_id']}")
        ]
        deliver_message(user_id, reply_token, p_text + "\n👇 แตะปุ่มเพื่ออนุมัติคนล่าสุดได้เลยครับ:", q_replies)
        return True

    if clean.startswith("อนุมัติ"):
        parts = clean.split()
        if len(parts) == 1:
            target = "latest"
            code = database.get_next_worker_code()
        elif len(parts) == 2:
            arg = parts[1]
            if len(arg) <= 3 and arg.isalnum():
                target = "latest"
                code = arg.upper()
            else:
                target = arg
                code = database.get_next_worker_code()
        else:
            code = parts[-1].upper()
            target = " ".join(parts[1:-1])
            
        updated = database.approve_user(target, code)
        if updated:
            reply_line_message(reply_token, f"✅ อนุมัติเรียบร้อย!\n👤 {updated['display_name']}\n🏷️ ได้รับรหัสพนักงาน: [{code}]")
            push_line_message(updated["user_id"], f"🎉 คุณได้รับการอนุมัติให้ใช้งานระบบแล้วครับ!\n🏷️ รหัสประจำตัวของคุณคือ: [{code}]\n💡 คุณสามารถเริ่มส่งรูปกระดาษบันทึกได้เลยครับ")
        else:
            reply_line_message(reply_token, f"❌ ไม่พบผู้ใช้งานที่รออนุมัติ หรือไม่พบชื่อ '{target}' ในระบบ")
        return True
        
    if clean.startswith("บล็อก"):
        parts = clean.split()
        target = " ".join(parts[1:]) if len(parts) > 1 else "latest"
        blocked = database.block_user(target)
        if blocked:
            reply_line_message(reply_token, f"⛔️ ระงับสิทธิ์การใช้งานของ {blocked['display_name']} เรียบร้อยแล้ว")
        else:
            reply_line_message(reply_token, f"❌ ไม่พบผู้ใช้งานที่ระบุ '{target}'")
        return True

    # Owner Period controls
    if clean == "ปิดงวด":
        closed = database.close_active_period()
        if closed:
            reply_line_message(reply_token, f"🔴 ปิดงวด [{closed['name']}] เรียบร้อยแล้วครับ\nระบบจะไม่รับรูปใหม่จนกว่าจะเปิดงวด\n🌐 {BASE_URL}")
        else:
            reply_line_message(reply_token, f"⚠️ ขณะนี้ไม่มีงวดที่เปิดอยู่ครับ (ปิดอยู่แล้ว)\n🌐 {BASE_URL}")
        return True

    if clean in ["เปิดงวด", "เปิดงวดเดิม", "เปิดงวดเดิมต่อ"]:
        reopened = database.reopen_latest_period()
        if reopened:
            reply_line_message(reply_token, f"🟢 เปิดรับข้อมูลต่อใน [{reopened['name']}] เรียบร้อยแล้วครับ\n🌐 {BASE_URL}")
        else:
            reply_line_message(reply_token, f"⚠️ ไม่พบงวดเดิมในระบบครับ กรุณาเปิดงวดใหม่\n🌐 {BASE_URL}")
        return True

    m_open_new = re.match(r"^เปิดงวดใหม่(?:\s+(.+))?$", clean)
    if m_open_new:
        custom_name = m_open_new.group(1)
        new_p = database.open_new_period(custom_name)
        reply_line_message(reply_token, f"🎉 เปิดงวดใหม่: [{new_p['name']}] เรียบร้อยแล้วครับ!\nพร้อมรับรูปเอกสารเข้าระบบแล้วครับ\n🌐 {BASE_URL}")
        return True

    return False

def handle_text_message(text: str, reply_token: str, user_id: str, is_owner: bool, user_info: dict = None):
    clean_text = text.strip()
    user_info = user_info or {}
    
    # 0. Owner Approval check
    if is_owner and handle_owner_command(clean_text, reply_token, user_id):
        return

    # 1.0. Batch Confirm & Cancel Commands
    if clean_text in ["ยืนยันทั้งหมด", "ยืนยันทุกใบ", "confirm all"]:
        confirmed_list = database.confirm_all_pending_scans(user_id)
        if confirmed_list:
            import gdrive_sync
            gdrive_sync.trigger_db_backup()
            active_p = database.get_active_period()
            p_name = active_p["name"] if active_p else "งวดปัจจุบัน"
            sids = ", ".join(f"[{c['sheet_id']}]" for c in confirmed_list)
            msg = (
                f"✅ ยืนยันบันทึกข้อมูลเรียบร้อยแล้วทั้งหมด {len(confirmed_list)} ใบ!\n"
                f"📋 รายการ: {sids}\n"
                f"📌 ข้อมูลถูกบันทึกลงใน: {p_name}\n\n"
                f"🌐 ดูกระดานสรุปสด: {BASE_URL}"
            )
            reply_line_message(reply_token, msg)
        else:
            reply_line_message(reply_token, "⚠️ ไม่พบรายการที่รอกดยืนยันในขณะนี้ครับ")
        return

    if clean_text in ["ยกเลิกทั้งหมด", "ยกเลิกทุกใบ", "cancel all"]:
        cnt = database.cancel_all_pending_scans(user_id)
        if cnt > 0:
            reply_line_message(reply_token, f"🗑️ ยกเลิกข้อมูลที่รอยืนยันทั้งหมด {cnt} ใบเรียบร้อยแล้วครับ")
        else:
            reply_line_message(reply_token, "⚠️ ไม่พบรายการที่รอยกเลิกในขณะนี้ครับ")
        return

    if clean_text in ["รอยืนยัน", "รายการค้าง", "ค้าง", "pending"]:
        pendings = database.get_all_pending_scans(user_id)
        if not pendings:
            reply_line_message(reply_token, "✅ ไม่มีรายการที่รอกดยืนยันในขณะนี้ครับ ข้อมูลทั้งหมดได้รับการบันทึกเรียบร้อยแล้ว")
            return
        msg = f"📋 [รายการที่รอกดยืนยัน: {len(pendings)} ใบ]\n─────────────────────────\n"
        for idx, p in enumerate(pendings, 1):
            msg += f"{idx}. ใบที่: {p['sheet_id']} (รหัส ID: {p['id']})\n"
        msg += "─────────────────────────\n💡 แตะปุ่มด้านล่างเพื่อยืนยันเฉพาะใบ หรือพิมพ์ 'ยืนยันทั้งหมด' เพื่อบันทึกทุกใบครับ"
        q_items = [
            ("✅ ยืนยันทั้งหมด", "ยืนยันทั้งหมด"),
            ("❌ ยกเลิกทั้งหมด", "ยกเลิกทั้งหมด")
        ]
        for p in pendings[:3]:
            q_items.append((f"✅ ยืนยัน {p['sheet_id']}", f"ยืนยัน {p['id']}"))
        deliver_message(user_id, reply_token, msg, q_items)
        return

    # 1. Confirm Pending Scan: "ยืนยัน 5" or "ยืนยัน"
    m_conf = re.match(r"^ยืนยัน(?:\s+(\d+))?$", clean_text)
    if m_conf:
        scan_id_str = m_conf.group(1)
        if scan_id_str:
            confirmed = database.confirm_pending_scan(int(scan_id_str))
        else:
            pendings = database.get_all_pending_scans(user_id)
            if pendings:
                confirmed = database.confirm_pending_scan(pendings[0]["id"])
            else:
                confirmed = None
            
        if confirmed:
            import gdrive_sync
            gdrive_sync.trigger_db_backup()
            active_p = database.get_active_period()
            p_name = active_p["name"] if active_p else "งวดปัจจุบัน"

            remaining_pendings = database.get_all_pending_scans(user_id)
            rem_hint = ""
            if remaining_pendings:
                rem_hint = f"\n💡 ยังมีอีก {len(remaining_pendings)} ใบที่รอยืนยัน (พิมพ์ 'ยืนยันทั้งหมด' เพื่อบันทึกทีเดียว)"

            msg = (
                f"✅ ยืนยันบันทึกข้อมูลเรียบร้อยแล้วครับ!\n"
                f"📋 [ใบที่: {confirmed['sheet_id']}]\n"
                f"📌 ข้อมูลถูกบันทึกลงใน: {p_name}{rem_hint}\n\n"
                f"🌐 ดูกระดานสรุปสด: {BASE_URL}"
            )
            q_items = [("✅ ยืนยันทั้งหมด", "ยืนยันทั้งหมด")] if remaining_pendings else None
            deliver_message(user_id, reply_token, msg, q_items)
        else:
            reply_line_message(reply_token, "⚠️ ไม่พบรายการที่รอกดยืนยัน หรือรายการนี้ได้รับการบันทึกไปแล้วครับ")
        return

    # 2. Cancel Pending Scan: "ยกเลิก 5" or "ยกเลิก"
    m_canc = re.match(r"^ยกเลิก(?:\s+(\d+))?$", clean_text)
    if m_canc:
        scan_id_str = m_canc.group(1)
        if scan_id_str:
            success = database.cancel_pending_scan(int(scan_id_str))
        else:
            scan = database.get_latest_pending_scan(user_id)
            success = database.cancel_pending_scan(scan["id"]) if scan else False
            
        if success:
            rem = database.get_all_pending_scans(user_id)
            rem_msg = f" (ยังมีรายการอื่นค้างอยู่ {len(rem)} ใบ)" if rem else ""
            reply_line_message(reply_token, f"🗑️ ยกเลิกข้อมูลเรียบร้อยครับ{rem_msg}")
        else:
            reply_line_message(reply_token, "⚠️ ไม่พบรายการที่รอยกเลิกครับ")
        return

    # 3. Single-item Delete: "ลบ 690"
    if handle_delete_command(clean_text, user_id, reply_token):
        return

    # 4. Single-item Edit: "แก้ 370 เป็น 375"
    if handle_edit_command(clean_text, user_id, reply_token):
        return

    # 5. Audit report: "รีเช็ค", "audit", "ตรวจงาน"
    if clean_text in ["รีเช็ค", "audit", "ตรวจงาน", "ตรวจ"]:
        audit_msg = query_service.format_audit_report()
        reply_line_message(reply_token, audit_msg)
        return

    # 6. Search Query command: "เช็ค 310"
    if clean_text.startswith("เช็ค") or clean_text.lower().startswith("check"):
        numbers = re.findall(r"\d+", clean_text)
        if not numbers:
            reply_line_message(reply_token, "⚠️ กรุณาระบุตัวเลขที่ต้องการค้นหา เช่น 'เช็ค 310' หรือ 'เช็ค 401 377'")
            return
        result_msg = query_service.format_search_results(numbers)
        reply_line_message(reply_token, result_msg)
        return
        
    # 7. Status command
    if clean_text in ["สถานะ", "status", "ยอด", "สรุป", "ตาราง", "dashboard"]:
        status_msg = query_service.format_daily_status()
        status_msg += f"\n\n🌐 ดูกระดานสรุป Real-Time:\n{BASE_URL}"
        reply_line_message(reply_token, status_msg)
        return
        
    # 8. Help menu
    if clean_text in ["เมนู", "menu", "help", "?", "วิธีใช้งาน", "คู่มือ", "คำแนะนำ"]:
        help_msg = (
            "📋 เมนูการใช้งานระบบ\n"
            "─────────────────────────\n"
            "1️⃣ ถ่ายรูปกระดาษส่งเข้ามา ➔ ระบบอ่านและส่งสรุปให้ตรวจ\n"
            "2️⃣ แก้ทีละรายการ: พิมพ์ 'แก้ 1234 เป็น 2345' หรือ 'ลบ 1234'\n"
            "3️⃣ แก้หลายรายการ/ย้ายคอลัมน์: คัดลอกข้อความทั้งใบ แก้ไข แล้วส่งกลับ\n"
            "4️⃣ พิมพ์ 'เช็ค [เลข]' ➔ ค้นหาเลขชุดที่ 1 ในงวดนี้\n"
            "5️⃣ พิมพ์ 'รีเช็ค' ➔ ตรวจสอบลำดับแผ่นและแผ่นที่ตกหล่น\n"
            "6️⃣ พิมพ์ 'สถานะ' ➔ ดูยอดรวมและสถานะงวด\n"
            "7️⃣ จัดการหลายรูป: 'รอยืนยัน', 'ยืนยันทั้งหมด', 'ยกเลิกทั้งหมด'\n"
        )
        if is_owner:
            help_msg += (
                "─────────────────────────\n"
                "👑 คำสั่งเจ้าของระบบ:\n"
                "• 'ปิดงวด' / 'เปิดงวด' / 'เปิดงวดใหม่' เพื่อเปิด-ปิดงวด\n"
                "• 'บล็อก [ชื่อ]' เพื่อตัดสิทธิ์\n"
            )
        help_msg += f"─────────────────────────\n🌐 ตารางสรุปสดและเปิดปิดงวด:\n{BASE_URL}"
        reply_line_message(reply_token, help_msg)
        return


    # 9. Check if Worker Pasted an Edited Sheet Text Block
    parsed_sheet = parse_sheet_text(clean_text)
    if parsed_sheet and any(len(items) > 0 for items in parsed_sheet["columns"].values()):
        active_p = database.get_active_period()
        if not active_p:
            reply_line_message(reply_token, f"⛔️ ขณะนี้ระบบปิดรับข้อมูล (ยังไม่เปิดงวดใหม่)\n🌐 {BASE_URL}")
            return

        worker_code = user_info.get("worker_code", "A")
        emp_name = user_info.get("display_name", "พนักงาน")

        # Update existing pending scan, or create a new one — but do NOT confirm yet
        pending_scan = database.get_latest_pending_scan(user_id)
        if pending_scan:
            scan_id = pending_scan["id"]
            sheet_id = pending_scan["sheet_id"]
            database.log_correction(
                scan_id=scan_id,
                sheet_id=sheet_id,
                period_id=pending_scan["period_id"],
                worker_code=worker_code,
                action_type="BULK_EDIT",
                note="พนักงานคัดลอกข้อความไปแก้ไขทั้งใบและส่งกลับมา"
            )
            database.update_pending_scan_items(scan_id, parsed_sheet["columns"])
        else:
            raw_s = parsed_sheet.get("sheet_id") or "1"
            sheet_id = f"{worker_code}-{raw_s}" if not raw_s.startswith(f"{worker_code}-") else raw_s
            val_result = OCRValidator.validate_document({"columns": parsed_sheet["columns"]})
            ocr_obj = {"header": {"sheet_id": sheet_id, "customer_name": emp_name, "date": "", "total_amount": ""}, "columns": parsed_sheet["columns"]}
            scan_id = database.create_pending_scan(user_id, worker_code, emp_name, active_p["id"], sheet_id, ocr_obj, val_result)

        cols = parsed_sheet["columns"]
        top_c = len(cols.get("top", []))
        bot_c = len(cols.get("bottom", []))
        topbot_c = len(cols.get("top_bottom", []))
        total_c = top_c + bot_c + topbot_c

        clean_text_out = format_clean_editable_text(sheet_id, cols)
        preview_msg = (
            f"✏️ ตรวจสอบข้อมูลที่แก้ไขแล้ว:\n"
            f"📋 ใบที่: {sheet_id} | 📊 {total_c} ชุด (บน {top_c} | ล่าง {bot_c} | บนล่าง {topbot_c})\n"
            f"─────────────────────────\n"
            f"{clean_text_out}\n"
            f"─────────────────────────\n"
            f"✅ หากถูกต้อง กดปุ่ม [ ยืนยัน ] เพื่อบันทึกลงระบบครับ"
        )
        quick_replies = [
            ("✅ ยืนยัน", f"ยืนยัน {scan_id}"),
            ("❌ ยกเลิก", f"ยกเลิก {scan_id}")
        ]
        reply_line_message(reply_token, preview_msg, quick_replies)

        return
        
    reply_line_message(
        reply_token,
        f"💡 คุณสามารถถ่ายรูปกระดาษส่งเข้ามาได้เลยครับ\nหรือพิมพ์ 'เช็ค [ตัวเลข]' เพื่อค้นหา\nหรือพิมพ์ 'รีเช็ค' เพื่อตรวจเช็กลำดับกระดาษ\n🌐 {BASE_URL}"
    )

# ==================== WEB DASHBOARD & QUICK-EDITOR ====================
def parse_columns_from_form(form: dict) -> dict:
    new_columns = {"top": [], "bottom": [], "top_bottom": []}
    pattern = re.compile(r"^(top|bottom|top_bottom)_set1_(\w+)$")
    items_by_col = {"top": [], "bottom": [], "top_bottom": []}
    
    for key in form.keys():
        m = pattern.match(key)
        if m:
            col_key = m.group(1)
            row_id = m.group(2)
            s1 = form.get(f"{col_key}_set1_{row_id}", [""])[0].strip()
            s3 = form.get(f"{col_key}_set3_{row_id}", [""])[0].strip()
            s2 = form.get(f"{col_key}_set2_{row_id}", [""])[0].strip()
            box_str = form.get(f"{col_key}_box_{row_id}", [""])[0].strip()
            unc_note = form.get(f"{col_key}_note_{row_id}", [""])[0].strip()
            
            box_2d = []
            if box_str:
                try:
                    box_2d = json.loads(box_str)
                except Exception:
                    box_2d = []
                    
            if s1 or s2:
                items_by_col[col_key].append((row_id, {
                    "set1": s1,
                    "set3": s3,
                    "set2": s2,
                    "raw_text": f"{s1} = {s3 + ' ' if s3 else ''}{s2}".strip(),
                    "box_2d": box_2d,
                    "uncertain_note": unc_note
                }))
                
    for col_key in ["top", "bottom", "top_bottom"]:
        def sort_key(pair):
            try:
                return (0, int(pair[0]))
            except ValueError:
                return (1, str(pair[0]))
        items_by_col[col_key].sort(key=sort_key)
        new_columns[col_key] = [item[1] for item in items_by_col[col_key]]
        
    return new_columns

def render_edit_success_page(sheet_id: str, is_confirmed: bool = False) -> str:
    dest_name = "หน้าหลัก Dashboard" if is_confirmed else "แชท LINE"
    redirect_url = "/?tab=live" if is_confirmed else "https://line.me"
    
    return f"""<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>บันทึกข้อมูลเรียบร้อย</title>
    <script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Prompt:wght@400;500;600;700&display=swap');
        * {{ box-sizing: border-box; font-family: 'Prompt', -apple-system, BlinkMacSystemFont, sans-serif; }}
        body {{
            background: #ECE7DF;
            margin: 0;
            padding: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            color: #2A2421;
        }}
        .card {{
            background: #FFFFFF;
            border: 1px solid #DDD5C7;
            border-radius: 28px;
            padding: 32px 24px;
            max-width: 380px;
            width: 100%;
            text-align: center;
            box-shadow: 0 20px 40px -15px rgba(80, 70, 60, 0.15);
        }}
        .icon-circle {{
            width: 64px;
            height: 64px;
            border-radius: 50%;
            background: #E8F3EB;
            color: #2D6A4F;
            border: 2px solid #CFE4D4;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 32px;
            margin: 0 auto 16px auto;
        }}
        h2 {{
            font-size: 20px;
            font-weight: 700;
            color: #2A2421;
            margin: 0 0 8px 0;
        }}
        p {{
            font-size: 13px;
            color: #7D756D;
            line-height: 1.6;
            margin: 0 0 20px 0;
        }}
        .countdown {{
            display: inline-block;
            background: #F9F6F0;
            border: 1px solid #EBE4D8;
            padding: 6px 14px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
            color: #3A7D58;
            margin-bottom: 24px;
        }}
        .btn {{
            display: block;
            width: 100%;
            padding: 13px;
            border-radius: 16px;
            font-size: 14px;
            font-weight: 700;
            text-decoration: none;
            border: none;
            cursor: pointer;
            transition: all 0.2s;
            margin-bottom: 10px;
        }}
        .btn-primary {{
            background: #3A7D58;
            color: #FFFFFF;
        }}
        .btn-secondary {{
            background: #F4EFE6;
            color: #4A423B;
            border: 1px solid #DDD5C7;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon-circle">✓</div>
        <h2>บันทึกข้อมูลเรียบร้อยแล้ว!</h2>
        <p>ใบที่ <b style="color: #2A2421;">{html_lib.escape(sheet_id)}</b> ได้รับการบันทึกข้อมูลและยืนยันเข้าระบบเรียบร้อยแล้ว</p>
        
        <div class="countdown" id="countdownBox">
            ⏱️ กำลังปิดหน้าต่างอัตโนมัติใน <span id="sec">2</span> วิ...
        </div>

        <button onclick="doClose()" class="btn btn-primary">
            ✕ ปิดหน้าต่างทันที
        </button>
        <a href="{redirect_url}" class="btn btn-secondary">
            ↩️ กลับสู่ {dest_name}
        </a>
    </div>

    <script>
        function doClose() {{
            if (window.liff && typeof liff.closeWindow === 'function') {{
                try {{ liff.closeWindow(); }} catch(e) {{}}
            }}
            try {{ window.close(); }} catch(e) {{}}
            window.location.href = "{redirect_url}";
        }}

        var timeLeft = 2;
        var timer = setInterval(function() {{
            timeLeft--;
            var secEl = document.getElementById('sec');
            if (secEl) secEl.innerText = timeLeft;
            if (timeLeft <= 0) {{
                clearInterval(timer);
                doClose();
            }}
        }}, 800);
    </script>
</body>
</html>"""

def render_split_editor(data_dict: dict, form_action: str, is_confirmed: bool = False, cancel_url: str = "") -> str:
    sheet_id = data_dict.get("sheet_id", "TEST-01")
    emp_name = data_dict.get("emp_name", "") or data_dict.get("employee_name", "")
    worker_code = data_dict.get("worker_code", "")
    period_name = data_dict.get("period_name", "") or "งวดปัจจุบัน"
    raw_img = data_dict.get("image_path", "")
    
    img_url = f"/{raw_img}" if raw_img.startswith("uploads/") else (raw_img if raw_img.startswith("/uploads/") else "")
    if not cancel_url:
        cancel_url = "/" if is_confirmed else "https://line.me"
        
    columns = data_dict.get("columns", {})
    
    total_items = 0
    initial_total = 0.0
    for col_key, items in columns.items():
        for itm in items:
            s1 = itm.get("set1", "")
            s2 = itm.get("set2", "")
            if s1 or s2:
                total_items += 1
                if "x" in s2.lower():
                    for p in s2.lower().split("x"):
                        cleaned = re.sub(r"[^\d.]", "", p)
                        if cleaned:
                            try: initial_total += float(cleaned)
                            except: pass
                else:
                    cleaned = re.sub(r"[^\d.]", "", s2)
                    if cleaned:
                        try: initial_total += float(cleaned)
                        except: pass
                        
    uncertain_items = []
    first_item_info = None
    
    for col_key in ["top", "bottom", "top_bottom"]:
        items = columns.get(col_key, [])
        for idx, itm in enumerate(items):
            uid = f"{col_key}_{idx}"
            box = itm.get("box_2d") or []
            s1 = itm.get("set1", "")
            s2 = itm.get("set2", "")
            s3 = itm.get("set3", "")
            note = itm.get("uncertain_note", "")
            is_val = itm.get("is_valid", True)
            label = f"{s1} = {s3 + ' ' if s3 else ''}{s2}".strip()
            
            if not first_item_info and (s1 or s2):
                first_item_info = (uid, box, label)
                
            if note or not is_val:
                uncertain_items.append({
                    "uid": uid,
                    "col_key": col_key,
                    "col_label": "บน" if col_key == "top" else ("ล่าง" if col_key == "bottom" else "บนล่าง"),
                    "idx": idx,
                    "set1": s1,
                    "set2": s2,
                    "set3": s3,
                    "label": label,
                    "box": box,
                    "note": note or "ตัวเลขไม่ชัดเจน โปรดตรวจสอบ"
                })

    urgent_html = ""
    if uncertain_items:
        cards_html = ""
        for u in uncertain_items:
            box_json_str = json.dumps(u["box"])
            chips_html = ""
            found_nums = re.findall(r"\b\d+\b", u["note"])
            for fn in found_nums[:2]:
                chips_html += f"""<button type="button" onclick="applyQuickFix('{u['uid']}', '{fn}')" class="text-[11px] font-bold text-[#8F6E14] bg-[#FEF9EA] hover:bg-[#FDF0D5] border border-[#F6ECCB] px-2.5 py-0.5 rounded-lg cursor-pointer transition-all">แก้เป็น {fn}</button>"""
                
            cards_html += f"""
            <div class="bg-[#FFFFFF] border border-[#FCD34D] rounded-xl p-2.5 flex items-center justify-between gap-2 shadow-xs">
                <div class="flex items-center gap-2">
                    <span class="text-[10px] font-bold text-[#B45309] bg-[#FEF3C7] px-2 py-0.5 rounded-md border border-[#FDE68A]">[{u['col_label']}]</span>
                    <div>
                        <div class="text-xs font-bold text-[#2A2421] font-mono">{html_lib.escape(u['label'])}</div>
                        <div class="text-[11px] text-[#B45309] font-medium">{html_lib.escape(u['note'])}</div>
                    </div>
                </div>
                <div class="flex items-center gap-1.5 shrink-0">
                    {chips_html}
                    <button type="button" onclick="triggerRowFocus('{u['uid']}', {box_json_str}, '{html_lib.escape(u['label'])}'); document.getElementById('inputNum-{u['uid']}').focus();" class="text-xs font-semibold bg-[#3A7D58] hover:bg-[#326C4C] text-[#FFFFFF] px-2.5 py-1 rounded-xl shadow-xs cursor-pointer transition-all">
                        🔍 ส่องจุดนี้
                    </button>
                </div>
            </div>
            """
            
        urgent_html = f"""
        <div class="bg-[#FEF3C7]/90 border border-[#FCD34D] rounded-2xl p-3 shadow-xs space-y-2 mb-3">
            <div class="flex items-center justify-between text-xs">
                <span class="font-bold text-[#B45309] flex items-center gap-1.5">
                    <span>⚠️</span> รายการที่ระบบไม่มั่นใจ ({len(uncertain_items)} รายการ)
                </span>
                <span class="text-[10px] text-[#92400E] font-medium">ส่องเทียบกับภาพด้านบน</span>
            </div>
            <div class="space-y-1.5">
                {cards_html}
            </div>
        </div>
        """

    cat_sections_html = ""
    cat_configs = [
        ("top", "หมวด [บน]", "#2D6A4F", "#E8F3EB", "#CFE4D4", "#3A7D58"),
        ("bottom", "หมวด [ล่าง]", "#6B5384", "#F3EFF8", "#E3D9ED", "#6B5384"),
        ("top_bottom", "หมวด [บนล่าง]", "#B85D19", "#FDF2EA", "#F7DFD2", "#B85D19")
    ]
    
    global_row_counter = 0
    for col_key, col_title, text_col, bg_col, border_col, accent_col in cat_configs:
        items = columns.get(col_key, [])
        rows_inner_html = ""
        
        for idx, itm in enumerate(items):
            global_row_counter += 1
            uid = f"{col_key}_{idx}"
            s1 = itm.get("set1", "")
            s2 = itm.get("set2", "")
            s3 = itm.get("set3", "")
            box = itm.get("box_2d") or []
            note = itm.get("uncertain_note", "")
            box_json = json.dumps(box)
            label = f"{s1} = {s3 + ' ' if s3 else ''}{s2}".strip()
            
            s3_input = f"""<input type="text" name="{col_key}_set3_{uid}" id="inputMod-{uid}" value="{html_lib.escape(s3)}" placeholder="ก3" class="w-12 bg-[#FEF9EA] border border-[#F6ECCB] text-[#8F6E14] font-bold text-center text-xs rounded-xl py-1.5 outline-none">"""
            
            rows_inner_html += f"""
            <div id="rowCard-{uid}" class="row-card bg-[#FFFFFF] border border-[#EDE7DD] hover:border-[#CFE4D4] rounded-2xl p-2.5 flex items-center justify-between shadow-xs transition-all">
                <span class="w-6 text-xs text-[#8A8279] font-mono text-center">#{global_row_counter}</span>
                <div class="flex items-center gap-2 flex-1 px-1">
                    <input type="text" name="{col_key}_set1_{uid}" id="inputNum-{uid}" value="{html_lib.escape(s1)}" placeholder="เลข" inputmode="numeric" onfocus="triggerRowFocus('{uid}', {box_json}, '{html_lib.escape(label)}')" oninput="onDataChanged()" class="w-20 bg-[#F9F6F0] focus:bg-[#FFFFFF] border border-[#DDD5C7] focus:border-[{accent_col}] text-[#2A2421] font-mono font-bold text-center text-base rounded-xl py-1.5 outline-none">
                    <span class="text-[#A8A095] font-bold">=</span>
                    {s3_input}
                    <input type="text" name="{col_key}_set2_{uid}" id="inputPrice-{uid}" value="{html_lib.escape(s2)}" placeholder="ยอดเงิน" inputmode="numeric" onfocus="triggerRowFocus('{uid}', {box_json}, '{html_lib.escape(label)}')" oninput="onDataChanged()" class="flex-1 bg-[#F9F6F0] focus:bg-[#FFFFFF] border border-[#DDD5C7] focus:border-[{accent_col}] text-[#2D6A4F] font-mono font-bold text-center text-base rounded-xl py-1.5 outline-none">
                </div>
                <input type="hidden" name="{col_key}_box_{uid}" value="{html_lib.escape(json.dumps(box))}">
                <input type="hidden" name="{col_key}_note_{uid}" value="{html_lib.escape(note)}">
                <button type="button" onclick="deleteRow('{uid}')" title="ลบรายการนี้" class="w-8 h-8 rounded-lg text-slate-400 hover:text-rose-500 hover:bg-rose-50 flex items-center justify-center text-sm cursor-pointer transition-colors">
                    ✕
                </button>
            </div>
            """
            
        cat_sections_html += f"""
        <div class="space-y-2 pt-2">
            <div class="flex items-center justify-between">
                <div class="text-[11px] font-bold px-2.5 py-1 rounded-lg inline-block border" style="color: {text_col}; background: {bg_col}; border-color: {border_col};">
                    {col_title} ({len(items)} รายการ)
                </div>
            </div>
            <div id="rowsContainer-{col_key}" class="space-y-2">
                {rows_inner_html}
            </div>
            <button type="button" onclick="addNewRow('{col_key}', '{col_title}')" class="w-full py-2 rounded-2xl bg-[#FFFFFF] hover:bg-[#F4EFE6] text-[#3A7D58] border border-dashed border-[#CFE4D4] font-semibold text-xs flex items-center justify-center gap-1.5 shadow-xs transition-all cursor-pointer">
                <span>➕ เพิ่มรายการใน{col_title}</span>
            </button>
        </div>
        """

    if img_url:
        image_element_html = f"""
        <div id="imageContainer" style="position: absolute; transform-origin: 0 0; transition: transform 0.15s ease-out;">
            <img id="realPaperImg" src="{img_url}" class="pointer-events-none select-none" style="display: block; max-width: none;" onload="initImageDimensions()">
            <div id="spotlightRing" class="spotlight-box" style="position: absolute; display: none; pointer-events: none;"></div>
        </div>
        """
    else:
        image_element_html = f"""
        <div class="text-center p-6 text-[#7D756D]">
            <div class="text-3xl mb-1">📷</div>
            <div class="text-xs font-bold text-[#2A2421]">ไม่มีไฟล์ภาพถ่ายในระบบ</div>
            <div class="text-[11px] text-[#8A8279] mt-0.5">สามารถตรวจสอบและแก้ไขตัวเลขในแบบฟอร์มด้านล่างได้ทันที</div>
        </div>
        """

    if uncertain_items:
        u = uncertain_items[0]
        init_script = f"triggerRowFocus('{u['uid']}', {json.dumps(u['box'])}, '{html_lib.escape(u['label'])}');"
    elif first_item_info:
        init_script = f"triggerRowFocus('{first_item_info[0]}', {json.dumps(first_item_info[1])}, '{html_lib.escape(first_item_info[2])}');"
    else:
        init_script = "resetPaperZoom();"

    status_badge_html = f"""<span class="text-[10px] font-semibold text-[#2D6A4F] bg-[#E8F3EB] px-2 py-0.5 rounded-full border border-[#CFE4D4]">บันทึกแล้ว</span>""" if is_confirmed else f"""<span class="text-[10px] font-semibold text-[#B45309] bg-[#FEF3C7] px-2 py-0.5 rounded-full border border-[#FCD34D]">รอการยืนยัน</span>"""

    return f"""<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>ตรวจสอบ/แก้ไข - ใบที่ {html_lib.escape(sheet_id)}</title>
    <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
    <script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Prompt:wght@300;400;500;600;700&family=JetBrains+Mono:wght@500;600;700;800&display=swap');
        * {{ font-family: 'Prompt', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; -webkit-tap-highlight-color: transparent; }}
        .font-mono {{ font-family: 'JetBrains Mono', monospace; font-feature-settings: 'tnum' on, 'lnum' on; }}
        ::-webkit-scrollbar {{ width: 4px; height: 4px; }}
        ::-webkit-scrollbar-track {{ background: transparent; }}
        ::-webkit-scrollbar-thumb {{ background: #D8D0C3; border-radius: 4px; }}
        
        .phone-frame {{
            max-width: 440px;
            margin: 0 auto;
            border-radius: 36px;
            box-shadow: 0 25px 60px -15px rgba(80, 70, 60, 0.22), 0 0 0 1px #DFD7CB;
            overflow: hidden;
            min-height: 860px;
            height: 94vh;
            position: relative;
            background: #F9F6F0;
            display: flex;
            flex-direction: column;
        }}
        
        .spotlight-box {{
            border: 2.5px solid #22c55e;
            background: rgba(34, 197, 94, 0.16);
            border-radius: 6px;
            box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.25);
            transition: all 0.25s cubic-bezier(0.2, 0.8, 0.2, 1);
        }}
        
        .row-focused {{
            border-color: #3A7D58 !important;
            background-color: #F2FAF5 !important;
            box-shadow: 0 0 0 2px rgba(58, 125, 88, 0.25) !important;
        }}
    </style>
</head>
<body class="bg-[#ECE7DF] text-[#2A2421] antialiased min-h-screen p-0 md:p-4 flex flex-col items-center">

    <div class="hidden md:flex w-full max-w-xl mb-4 items-center justify-between bg-[#FFFFFF] border border-[#DDD5C7] px-4 py-2 rounded-2xl shadow-xs">
        <div class="flex items-center gap-2">
            <span class="w-2.5 h-2.5 rounded-full bg-[#3A7D58]"></span>
            <span class="text-xs font-semibold text-[#2A2421]">ระบบตรวจสอบ/แก้ไขตัวเลข (ภาพถ่ายจริง + ไฮไลท์แม่นยำ)</span>
        </div>
        <div class="flex items-center gap-2">
            <button type="button" id="btnToggleFrame" onclick="togglePhoneFrame()" class="text-xs font-medium px-3 py-1.5 rounded-xl bg-[#F4EFE6] hover:bg-[#EAE4D8] text-[#4A423B] border border-[#DDD5C7] transition-all cursor-pointer">
                <span id="frameText">สลับดูแบบเต็มจอ (Desktop)</span>
            </button>
        </div>
    </div>

    <!-- Container Frame -->
    <div id="appContainer" class="phone-frame w-full text-[#2A2421]">
        
        <!-- Header -->
        <header class="bg-[#FFFFFF] border-b border-[#EBE4D8] px-4 py-3 flex items-center justify-between shrink-0 shadow-xs z-20">
            <div class="flex items-center gap-2.5">
                <a href="{cancel_url}" onclick="tryClose()" class="w-8 h-8 rounded-xl bg-[#F4EFE6] flex items-center justify-center text-[#5C544C] hover:text-[#2A2421] transition-colors text-sm text-decoration-none">
                    ✕
                </a>
                <div>
                    <div class="text-[11px] font-semibold text-[#6D655E]">ตรวจสอบและแก้ไขโพย</div>
                    <div class="text-sm font-bold text-[#2A2421] flex items-center gap-1.5">
                        <span>ใบที่: {html_lib.escape(sheet_id)}</span>
                        {status_badge_html}
                    </div>
                </div>
            </div>
            <div class="text-right">
                <span class="text-[11px] font-medium text-[#7D756D]">{html_lib.escape(period_name)}</span>
                <div class="text-xs font-bold text-[#3A7D58] font-mono">{html_lib.escape(emp_name or worker_code or "ผู้ส่ง")}</div>
            </div>
        </header>

        <!-- 1. STICKY REAL PAPER VIEWER (Top 260px) -->
        <div class="bg-[#E5DFD5] border-b border-[#DDD5C7] shrink-0 relative overflow-hidden flex flex-col" style="height: 260px;">
            <div class="absolute top-2.5 left-3 right-3 z-10 flex items-center justify-between pointer-events-none">
                <div class="inline-flex items-center gap-1.5 bg-[#FFFFFF]/95 backdrop-blur-xs px-2.5 py-1 rounded-xl border border-[#DDD5C7] shadow-xs pointer-events-auto">
                    <span class="w-2 h-2 rounded-full bg-[#22c55e] animate-pulse"></span>
                    <span id="spotlightTargetText" class="text-[11px] font-semibold text-[#2A2421]">ส่องจุด: กำลังโหลด</span>
                </div>
                <div class="flex items-center gap-1 bg-[#FFFFFF]/95 backdrop-blur-xs p-1 rounded-xl border border-[#DDD5C7] shadow-xs pointer-events-auto">
                    <button type="button" onclick="zoomPaper(0.3)" title="ซูมเข้า" class="w-7 h-7 rounded-lg bg-[#F9F6F0] hover:bg-[#EAE4D8] text-xs font-bold text-[#2A2421] flex items-center justify-center cursor-pointer">➕</button>
                    <button type="button" onclick="zoomPaper(-0.3)" title="ซูมออก" class="w-7 h-7 rounded-lg bg-[#F9F6F0] hover:bg-[#EAE4D8] text-xs font-bold text-[#2A2421] flex items-center justify-center cursor-pointer">➖</button>
                    <button type="button" onclick="resetPaperZoom()" title="ดูทั้งใบ" class="w-7 h-7 rounded-lg bg-[#F9F6F0] hover:bg-[#EAE4D8] text-xs text-[#2A2421] flex items-center justify-center cursor-pointer">🔄</button>
                </div>
            </div>

            <!-- Viewport -->
            <div id="paperViewport" class="w-full h-full relative overflow-hidden flex items-center justify-center cursor-grab select-none">
                {image_element_html}
            </div>
        </div>

        <!-- 2. SCROLLABLE EDITING FORM -->
        <form id="editForm" method="POST" action="{form_action}" class="flex-1 overflow-y-auto pb-24 p-3 space-y-3">
            {urgent_html}
            {cat_sections_html}
            
            <!-- Bottom Action Bar (Inside Form for easy submission) -->
            <footer class="fixed md:absolute bottom-0 left-0 right-0 bg-[#FFFFFF] border-t border-[#EBE4D8] px-4 py-3 shadow-lg z-30 flex flex-col gap-2">
                <div class="flex items-center justify-between text-xs px-0.5">
                    <div class="text-[#7D756D]">
                        สรุปรวม: <span id="footerCountText" class="font-bold text-[#2A2421] font-mono">{total_items}</span> ชุด
                    </div>
                    <div class="text-[#7D756D]">
                        ยอดรวมสุทธิ: <span id="footerTotalText" class="font-bold text-[#2D6A4F] font-mono text-sm">{initial_total:,.0f}</span> <span class="font-normal text-[#2D6A4F]">฿</span>
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    <a href="{cancel_url}" onclick="tryClose()" class="w-24 py-2.5 rounded-2xl bg-[#FDF0F1] hover:bg-[#FCE3E5] text-[#A84357] border border-[#F7D5D9] text-xs font-semibold transition-all cursor-pointer text-center text-decoration-none">
                        ยกเลิก
                    </a>
                    <button type="submit" class="flex-1 py-2.5 rounded-2xl bg-[#3A7D58] hover:bg-[#326C4C] text-[#FFFFFF] text-xs font-bold shadow-md transition-all active:scale-[0.98] flex items-center justify-center gap-1.5 cursor-pointer">
                        <span>✅ บันทึกและยืนยันข้อมูล</span>
                    </button>
                </div>
            </footer>
        </form>

    </div>

    <!-- Toast -->
    <div id="toast" class="fixed top-4 z-50 bg-[#2A2421] text-[#F9F6F0] text-xs px-4 py-2 rounded-2xl shadow-lg opacity-0 transform -translate-y-3 transition-all pointer-events-none">
        ข้อความแจ้งเตือน
    </div>

    <script>
        var currentZoom = 2.4;
        var currentPanX = 0;
        var currentPanY = 0;
        var isPanning = false;
        var panStartX = 0;
        var panStartY = 0;

        function togglePhoneFrame() {{
            var app = document.getElementById('appContainer');
            var txt = document.getElementById('frameText');
            if (app.classList.contains('phone-frame')) {{
                app.classList.remove('phone-frame');
                app.classList.add('max-w-4xl', 'rounded-3xl', 'shadow-sm', 'min-h-[850px]', 'h-auto');
                txt.innerText = 'สลับดูแบบจอมือถือ (Mobile)';
            }} else {{
                app.classList.add('phone-frame');
                app.classList.remove('max-w-4xl', 'rounded-3xl', 'shadow-sm', 'min-h-[850px]', 'h-auto');
                txt.innerText = 'สลับดูแบบเต็มจอ (Desktop)';
            }}
        }}

        function tryClose() {{
            if (window.liff && typeof liff.closeWindow === 'function') {{
                try {{ liff.closeWindow(); }} catch(e) {{}}
            }}
            try {{ window.close(); }} catch(e) {{}}
        }}

        function applyPaperTransform() {{
            var container = document.getElementById('imageContainer');
            if (container) {{
                container.style.transform = 'translate(' + currentPanX + 'px, ' + currentPanY + 'px) scale(' + currentZoom + ')';
            }}
        }}

        function zoomPaper(delta) {{
            currentZoom = Math.max(0.6, Math.min(5.0, currentZoom + delta));
            applyPaperTransform();
        }}

        function initImageDimensions() {{
            var img = document.getElementById('realPaperImg');
            var cont = document.getElementById('imageContainer');
            if (!img || !cont) return;
            var nw = img.naturalWidth || 500;
            var nh = img.naturalHeight || 500;
            var baseWidth = 480;
            var baseHeight = (nh / nw) * baseWidth;
            cont.style.width = baseWidth + 'px';
            cont.style.height = baseHeight + 'px';
            resetPaperZoom();
        }}

        function resetPaperZoom() {{
            var vp = document.getElementById('paperViewport');
            var cont = document.getElementById('imageContainer');
            if (!vp || !cont) return;
            var vpW = vp.clientWidth || 400;
            var vpH = vp.clientHeight || 260;
            var contW = cont.offsetWidth || 480;
            var contH = cont.offsetHeight || 480;

            currentZoom = Math.min(vpW / contW, vpH / contH) * 0.95;
            currentPanX = (vpW - (contW * currentZoom)) / 2;
            currentPanY = (vpH - (contH * currentZoom)) / 2;
            applyPaperTransform();
            
            var ring = document.getElementById('spotlightRing');
            if (ring) ring.style.display = 'none';
            var txt = document.getElementById('spotlightTargetText');
            if (txt) txt.innerText = 'ภาพรวมทั้งใบ';
        }}

        function triggerRowFocus(rowId, boxCoords, label) {{
            var vp = document.getElementById('paperViewport');
            var cont = document.getElementById('imageContainer');
            if (!vp || !cont) return;
            var vpW = vp.clientWidth || 400;
            var vpH = vp.clientHeight || 260;
            var contW = cont.offsetWidth || 480;
            var contH = cont.offsetHeight || 480;

            var ring = document.getElementById('spotlightRing');
            if (boxCoords && boxCoords.length === 4 && ring) {{
                var ymin = boxCoords[0];
                var xmin = boxCoords[1];
                var ymax = boxCoords[2];
                var xmax = boxCoords[3];

                ring.style.top = (ymin / 10.0) + '%';
                ring.style.left = (xmin / 10.0) + '%';
                ring.style.height = ((ymax - ymin) / 10.0) + '%';
                ring.style.width = ((xmax - xmin) / 10.0) + '%';
                ring.style.display = 'block';

                currentZoom = 2.4;
                var targetX = ((xmin + xmax) / 2000.0) * contW;
                var targetY = ((ymin + ymax) / 2000.0) * contH;

                currentPanX = (vpW / 2) - (targetX * currentZoom);
                currentPanY = (vpH / 2) - (targetY * currentZoom);
                applyPaperTransform();
            }}

            if (label) {{
                var txt = document.getElementById('spotlightTargetText');
                if (txt) txt.innerText = 'ส่องจุด: ' + label;
            }}

            var cards = document.querySelectorAll('.row-card');
            cards.forEach(function(c) {{ c.classList.remove('row-focused'); }});
            var activeCard = document.getElementById('rowCard-' + rowId);
            if (activeCard) activeCard.classList.add('row-focused');
        }}

        function applyQuickFix(rowId, val) {{
            var inp = document.getElementById('inputNum-' + rowId);
            if (inp) {{
                inp.value = val;
                onDataChanged();
                showToast('แก้ไขตัวเลขเป็น ' + val + ' เรียบร้อย');
            }}
        }}

        function deleteRow(rowId) {{
            var card = document.getElementById('rowCard-' + rowId);
            if (card) {{
                card.style.opacity = '0';
                card.style.transform = 'scale(0.95)';
                setTimeout(function() {{
                    card.remove();
                    onDataChanged();
                    showToast('ลบรายการเรียบร้อย');
                }}, 150);
            }}
        }}

        function addNewRow(colKey, colName) {{
            var newId = 'new_' + Date.now();
            var container = document.getElementById('rowsContainer-' + colKey);
            if (!container) return;
            
            var newCard = document.createElement('div');
            newCard.id = 'rowCard-' + newId;
            newCard.className = 'row-card bg-[#FFFFFF] border-2 border-dashed border-[#3A7D58] rounded-2xl p-2.5 flex items-center justify-between shadow-xs transition-all animate-fade-in';
            
            newCard.innerHTML = 
                '<span class="w-6 text-xs text-[#8A8279] font-mono text-center">+</span>' +
                '<div class="flex items-center gap-2 flex-1 px-1">' +
                    '<input type="text" name="' + colKey + '_set1_' + newId + '" id="inputNum-' + newId + '" placeholder="เลข" inputmode="numeric" oninput="onDataChanged()" class="w-20 bg-[#F9F6F0] focus:bg-[#FFFFFF] border border-[#DDD5C7] focus:border-[#3A7D58] text-[#2A2421] font-mono font-bold text-center text-base rounded-xl py-1.5 outline-none">' +
                    '<span class="text-[#A8A095] font-bold">=</span>' +
                    '<input type="text" name="' + colKey + '_set3_' + newId + '" id="inputMod-' + newId + '" placeholder="ก3" class="w-12 bg-[#FEF9EA] border border-[#F6ECCB] text-[#8F6E14] font-bold text-center text-xs rounded-xl py-1.5 outline-none">' +
                    '<input type="text" name="' + colKey + '_set2_' + newId + '" id="inputPrice-' + newId + '" placeholder="ยอดเงิน" inputmode="numeric" oninput="onDataChanged()" class="flex-1 bg-[#F9F6F0] focus:bg-[#FFFFFF] border border-[#DDD5C7] focus:border-[#3A7D58] text-[#2D6A4F] font-mono font-bold text-center text-base rounded-xl py-1.5 outline-none">' +
                '</div>' +
                '<input type="hidden" name="' + colKey + '_box_' + newId + '" value="[]">' +
                '<input type="hidden" name="' + colKey + '_note_' + newId + '" value="">' +
                '<button type="button" onclick="deleteRow(\'' + newId + '\')" class="w-8 h-8 rounded-lg text-slate-400 hover:text-rose-500 hover:bg-rose-50 flex items-center justify-center text-sm cursor-pointer transition-colors">✕</button>';
                
            container.appendChild(newCard);
            onDataChanged();
            var inp = document.getElementById('inputNum-' + newId);
            if (inp) inp.focus();
            showToast('เพิ่มรายการใหม่ใน ' + colName);
        }}

        function onDataChanged() {{
            var total = 0;
            var count = 0;
            var inputs = document.querySelectorAll('input[id^="inputPrice-"]');
            inputs.forEach(function(inp) {{
                var val = (inp.value || '').trim();
                var numInp = document.getElementById(inp.id.replace('inputPrice-', 'inputNum-'));
                var numVal = numInp ? (numInp.value || '').trim() : '';
                
                if (numVal || val) {{
                    count++;
                }}
                if (!val) return;
                if (val.indexOf('x') !== -1 || val.indexOf('X') !== -1) {{
                    var parts = val.toLowerCase().split('x');
                    parts.forEach(function(p) {{
                        var num = parseFloat(p.replace(/[^0-9.]/g, '')) || 0;
                        total += num;
                    }});
                }} else {{
                    var num = parseFloat(val.replace(/[^0-9.]/g, '')) || 0;
                    total += num;
                }}
            }});
            var cntEl = document.getElementById('footerCountText');
            if (cntEl) cntEl.innerText = count;
            var totEl = document.getElementById('footerTotalText');
            if (totEl) totEl.innerText = total.toLocaleString();
        }}

        function showToast(msg) {{
            var toast = document.getElementById('toast');
            toast.innerText = msg;
            toast.classList.remove('opacity-0', '-translate-y-3');
            toast.classList.add('opacity-100', 'translate-y-0');
            setTimeout(function() {{
                toast.classList.remove('opacity-100', 'translate-y-0');
                toast.classList.add('opacity-0', '-translate-y-3');
            }}, 2000);
        }}

        window.addEventListener('DOMContentLoaded', function() {{
            setTimeout(function() {{
                {init_script}
            }}, 250);

            var vp = document.getElementById('paperViewport');
            if (!vp) return;

            vp.addEventListener('mousedown', function(e) {{
                isPanning = true;
                panStartX = e.clientX - currentPanX;
                panStartY = e.clientY - currentPanY;
                vp.style.cursor = 'grabbing';
            }});

            window.addEventListener('mousemove', function(e) {{
                if (!isPanning) return;
                currentPanX = e.clientX - panStartX;
                currentPanY = e.clientY - panStartY;
                applyPaperTransform();
            }});

            window.addEventListener('mouseup', function() {{
                isPanning = false;
                if (vp) vp.style.cursor = 'grab';
            }});

            vp.addEventListener('touchstart', function(e) {{
                if (e.touches.length === 1) {{
                    isPanning = true;
                    panStartX = e.touches[0].clientX - currentPanX;
                    panStartY = e.touches[0].clientY - currentPanY;
                }}
            }}, {{ passive: true }});

            window.addEventListener('touchmove', function(e) {{
                if (!isPanning || e.touches.length !== 1) return;
                currentPanX = e.touches[0].clientX - panStartX;
                currentPanY = e.touches[0].clientY - panStartY;
                applyPaperTransform();
            }}, {{ passive: true }});

            window.addEventListener('touchend', function() {{
                isPanning = false;
            }});
        }});
    </script>
</body>
</html>"""

def render_edit_page(scan_id: int) -> str:
    scan = database.get_pending_scan(scan_id)
    if not scan:
        return f"""<!DOCTYPE html>
<html lang="th"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>ไม่พบข้อมูล</title>
<style>body{{font-family:sans-serif;background:#ECE7DF;padding:40px;text-align:center;color:#2A2421;}}
.card{{background:white;padding:30px;border-radius:24px;max-width:400px;margin:auto;box-shadow:0 4px 12px rgba(0,0,0,0.08);}}
a{{display:inline-block;margin-top:16px;padding:10px 20px;background:#3A7D58;color:white;text-decoration:none;border-radius:12px;font-weight:700;}}
</style></head><body><div class="card">
    <h2 style="color:#A84357;margin-top:0;">❌ ไม่พบรายการนี้</h2>
    <p style="color:#7D756D;font-size:14px;">รายการสแกนนี้อาจถูกยกเลิกหรือไม่มีอยู่ในระบบ</p>
    <a href="https://line.me">กลับไปที่ LINE</a>
</div></body></html>"""

    if scan["status"] != "PENDING":
        sheet_id = scan.get("sheet_id", "")
        edit_link_html = f'<div style="margin-top:12px;"><a href="/sheet/edit/{sheet_id}" style="background:#3A7D58;color:white;padding:10px 20px;border-radius:12px;text-decoration:none;font-weight:700;display:inline-block;">✏️ แก้ไขข้อมูลใบนี้ย้อนหลัง</a></div>' if sheet_id else ''
        return f"""<!DOCTYPE html>
<html lang="th"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>สถานะรายการ</title>
<style>body{{font-family:sans-serif;background:#ECE7DF;padding:40px;text-align:center;color:#2A2421;}}
.card{{background:white;padding:30px;border-radius:24px;max-width:400px;margin:auto;box-shadow:0 4px 12px rgba(0,0,0,0.08);}}
a{{display:inline-block;margin-top:16px;padding:10px 20px;background:#64748b;color:white;text-decoration:none;border-radius:12px;font-weight:700;}}
</style></head><body><div class="card">
    <h2 style="color:#B45309;margin-top:0;">⚠️ รายการนี้ได้รับการยืนยันแล้ว</h2>
    <p style="color:#7D756D;font-size:14px;">รายการนี้อยู่ในสถานะ '{scan['status']}' แล้ว</p>
    {edit_link_html}
    <div style="margin-top:8px;"><a href="https://line.me">กลับไปที่ LINE</a></div>
</div></body></html>"""

    val_data = json.loads(scan["val_json"]) if scan.get("val_json") else {}
    cols = val_data.get("validated_columns", {})
    
    period = database.get_period_by_id(scan.get("period_id")) if scan.get("period_id") else None
    period_name = period["name"] if period else "งวดปัจจุบัน"
    
    data_dict = {
        "sheet_id": scan.get("sheet_id", ""),
        "emp_name": scan.get("emp_name", ""),
        "worker_code": scan.get("worker_code", ""),
        "period_name": period_name,
        "image_path": scan.get("image_path", ""),
        "columns": cols
    }
    return render_split_editor(data_dict, form_action=f"/edit/{scan_id}", is_confirmed=False, cancel_url="https://line.me")

def render_confirmed_sheet_edit_page(sheet_id_or_db_id: str) -> str:
    data = database.get_sheet_for_edit(sheet_id_or_db_id)
    if not data:
        return f"""<!DOCTYPE html>
<html lang="th"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>ไม่พบข้อมูล</title>
<style>body{{font-family:sans-serif;background:#ECE7DF;padding:40px;text-align:center;color:#2A2421;}}
.card{{background:white;padding:30px;border-radius:24px;max-width:400px;margin:auto;box-shadow:0 4px 12px rgba(0,0,0,0.08);}}
a{{display:inline-block;margin-top:16px;padding:10px 20px;background:#3A7D58;color:white;text-decoration:none;border-radius:12px;font-weight:700;}}
</style></head><body><div class="card">
    <h2 style="color:#A84357;margin-top:0;">❌ ไม่พบใบนี้ในระบบ</h2>
    <p style="color:#7D756D;font-size:14px;">ไม่พบข้อมูลของใบที่ <b>{html_lib.escape(str(sheet_id_or_db_id))}</b> หรืออาจยังไม่ได้บันทึกเข้าระบบ</p>
    <a href="/">กลับสู่หน้าหลัก Dashboard</a>
</div></body></html>"""

    sheet = data["sheet"]
    cols = data["columns"]
    data_dict = {
        "sheet_id": sheet["sheet_id"],
        "employee_name": sheet.get("employee_name", ""),
        "worker_code": sheet.get("worker_code", ""),
        "period_name": sheet.get("period_name") or "งวดปัจจุบัน",
        "image_path": sheet.get("image_path", ""),
        "columns": cols
    }
    return render_split_editor(data_dict, form_action=f"/sheet/edit/{sheet['sheet_id']}", is_confirmed=True, cancel_url="/")

def render_html_dashboard(period_id: Optional[int] = None, scope: str = "period", active_tab: str = "overview") -> str:
    all_periods = database.get_all_periods()
    active_period = database.get_active_period()
    latest_period = database.get_latest_period()
    
    if period_id:
        selected_p = next((p for p in all_periods if p["id"] == period_id), latest_period)
    else:
        selected_p = active_period or latest_period
        
    p_id = selected_p["id"] if selected_p else 1
    p_name = selected_p["name"] if selected_p else "งวดปัจจุบัน"
    p_is_open = (selected_p and selected_p.get("status") == "OPEN")

    financials = database.get_period_financials(p_id)
    total_inflow = financials.get("total_inflow", 0.0)
    total_sheets = financials.get("total_sheets", 0)
    total_employees = financials.get("total_employees", 0)
    total_entries = financials.get("total_entries", 0)
    
    conn = database.get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT e.id, e.category, e.set1, e.set3, e.set2, e.is_valid, e.sheet_id, e.worker_code, e.employee_name,
           e.box_2d, e.uncertain_note, s.created_at, s.image_path
    FROM entries e
    LEFT JOIN sheets s ON e.sheet_db_id = s.id
    WHERE e.period_id = ?
    ORDER BY e.sheet_db_id ASC, e.id ASC
    """, (p_id,))
    rows = cursor.fetchall()

    cursor.execute("""
    SELECT employee_name, worker_code, COUNT(id) as sheet_cnt
    FROM sheets
    WHERE period_id = ?
    GROUP BY employee_name, worker_code
    ORDER BY sheet_cnt DESC
    """, (p_id,))
    worker_counts = cursor.fetchall()
    conn.close()

    prize_data = {}
    p_winners = {"total_winners": 0, "total_payout": 0.0, "winners": []}
    try:
        import prize_service
        prize_data = prize_service.fetch_latest_thai_lottery()
        if prize_data.get("status") == "success":
            p_winners = prize_service.check_period_winners(p_id, prize_data.get("top3", ""), prize_data.get("bottom2", ""))
    except Exception as e:
        prize_data = {"status": "pending", "prize1": "-", "top3": "-", "bottom2": "-", "date": p_name}

    payout_total = p_winners.get("total_payout", 0.0)
    net_margin = total_inflow - payout_total
    margin_pct = round((net_margin / total_inflow * 100), 1) if total_inflow > 0 else 0.0
    dealer_share_vol = round(total_inflow * 0.67, 2)
    dealer_share_pct = 67 if total_inflow > 0 else 0
    payout_share_pct = round((payout_total / total_inflow * 100), 1) if total_inflow > 0 else 0

    # Period dropdown options
    period_options_html = ""
    for p in all_periods:
        p_sel = 'selected' if p["id"] == p_id else ''
        p_stat_text = "(เปิด)" if p.get("status") == "OPEN" else "(ปิด)"
        period_options_html += f'<option value="{p["id"]}" {p_sel}>{html_lib.escape(p["name"])} {p_stat_text}</option>'

    # Search items JSON
    search_items = []
    for r in rows:
        s1 = str(r["set1"] or "").strip()
        s2 = str(r["set2"] or "").strip()
        s3 = str(r["set3"] or "").strip()
        cat = r["category"] or ""
        sheet = r["sheet_id"] or "-"
        worker = f"{r['employee_name'] or 'พนักงาน'} ({r['worker_code'] or 'A'})"
        raw_img = str(r["image_path"] or "").strip()
        img_val = f"/{raw_img}" if raw_img.startswith("uploads/") else (raw_img if raw_img.startswith("/uploads/") else "")
        box_val = r["box_2d"] or ""
        note_val = r["uncertain_note"] or ""
        time_val = str(r["created_at"] or "")[:16]
        price_val = f"{s3} {s2}".strip() if s3 else s2
        search_items.append({
            "num": s1,
            "cat": cat,
            "price": price_val,
            "sheet": sheet,
            "worker": worker,
            "img": img_val,
            "box": box_val,
            "note": note_val,
            "time": time_val
        })
    search_json = json.dumps(search_items, ensure_ascii=False)

    # Table rows HTML
    table_rows_html = ""
    if rows:
        for idx, r in enumerate(rows, 1):
            cat = r["category"] or ""
            s1 = str(r["set1"] or "").strip()
            s2 = str(r["set2"] or "").strip()
            s3 = str(r["set3"] or "").strip()
            s3_part = f"<span class='text-amber-600 font-bold font-sans'>{html_lib.escape(s3)}</span> " if s3 else ""
            num_plain = f"{s1} = {s3 + ' ' if s3 else ''}{s2}"
            sheet_val = r["sheet_id"] or "-"
            worker_val = f"{r['employee_name'] or 'พนักงาน'} ({r['worker_code'] or 'A'})"
            time_val = str(r["created_at"] or "")[:16]
            raw_img = str(r["image_path"] or "").strip()
            img_val = f"/{raw_img}" if raw_img.startswith("uploads/") else (raw_img if raw_img.startswith("/uploads/") else "")
            box_val = r["box_2d"] or ""
            note_val = r["uncertain_note"] or ""
            
            brace_badge = ""
            if note_val and "ปีกกา" in note_val:
                brace_badge = f"<span class='text-[10px] font-medium text-[#A35C2B] bg-[#FDF0E7] border border-[#F6DCD0] px-2 py-0.5 rounded-md font-sans'>{html_lib.escape(note_val)}</span>"
                
            cat_badge_class = "text-[#2D6A4F]" if cat == "บน" else ("text-[#6B5384]" if cat == "ล่าง" else "text-[#A35C2B]")
            
            table_rows_html += f"""
            <div class="row-entry bg-[#FFFFFF] border border-[#EDE7DD] hover:border-[#D4CBBD] rounded-2xl p-3 flex items-center justify-between cursor-pointer shadow-xs transition-colors" data-cat="{html_lib.escape(cat)}" data-num="{html_lib.escape(num_plain)}" data-sheet="{html_lib.escape(sheet_val)}" data-worker="{html_lib.escape(worker_val)}" data-catname="[{html_lib.escape(cat)}]" data-time="{html_lib.escape(time_val)}" data-img="{html_lib.escape(img_val)}" data-box="{html_lib.escape(box_val)}" data-note="{html_lib.escape(note_val)}" onclick="openAuditSpotlight(this)">
              <div class="flex items-center gap-3">
                <span class="w-6 text-xs text-[#8A8279] font-mono text-center">{idx}</span>
                <div>
                  <div class="flex items-baseline gap-2">
                    <span class="text-base font-bold text-[#2A2421] font-mono">{html_lib.escape(s1)}</span>
                    <span class="text-xs text-[#A8A095]">=</span>
                    <span class="text-sm font-bold text-[#2D6A4F] font-mono">{s3_part}{html_lib.escape(s2)}</span>
                    {brace_badge}
                  </div>
                  <div class="flex items-center gap-2 mt-0.5 text-[11px] text-[#7D756D]">
                    <span class="{cat_badge_class} font-medium">[{html_lib.escape(cat)}]</span>
                    <span>ใบที่ {html_lib.escape(sheet_val)} ({html_lib.escape(worker_val)})</span>
                  </div>
                </div>
              </div>
              <span class="text-xs font-medium text-[#5C544C] bg-[#F5F2EB] border border-[#DFD8CC] px-2.5 py-1 rounded-xl">ส่องภาพ</span>
            </div>
            """
    else:
        table_rows_html = '<div class="text-center py-10 text-[#7D756D] bg-[#FFFFFF] rounded-2xl border border-[#EDE7DD] text-xs">ยังไม่มีข้อมูลโพยในงวดนี้</div>'

    # Top popular numbers HTML
    top_numbers_html = ""
    top_num_list = financials.get("top_numbers", [])
    if top_num_list:
        for tn in top_num_list[:4]:
            tnum = html_lib.escape(str(tn["number"]))
            tvol = f"{int(tn['volume']):,}฿" if tn["volume"] > 0 else f"{tn['count']} ครั้ง"
            top_numbers_html += f"""
            <div class="bg-[#F9F6F0] hover:bg-[#F2ECE2] cursor-pointer p-2 rounded-2xl border border-[#E8E1D5] transition-colors" onclick="quickSearchNum('{tnum}')">
              <div class="text-base font-bold text-[#2A2421] font-mono">{tnum}</div>
              <div class="text-[10px] text-[#2D6A4F] font-mono font-semibold">{tvol}</div>
            </div>
            """
    else:
        top_numbers_html = '<div class="col-span-4 text-center py-3 text-xs text-[#7D756D]">- ยังไม่มีสถิติ -</div>'

    # Worker summary HTML
    worker_rows_html = ""
    if worker_counts:
        for wc in worker_counts:
            w_name = html_lib.escape(str(wc["employee_name"] or "พนักงาน"))
            w_code = html_lib.escape(str(wc["worker_code"] or "A"))
            w_cnt = int(wc["sheet_cnt"] or 0)
            worker_rows_html += f"""
            <div class="flex items-center justify-between p-2.5 rounded-2xl bg-[#F9F6F0] border border-[#EAE3D8]">
              <span>{w_name} <span class="text-[#2D6A4F] font-mono font-semibold">({w_code})</span></span>
              <span class="font-mono text-[#2A2421] font-bold">{w_cnt} ใบ</span>
            </div>
            """
    else:
        worker_rows_html = '<div class="text-center py-2 text-xs text-[#7D756D]">- ยังไม่มีข้อมูล -</div>'

    # Winners list HTML
    winners_rows_html = ""
    w_list = p_winners.get("winners", [])
    if w_list:
        for idx, w in enumerate(w_list, 1):
            w_num = html_lib.escape(str(w.get("number", "")))
            w_type = html_lib.escape(str(w.get("match_type", "")))
            w_pay = f"{int(w.get('payout', 0)):,} ฿"
            w_sheet = html_lib.escape(str(w.get("sheet_id", "-")))
            w_emp = html_lib.escape(str(w.get("employee_name", "พนักงาน")))
            w_img = html_lib.escape(str(w.get("image_path", "")))
            w_img_url = f"/{w_img}" if w_img.startswith("uploads/") else (w_img if w_img.startswith("/uploads/") else "")
            
            winners_rows_html += f"""
            <div class="p-2.5 rounded-2xl bg-[#FFFFFF] border border-[#EDE7DD] flex items-center justify-between text-xs">
              <div>
                <div class="flex items-baseline gap-2 font-mono">
                  <span class="font-bold text-[#2A2421] text-sm">{w_num}</span>
                  <span class="text-[10px] text-[#2D6A4F] bg-[#E8F3EB] px-2 py-0.5 rounded-full">{w_type}</span>
                  <span class="font-bold text-[#A84357]">{w_pay}</span>
                </div>
                <div class="text-[10px] text-[#7D756D] mt-0.5">
                  ใบที่ {w_sheet} ({w_emp})
                </div>
              </div>
              {f'<a href="{w_img_url}" target="_blank" class="px-2.5 py-1 text-xs text-[#5C544C] bg-[#F5F2EB] border border-[#DFD8CC] rounded-xl text-decoration-none">ดูรูป</a>' if w_img_url else ''}
            </div>
            """
    else:
        winners_rows_html = '<div class="text-center py-3 text-xs text-[#7D756D]">ยังไม่มีรายการถูกรางวัลในงวดนี้</div>'

    status_pill_html = '<span class="inline-flex items-center gap-1.5 text-[11px] font-semibold px-2.5 py-1 rounded-full bg-[#E8F3EB] text-[#2D6A4F] border border-[#CFE4D4]"><span class="w-1.5 h-1.5 rounded-full bg-[#3A7D58]"></span>เปิดรับยอด</span>' if p_is_open else '<span class="inline-flex items-center gap-1.5 text-[11px] font-semibold px-2.5 py-1 rounded-full bg-[#FCEEEF] text-[#A84357] border border-[#F7D5D9]"><span class="w-1.5 h-1.5 rounded-full bg-[#A84357]"></span>ปิดงวดแล้ว</span>'

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>ระบบจัดการโพยหวย - {html_lib.escape(p_name)}</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Prompt:wght@300;400;500;600;700&family=JetBrains+Mono:wght@500;600;700;800&display=swap');
    
    * {{
      font-family: 'Prompt', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      -webkit-tap-highlight-color: transparent;
    }}
    
    .font-mono {{
      font-family: 'JetBrains Mono', monospace;
      font-feature-settings: 'tnum' on, 'lnum' on;
    }}

    ::-webkit-scrollbar {{
      width: 4px;
      height: 4px;
    }}
    ::-webkit-scrollbar-track {{
      background: transparent;
    }}
    ::-webkit-scrollbar-thumb {{
      background: #D8D0C3;
      border-radius: 4px;
    }}

    .phone-frame {{
      max-width: 430px;
      margin: 0 auto;
      border-radius: 44px;
      box-shadow: 0 25px 60px -15px rgba(80, 70, 60, 0.22), 0 0 0 1px #DFD7CB;
      overflow: hidden;
      min-height: 870px;
      position: relative;
      background: #F9F6F0;
    }}

    .spotlight-box {{
      border: 2px solid #2D6A4F;
      background: rgba(45, 106, 79, 0.12);
      border-radius: 6px;
    }}
  </style>
</head>
<body class="bg-[#ECE7DF] text-[#2A2421] antialiased min-h-screen p-0 md:p-6 flex flex-col items-center">

  <!-- Desktop vs Mobile Simulator Switcher Bar -->
  <div class="hidden md:flex w-full max-w-xl mb-4 items-center justify-between bg-[#FFFFFF] border border-[#DDD5C7] px-4 py-2 rounded-2xl shadow-xs">
    <div class="flex items-center gap-2">
      <span class="w-2.5 h-2.5 rounded-full bg-[#3A7D58]"></span>
      <span class="text-xs font-semibold text-[#2A2421]">ระบบจัดการโพยหวย (Mobile-First Ledger)</span>
    </div>
    <div class="flex items-center gap-2">
      <button id="btnToggleFrame" onclick="togglePhoneFrame()" class="text-xs font-medium px-3 py-1.5 rounded-xl bg-[#F4EFE6] hover:bg-[#EAE4D8] text-[#4A423B] border border-[#DDD5C7] transition-all">
        <span id="frameText">สลับดูแบบเต็มจอ (Desktop)</span>
      </button>
    </div>
  </div>

  <!-- Main Viewport Container -->
  <div id="appContainer" class="phone-frame w-full text-[#2A2421] flex flex-col transition-all duration-200">

    <!-- Sticky App Header -->
    <header class="sticky top-0 z-30 bg-[#F9F6F0]/95 backdrop-blur-sm border-b border-[#EBE4D8] px-4 py-3 flex items-center justify-between">
      
      <!-- Period Selector Dropdown -->
      <div class="flex items-center gap-1.5 bg-[#FFFFFF] hover:bg-[#F5F0E6] px-3.5 py-1.5 rounded-full border border-[#DFD8CC] shadow-xs transition-all">
        <svg class="w-3.5 h-3.5 text-[#2D6A4F]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"></path>
        </svg>
        <select onchange="location.href='/?period_id=' + this.value" class="bg-transparent text-xs font-semibold text-[#2A2421] outline-none cursor-pointer">
          {period_options_html}
        </select>
      </div>

      <div class="flex items-center gap-2">
        {status_pill_html}
        <!-- CSV Export Button -->
        <a href="/export-csv?period_id={p_id}" title="ดาวน์โหลด CSV" class="h-7.5 px-2.5 rounded-xl bg-[#FFFFFF] hover:bg-[#F3EFE8] border border-[#DDD5C7] text-[#5C544C] text-xs font-medium flex items-center gap-1 shadow-xs transition-all text-decoration-none">
          <svg class="w-3.5 h-3.5 text-[#7D756D]" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"></path></svg>
          <span>CSV</span>
        </a>
      </div>
    </header>

    <!-- Scrollable Content Area -->
    <main class="flex-1 overflow-y-auto pb-28 px-4 pt-3 space-y-4">

      <!-- ==================== TAB 1: OVERVIEW (ภาพรวม) ==================== -->
      <div id="tab-content-overview" class="space-y-4">
        
        <div>
          <h1 class="text-xl font-bold text-[#2A2421] tracking-tight">ภาพรวมของงวด</h1>
          <div class="text-xs font-medium text-[#7D756D] mt-0.5">{html_lib.escape(p_name)}</div>
          <p class="text-xs text-[#8A8279] mt-0.5">ทุกโพย ทุกรางวัล และทุกยอดค้าง รวมอยู่ที่นี่</p>
        </div>

        <!-- Quick Action Buttons Bar -->
        <div class="flex items-center gap-2">
          <a href="/export-csv?period_id={p_id}" class="flex-1 py-2 px-3 rounded-2xl bg-[#FFFFFF] hover:bg-[#F5F0E6] border border-[#DDD5C7] text-xs font-medium text-[#4A423B] flex items-center justify-center gap-1.5 shadow-xs transition-all text-decoration-none">
            <svg class="w-3.5 h-3.5 text-[#7D756D]" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"></path></svg>
            <span>ดาวน์โหลดสรุป</span>
          </a>
          <button onclick="openSearchModal()" class="flex-1 py-2 px-3 rounded-2xl bg-[#FFFFFF] hover:bg-[#F5F0E6] border border-[#DDD5C7] text-xs font-medium text-[#4A423B] flex items-center justify-center gap-1.5 shadow-xs transition-all">
            <svg class="w-3.5 h-3.5 text-[#3A7D58]" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"></path></svg>
            <span>ค้นหาตัวเลข</span>
          </button>
        </div>

        <!-- 1. Hero Main Card (Pastel Matcha Cream) -->
        <div class="rounded-3xl bg-[#E8F3EB] border border-[#CFE4D4] p-5 shadow-xs relative overflow-hidden">
          
          <div class="flex items-start justify-between">
            <div>
              <div class="flex items-center gap-2">
                <span class="text-[11px] font-semibold text-[#1F543B] px-2.5 py-0.5 rounded-full bg-[#D4ECD9] border border-[#BCE1C5]">
                  {'ยกยอดแล้ว' if not p_is_open else 'เปิดรับยอด'}
                </span>
                <span class="text-xs text-[#687F75]">{html_lib.escape(p_name)}</span>
              </div>
              <div class="mt-3">
                <div class="text-xs text-[#526B5F] font-medium">ยอดรับโพยทั้งหมด</div>
                <div class="text-3xl sm:text-4xl font-bold text-[#1F2623] font-mono tracking-tight mt-1 flex items-baseline gap-1">
                  <span>{int(total_inflow):,}</span>
                  <span class="text-xl font-bold text-[#2D6A4F]">฿</span>
                </div>
                <div class="text-xs text-[#687F75] mt-1 font-mono">
                  {total_sheets} ใบ · {total_employees} คนเดินโพย · {total_entries} รายการ
                </div>
              </div>
            </div>

            <!-- Stylized Sheet Illustration -->
            <div class="w-14 h-18 bg-[#FFFFFF] rounded-2xl border border-[#CFE4D4] p-2 flex flex-col justify-between shadow-xs">
              <div class="flex items-center justify-between">
                <div class="w-4 h-1.5 bg-[#A3D4B3] rounded"></div>
                <span class="w-4 h-4 rounded-full bg-[#3A7D58] text-[#FFFFFF] flex items-center justify-center text-[9px] font-bold">✓</span>
              </div>
              <div class="space-y-1.5">
                <div class="w-full h-1 bg-[#E1ECE4] rounded"></div>
                <div class="w-3/4 h-1 bg-[#E1ECE4] rounded"></div>
                <div class="w-1/2 h-1 bg-[#E1ECE4] rounded"></div>
              </div>
            </div>
          </div>

          <!-- Action Button inside Hero Card -->
          <div class="mt-5">
            <button onclick="switchTab('table')" class="w-full py-2.5 px-4 rounded-2xl bg-[#3A7D58] hover:bg-[#326C4C] text-[#FFFFFF] text-sm font-semibold flex items-center justify-center gap-2 shadow-xs transition-all active:scale-[0.99]">
              <svg class="w-4 h-4 text-[#FFFFFF]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01"></path>
              </svg>
              <span>ดูโพยทั้งหมด</span>
            </button>
          </div>
        </div>

        <!-- 2. Dual Ledger Cards (ยอดส่งเจ้ามือ vs รางวัลคนเดินโพย) -->
        <div class="grid grid-cols-2 gap-2.5">
          
          <!-- Card Left: ยอดส่งเจ้ามือ (Peach) -->
          <div class="bg-[#FDF2EA] border border-[#F7DFD2] p-3.5 rounded-3xl flex flex-col justify-between shadow-xs">
            <div>
              <div class="flex items-center gap-2">
                <div class="w-7 h-7 rounded-xl bg-[#FBE4D5] flex items-center justify-center text-[#A35C2B]">
                  <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"></path></svg>
                </div>
                <span class="text-xs font-semibold text-[#4A392F]">ยอดส่งเจ้ามือ</span>
              </div>
              <div class="mt-2.5">
                <div class="text-lg font-bold text-[#2A2421] font-mono flex items-baseline gap-1">
                  <span>{int(dealer_share_vol):,}</span>
                  <span class="text-xs font-semibold text-[#A35C2B]">฿</span>
                </div>
                <div class="mt-1">
                  <span class="text-[10px] font-semibold text-[#A35C2B] px-2 py-0.5 rounded-full bg-[#FCE6D7]">
                    1 เจ้า
                  </span>
                </div>
              </div>
            </div>

            <div class="mt-3 pt-2.5 border-t border-[#F5D5C4]">
              <div class="flex items-center justify-between text-[11px] text-[#7A6457]">
                <span>สัดส่วนต่อยอดรับ</span>
                <span class="font-mono text-[#2A2421] font-semibold">{dealer_share_pct}%</span>
              </div>
              <div class="w-full bg-[#F5D5C4] h-1.5 rounded-full mt-1.5 overflow-hidden">
                <div class="bg-[#D97736] h-full rounded-full" style="width: {dealer_share_pct}%"></div>
              </div>
            </div>
          </div>

          <!-- Card Right: รางวัลคนเดินโพย (Vanilla) -->
          <div class="bg-[#FEF9EA] border border-[#F6ECCB] p-3.5 rounded-3xl flex flex-col justify-between shadow-xs">
            <div>
              <div class="flex items-center gap-2">
                <div class="w-7 h-7 rounded-xl bg-[#FCF0CE] flex items-center justify-center text-[#8F6E14]">
                  <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z"></path></svg>
                </div>
                <span class="text-xs font-semibold text-[#4D4222]">รางวัลคนเดินโพย</span>
              </div>
              <div class="mt-2.5">
                <div class="text-lg font-bold text-[#2A2421] font-mono flex items-baseline gap-1">
                  <span>{int(payout_total):,}</span>
                  <span class="text-xs font-semibold text-[#8F6E14]">฿</span>
                </div>
                <div class="mt-1">
                  <span class="text-[10px] font-semibold text-[#8F6E14] px-2 py-0.5 rounded-full bg-[#FAF0CE]">
                    {p_winners.get('total_winners', 0)} ใบถูก
                  </span>
                </div>
              </div>
            </div>

            <div class="mt-3 pt-2.5 border-t border-[#F2E3B8]">
              <div class="flex items-center justify-between text-[11px] text-[#7A6B43]">
                <span>สัดส่วนต่อยอดรับ</span>
                <span class="font-mono text-[#2A2421] font-semibold">{payout_share_pct}%</span>
              </div>
              <div class="w-full bg-[#F2E3B8] h-1.5 rounded-full mt-1.5 overflow-hidden">
                <div class="bg-[#D49E1E] h-full rounded-full" style="width: {min(payout_share_pct, 100)}%"></div>
              </div>
            </div>
          </div>

        </div>

        <!-- 3. Category Breakdown: หมวดหมู่ [บน / ล่าง / บนล่าง] -->
        <div class="bg-[#FFFFFF] border border-[#EDE7DD] p-4 rounded-3xl shadow-xs">
          <div class="flex items-center justify-between mb-2.5">
            <span class="text-xs font-semibold text-[#4A423B]">ยอดแยกตามหมวดหมู่</span>
            <span class="text-[11px] text-[#7D756D] font-mono">3 หมวด</span>
          </div>
          <div class="grid grid-cols-3 gap-2">
            <!-- บน -->
            <div class="bg-[#E8F3EB] border border-[#CFE4D4] p-2.5 rounded-2xl">
              <div class="text-xs font-bold text-[#2D6A4F]">[บน]</div>
              <div class="text-sm font-bold text-[#1F2623] font-mono mt-1">{int(financials['inflow_by_category'].get('บน', 0)):,}</div>
              <div class="text-[10px] text-[#5C6E64] mt-0.5 font-mono">{financials['count_by_category'].get('บน', 0)} รายการ</div>
              <div class="w-full bg-[#CFE4D4] h-1 rounded-full mt-1.5 overflow-hidden">
                <div class="bg-[#3A7D58] h-full rounded-full" style="width: {round(financials['inflow_by_category'].get('บน', 0) / total_inflow * 100) if total_inflow else 0}%"></div>
              </div>
            </div>

            <!-- ล่าง -->
            <div class="bg-[#F3EFF8] border border-[#E3D9ED] p-2.5 rounded-2xl">
              <div class="text-xs font-bold text-[#6B5384]">[ล่าง]</div>
              <div class="text-sm font-bold text-[#1F2623] font-mono mt-1">{int(financials['inflow_by_category'].get('ล่าง', 0)):,}</div>
              <div class="text-[10px] text-[#6E5F7A] mt-0.5 font-mono">{financials['count_by_category'].get('ล่าง', 0)} รายการ</div>
              <div class="w-full bg-[#E3D9ED] h-1 rounded-full mt-1.5 overflow-hidden">
                <div class="bg-[#7E649B] h-full rounded-full" style="width: {round(financials['inflow_by_category'].get('ล่าง', 0) / total_inflow * 100) if total_inflow else 0}%"></div>
              </div>
            </div>

            <!-- บนล่าง -->
            <div class="bg-[#FDF2EA] border border-[#F7DFD2] p-2.5 rounded-2xl">
              <div class="text-xs font-bold text-[#A35C2B]">[บนล่าง]</div>
              <div class="text-sm font-bold text-[#1F2623] font-mono mt-1">{int(financials['inflow_by_category'].get('บนล่าง', 0)):,}</div>
              <div class="text-[10px] text-[#7A6457] mt-0.5 font-mono">{financials['count_by_category'].get('บนล่าง', 0)} รายการ</div>
              <div class="w-full bg-[#F5D5C4] h-1 rounded-full mt-1.5 overflow-hidden">
                <div class="bg-[#D97736] h-full rounded-full" style="width: {round(financials['inflow_by_category'].get('บนล่าง', 0) / total_inflow * 100) if total_inflow else 0}%"></div>
              </div>
            </div>
          </div>
        </div>

        <!-- 4. Breakdown Section: ประเภทเลข [2 ตัว / 3 ตัว] -->
        <div class="bg-[#FFFFFF] border border-[#EDE7DD] p-4 rounded-3xl shadow-xs">
          <div class="flex items-center justify-between mb-2.5">
            <span class="text-xs font-semibold text-[#4A423B]">สัดส่วนประเภทตัวเลข</span>
            <span class="text-[11px] text-[#7D756D] font-mono">2 ตัว vs 3 ตัว</span>
          </div>
          <div class="grid grid-cols-2 gap-2.5">
            <!-- 2 Digits -->
            <div class="bg-[#F9F6F0] border border-[#EAE3D8] p-3 rounded-2xl">
              <div class="flex items-center justify-between text-xs">
                <span class="font-medium text-[#4A423B]">เลข 2 ตัว</span>
                <span class="text-[#6B5384] font-mono font-semibold">{round(financials.get('digits_2_volume', 0) / total_inflow * 100) if total_inflow else 0}%</span>
              </div>
              <div class="text-base font-bold text-[#2A2421] font-mono mt-1">
                {int(financials.get('digits_2_volume', 0)):,} <span class="text-xs font-normal text-[#6B5384] font-sans">฿</span>
              </div>
              <div class="text-[10px] text-[#7D756D] mt-0.5 font-mono">{financials.get('digits_2_count', 0)} รายการ</div>
              <div class="w-full bg-[#E5DFD4] h-1 rounded-full mt-2 overflow-hidden">
                <div class="bg-[#7E649B] h-full rounded-full" style="width: {round(financials.get('digits_2_volume', 0) / total_inflow * 100) if total_inflow else 0}%"></div>
              </div>
            </div>

            <!-- 3 Digits -->
            <div class="bg-[#F9F6F0] border border-[#EAE3D8] p-3 rounded-2xl">
              <div class="flex items-center justify-between text-xs">
                <span class="font-medium text-[#4A423B]">เลข 3 ตัว</span>
                <span class="text-[#2D6A4F] font-mono font-semibold">{round(financials.get('digits_3_volume', 0) / total_inflow * 100) if total_inflow else 0}%</span>
              </div>
              <div class="text-base font-bold text-[#2A2421] font-mono mt-1">
                {int(financials.get('digits_3_volume', 0)):,} <span class="text-xs font-normal text-[#2D6A4F] font-sans">฿</span>
              </div>
              <div class="text-[10px] text-[#7D756D] mt-0.5 font-mono">{financials.get('digits_3_count', 0)} รายการ</div>
              <div class="w-full bg-[#E5DFD4] h-1 rounded-full mt-2 overflow-hidden">
                <div class="bg-[#3A7D58] h-full rounded-full" style="width: {round(financials.get('digits_3_volume', 0) / total_inflow * 100) if total_inflow else 0}%"></div>
              </div>
            </div>
          </div>
        </div>

        <!-- 5. Top Popular Numbers -->
        <div class="bg-[#FFFFFF] border border-[#EDE7DD] p-4 rounded-3xl shadow-xs">
          <div class="flex items-center justify-between mb-2.5">
            <span class="text-xs font-semibold text-[#4A423B]">ตัวเลขยอดนิยมประจำงวด</span>
            <span class="text-[11px] text-[#7D756D]">แตะเพื่อค้นหา</span>
          </div>

          <div class="grid grid-cols-4 gap-2 text-center">
            {top_numbers_html}
          </div>
        </div>

      </div>

      <!-- ==================== TAB 2: LIVE SHEET TABLE (ตารางโพย) ==================== -->
      <div id="tab-content-table" class="space-y-3 hidden">
        
        <div class="flex items-center justify-between">
          <div>
            <h2 class="text-base font-bold text-[#2A2421]">กระดานตารางโพย</h2>
            <p class="text-xs text-[#7D756D]">แตะที่แถวเพื่อส่องลายมือต้นฉบับ</p>
          </div>
          <span class="text-xs font-mono font-semibold text-[#2D6A4F] bg-[#E8F3EB] px-2.5 py-1 rounded-xl border border-[#CFE4D4]">
            {total_entries} รายการ
          </span>
        </div>

        <!-- Sub-tabs for Column Filter -->
        <div class="flex items-center gap-1 p-1 bg-[#F0EBE1] border border-[#DDD5C7] rounded-2xl">
          <button id="btnColAll" onclick="filterCol('all')" class="flex-1 py-1.5 text-xs font-semibold rounded-xl bg-[#FFFFFF] text-[#2A2421] border border-[#DDD5C7] shadow-xs transition-all">
            ทั้งหมด
          </button>
          <button id="btnColTop" onclick="filterCol('บน')" class="flex-1 py-1.5 text-xs font-medium rounded-xl text-[#6D655E] hover:text-[#2A2421] transition-all">
            บน
          </button>
          <button id="btnColBot" onclick="filterCol('ล่าง')" class="flex-1 py-1.5 text-xs font-medium rounded-xl text-[#6D655E] hover:text-[#2A2421] transition-all">
            ล่าง
          </button>
          <button id="btnColTopBot" onclick="filterCol('บนล่าง')" class="flex-1 py-1.5 text-xs font-medium rounded-xl text-[#6D655E] hover:text-[#2A2421] transition-all">
            บนล่าง
          </button>
        </div>

        <!-- Table Rows Container -->
        <div id="tableRowsList" class="space-y-2">
          {table_rows_html}
        </div>

      </div>

      <!-- ==================== TAB 4: PRIZES & P&L (ตรวจรางวัล) ==================== -->
      <div id="tab-content-prizes" class="space-y-4 hidden">
        
        <div>
          <h2 class="text-base font-bold text-[#2A2421]">ผลรางวัล & บัญชีกำไร-ขาดทุน</h2>
          <p class="text-xs text-[#7D756D]">ผลสลากกินแบ่งรัฐบาล ({prize_data.get('source', 'GLO Official API')})</p>
        </div>

        <!-- Official Lottery Live Results Box -->
        <div class="bg-[#FEF9EA] border border-[#F6ECCB] p-4 rounded-3xl shadow-xs">
          <div class="flex items-center justify-between mb-2.5">
            <span class="text-xs font-semibold text-[#5E4F28]">ผลสลากประจำงวด</span>
            <span class="text-[11px] text-[#7A6B43] font-mono">{prize_data.get('date', p_name)}</span>
          </div>

          <div class="grid grid-cols-3 gap-2 text-center">
            <div class="bg-[#FFFFFF] p-2.5 rounded-2xl border border-[#F4E6BD]">
              <div class="text-[10px] text-[#7D756D]">รางวัลที่ 1</div>
              <div class="text-base font-bold text-[#2A2421] font-mono mt-0.5">{prize_data.get('prize1', '-')}</div>
            </div>
            <div class="bg-[#FFFFFF] p-2.5 rounded-2xl border border-[#F4E6BD]">
              <div class="text-[10px] text-[#7D756D]">3 ตัวบน</div>
              <div class="text-base font-bold text-[#2D6A4F] font-mono mt-0.5">{prize_data.get('top3', '-')}</div>
            </div>
            <div class="bg-[#FFFFFF] p-2.5 rounded-2xl border border-[#F4E6BD]">
              <div class="text-[10px] text-[#7D756D]">2 ตัวล่าง</div>
              <div class="text-base font-bold text-[#2D6A4F] font-mono mt-0.5">{prize_data.get('bottom2', '-')}</div>
            </div>
          </div>
        </div>

        <!-- P&L Financial Card -->
        <div class="bg-[#FFFFFF] border border-[#EDE7DD] p-4 rounded-3xl space-y-3 shadow-xs">
          <div class="flex items-center justify-between">
            <span class="text-xs font-semibold text-[#2A2421]">สรุปกำไร-ขาดทุนประจำงวด</span>
            <span class="text-xs font-semibold {'text-[#1F543B] bg-[#E8F3EB] border-[#CFE4D4]' if net_margin >= 0 else 'text-[#A84357] bg-[#FCEEEF] border-[#F7D5D9]'} px-2.5 py-0.5 rounded-full border">
              {'กำไรสุทธิ +' if net_margin >= 0 else 'ขาดทุน '}{margin_pct}%
            </span>
          </div>

          <div class="grid grid-cols-2 gap-2 text-center">
            <div class="bg-[#E8F3EB] p-3 rounded-2xl border border-[#CFE4D4]">
              <div class="text-[11px] text-[#526B5F]">ยอดรับรวม (Inflow)</div>
              <div class="text-base font-bold text-[#1F2623] font-mono mt-0.5">{int(total_inflow):,} ฿</div>
            </div>
            <div class="bg-[#FCEEEF] p-3 rounded-2xl border border-[#F7D5D9]">
              <div class="text-[11px] text-[#A84357]">ยอดจ่ายรางวัล (Payout)</div>
              <div class="text-base font-bold text-[#912A3E] font-mono mt-0.5">{int(payout_total):,} ฿</div>
            </div>
          </div>

          <div class="p-3.5 bg-[#E8F3EB] border border-[#CFE4D4] rounded-2xl flex items-center justify-between">
            <div>
              <div class="text-xs text-[#526B5F]">กำไรสุทธิ (Net Margin)</div>
              <div class="text-xl font-bold {'text-[#2D6A4F]' if net_margin >= 0 else 'text-[#A84357]'} font-mono mt-0.5">
                {'+' if net_margin >= 0 else ''}{int(net_margin):,} ฿
              </div>
            </div>
            <a href="/prizes?period_id={p_id}" class="px-4 py-2 text-xs font-bold bg-[#3A7D58] hover:bg-[#326C4C] text-[#FFFFFF] rounded-xl shadow-xs transition-all text-decoration-none">
              ดูบิลถูกรางวัล
            </a>
          </div>
        </div>

        <!-- Winning bills list -->
        <div class="bg-[#FFFFFF] border border-[#EDE7DD] p-4 rounded-3xl space-y-2.5 shadow-xs">
          <div class="flex items-center justify-between text-xs">
            <span class="font-semibold text-[#2A2421]">รายการถูกรางวัล ({p_winners.get('total_winners', 0)} รายการ)</span>
          </div>
          <div class="space-y-1.5">
            {winners_rows_html}
          </div>
        </div>

      </div>

      <!-- ==================== TAB 5: ADMIN / SETTINGS (จัดการ) ==================== -->
      <div id="tab-content-admin" class="space-y-4 hidden">
        
        <div>
          <h2 class="text-base font-bold text-[#2A2421]">จัดการระบบ</h2>
          <p class="text-xs text-[#7D756D]">ควบคุมสถานะงวดและส่งออกรายงาน</p>
        </div>

        <div class="bg-[#FFFFFF] border border-[#EDE7DD] p-4 rounded-3xl space-y-3 shadow-xs">
          <div class="text-xs font-semibold text-[#2A2421]">สถานะงวดปัจจุบัน: <span class="font-bold">{html_lib.escape(p_name)}</span></div>
          <div class="flex gap-2">
            <form method="POST" action="/api/period/toggle" class="flex-1">
              <input type="hidden" name="period_id" value="{p_id}">
              <input type="hidden" name="action" value="close">
              <button type="submit" class="w-full py-2 text-xs font-semibold bg-[#FDF0F1] hover:bg-[#FCE3E5] text-[#A84357] border border-[#F7D5D9] rounded-2xl transition-all cursor-pointer">
                ปิดรับงวดนี้
              </button>
            </form>
            <form method="POST" action="/api/period/toggle" class="flex-1">
              <input type="hidden" name="period_id" value="{p_id}">
              <input type="hidden" name="action" value="open">
              <button type="submit" class="w-full py-2 text-xs font-semibold bg-[#E8F3EB] hover:bg-[#DDF0E2] text-[#2D6A4F] border border-[#CFE4D4] rounded-2xl transition-all cursor-pointer">
                เปิดรับงวดนี้
              </button>
            </form>
          </div>
        </div>

        <div class="bg-[#FFFFFF] border border-[#EDE7DD] p-4 rounded-3xl space-y-2 shadow-xs">
          <div class="text-xs font-semibold text-[#2A2421]">ดาวน์โหลดรายงาน</div>
          <div class="space-y-2">
            <a href="/export-csv?period_id={p_id}" class="w-full py-2.5 px-3.5 rounded-2xl bg-[#F9F6F0] hover:bg-[#F2ECE2] border border-[#DDD5C7] text-xs font-medium text-[#2A2421] flex items-center justify-between transition-colors text-decoration-none">
              <span>รายงาน CSV รวมตารางทั้งหมด</span>
              <span class="text-[#7D756D] font-mono">.csv</span>
            </a>
            <a href="/export-vertical-csv?period_id={p_id}" class="w-full py-2.5 px-3.5 rounded-2xl bg-[#F9F6F0] hover:bg-[#F2ECE2] border border-[#DDD5C7] text-xs font-medium text-[#2A2421] flex items-center justify-between transition-colors text-decoration-none">
              <span>รายงาน CSV แนวตั้ง (สำหรับ Excel)</span>
              <span class="text-[#7D756D] font-mono">.csv</span>
            </a>
            <a href="/export-ai-bundle?period_id={p_id}" class="w-full py-2.5 px-3.5 rounded-2xl bg-[#F9F6F0] hover:bg-[#F2ECE2] border border-[#DDD5C7] text-xs font-medium text-[#2A2421] flex items-center justify-between transition-colors text-decoration-none">
              <span>ชุดข้อมูล JSONL สำหรับเทรน AI (Google Colab)</span>
              <span class="text-[#7D756D] font-mono">.jsonl</span>
            </a>
          </div>
        </div>

        <!-- Worker Sheet Counts -->
        <div class="bg-[#FFFFFF] border border-[#EDE7DD] p-4 rounded-3xl space-y-2.5 shadow-xs">
          <div class="flex items-center justify-between text-xs">
            <span class="font-semibold text-[#2A2421]">พนักงานส่งงาน</span>
            <span class="text-[#7D756D] font-mono">{len(worker_counts)} คน</span>
          </div>
          <div class="space-y-2 text-xs text-[#2A2421]">
            {worker_rows_html}
          </div>
        </div>

      </div>

    </main>

    <!-- Bottom Navigation Bar (Pastel Ivory Style) -->
    <nav class="fixed md:absolute bottom-0 left-0 right-0 z-40 bg-[#FFFFFF] border-t border-[#EBE4D8] px-3 py-2 flex items-center justify-around shadow-lg">
      
      <!-- Tab 1: Overview -->
      <button id="nav-btn-overview" onclick="switchTab('overview')" class="flex flex-col items-center justify-center text-[#2D6A4F] transition-colors cursor-pointer">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"></path></svg>
        <span class="text-[10px] font-semibold mt-1">ภาพรวม</span>
      </button>

      <!-- Tab 2: Table -->
      <button id="nav-btn-table" onclick="switchTab('table')" class="flex flex-col items-center justify-center text-[#9E968D] hover:text-[#4A423B] transition-colors cursor-pointer">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01"></path></svg>
        <span class="text-[10px] font-medium mt-1">ตารางโพย</span>
      </button>

      <!-- Tab 3 (Center): Raised Action Button with Warm Cream Ring -->
      <div class="relative -top-5">
        <button onclick="openSearchModal()" title="ค้นหาตัวเลขด่วน" class="w-13 h-13 rounded-full bg-[#3A7D58] hover:bg-[#326C4C] text-[#FFFFFF] flex items-center justify-center text-2xl font-bold shadow-md ring-4 ring-[#F9F6F0] transition-all active:scale-95 p-3.5 cursor-pointer">
          <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M12 4v16m8-8H4"></path></svg>
        </button>
      </div>

      <!-- Tab 4: Prizes -->
      <button id="nav-btn-prizes" onclick="switchTab('prizes')" class="flex flex-col items-center justify-center text-[#9E968D] hover:text-[#4A423B] transition-colors cursor-pointer">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
        <span class="text-[10px] font-medium mt-1">ตรวจรางวัล</span>
      </button>

      <!-- Tab 5: Admin -->
      <button id="nav-btn-admin" onclick="switchTab('admin')" class="flex flex-col items-center justify-center text-[#9E968D] hover:text-[#4A423B] transition-colors cursor-pointer">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path></svg>
        <span class="text-[10px] font-medium mt-1">จัดการ</span>
      </button>

    </nav>

    <!-- ==================== MODAL 1: INSTANT SEARCH ==================== -->
    <div id="searchModal" class="fixed inset-0 z-50 bg-[#2A2421]/45 backdrop-blur-xs hidden flex items-center justify-center p-3" onclick="if(event.target === this) closeSearchModal()">
      <div class="w-full max-w-sm bg-[#FFFFFF] border border-[#DDD5C7] rounded-3xl p-4 shadow-xl space-y-3">
        
        <div class="flex items-center justify-between border-b border-[#EBE4D8] pb-2.5">
          <div class="text-sm font-bold text-[#2A2421]">ค้นหาตัวเลขด่วน</div>
          <button onclick="closeSearchModal()" class="w-7 h-7 rounded-xl bg-[#F4EFE6] text-[#7D756D] hover:text-[#2A2421] flex items-center justify-center text-xs cursor-pointer">✕</button>
        </div>

        <input type="text" id="instantSearchInput" oninput="doSearch()" placeholder="พิมพ์ตัวเลข เช่น 401, 370..." class="w-full bg-[#F9F6F0] border border-[#DDD5C7] focus:border-[#3A7D58] rounded-2xl px-3.5 py-2.5 text-base font-mono font-bold text-[#2A2421] placeholder-[#A8A095] outline-none">

        <!-- Quick suggestion pills -->
        <div id="searchQuickPills" class="flex items-center gap-1.5 flex-wrap text-xs">
          <!-- Populated by JS -->
        </div>

        <!-- Search Results Box -->
        <div id="searchResultsBox" class="space-y-1.5 max-h-60 overflow-y-auto pt-1">
          <!-- Dynamic Results -->
        </div>

      </div>
    </div>

    <!-- ==================== MODAL 2: AUTO-ZOOM SPOTLIGHT AUDIT MODAL ==================== -->
    <div id="auditModal" class="fixed inset-0 z-50 bg-[#2A2421]/50 backdrop-blur-xs hidden flex items-center justify-center p-3" onclick="if(event.target === this) closeAuditModal()">
      <div class="w-full max-w-md bg-[#FFFFFF] border border-[#DDD5C7] rounded-3xl overflow-hidden shadow-2xl flex flex-col max-h-[85vh]">
        
        <!-- Modal Header -->
        <div class="p-3.5 bg-[#F9F6F0] border-b border-[#EBE4D8] flex items-center justify-between">
          <div>
            <div class="text-xs font-semibold text-[#6D655E]">ตรวจสอบภาพถ่ายต้นฉบับ</div>
            <div class="text-sm font-bold text-[#2A2421] font-mono flex items-center gap-2 mt-0.5">
              <span id="auditNumTag" class="text-[#2D6A4F] font-bold">-</span>
              <span id="auditSubMeta" class="text-xs font-normal text-[#7D756D] font-sans">-</span>
            </div>
          </div>
          <button onclick="closeAuditModal()" class="w-7 h-7 rounded-xl bg-[#EAE4D8] text-[#7D756D] hover:text-[#2A2421] flex items-center justify-center text-xs cursor-pointer">✕</button>
        </div>

        <!-- Viewport -->
        <div class="p-3.5 space-y-3 flex-1 overflow-hidden flex flex-col">
          <div class="flex items-center justify-between text-xs text-[#6D655E]">
            <span class="font-medium">ตำแหน่งบนกระดาษโพยจริง</span>
            <div class="flex items-center gap-2">
              <span id="auditZoomIndicator" class="font-mono text-[#7D756D]">ซูม 250%</span>
              <button type="button" onclick="zoomAudit(0.4)" class="px-1.5 py-0.5 rounded bg-[#F0EBE1] text-[#2A2421] text-xs font-bold border border-[#DFD8CC]">➕</button>
              <button type="button" onclick="zoomAudit(-0.4)" class="px-1.5 py-0.5 rounded bg-[#F0EBE1] text-[#2A2421] text-xs font-bold border border-[#DFD8CC]">➖</button>
              <button type="button" onclick="resetAuditZoom()" class="px-1.5 py-0.5 rounded bg-[#F0EBE1] text-[#2A2421] text-xs border border-[#DFD8CC]">🔄</button>
              <a id="auditFullLink" href="#" target="_blank" class="px-2 py-0.5 rounded bg-[#F0EBE1] text-[#2A2421] text-xs border border-[#DFD8CC] text-decoration-none">เต็มจอ</a>
            </div>
          </div>

          <!-- Canvas / Viewport Simulation -->
          <div id="auditViewport" class="relative w-full h-64 bg-[#EBE5DB] border border-[#DDD5C7] rounded-2xl overflow-hidden flex items-center justify-center" style="cursor: grab;">
            
            <div id="auditZoomContainer" style="position: absolute; transform-origin: 0 0; transition: transform 0.1s ease-out;">
              <img id="auditImg" src="" alt="Sheet Photo" style="display: block; max-width: none; border-radius: 4px;" onload="onAuditImageLoaded()">
              <div id="auditSpotlightRing" class="spotlight-box" style="position: absolute; display: none; pointer-events: none;"></div>
            </div>

            <div id="auditEmptyPlaceholder" style="display: none; padding: 40px 16px; text-align: center;">
              <div style="font-size: 36px; margin-bottom: 8px;">📷</div>
              <div style="font-size: 14px; font-weight: 700; color: #2A2421;">ไม่มีไฟล์ภาพถ่ายในเซิร์ฟเวอร์</div>
              <div style="font-size: 11px; color: #7D756D; margin-top: 4px;">ข้อมูลนี้อาจส่งมาทางแชทข้อความโดยตรง หรือเป็นข้อมูลทดสอบ</div>
            </div>

            <div class="absolute bottom-2 text-[10px] text-[#2A2421] bg-[#FFFFFF]/90 px-2.5 py-0.5 rounded-lg border border-[#DDD5C7] shadow-xs pointer-events-none">
              ตำแหน่งจริงบนกระดาษ
            </div>
          </div>

          <div id="auditBraceBanner" class="hidden p-2.5 rounded-2xl bg-[#FDF2EA] border border-[#F7DFD2] text-xs text-[#A35C2B] flex items-center gap-2">
            <span class="w-2 h-2 rounded-full bg-[#D97736]"></span>
            <span id="auditBraceDesc">ตัวเลขนี้อยู่ในกลุ่มปีกการ่วมกัน</span>
          </div>

          <div class="flex items-center gap-2">
            <a id="auditEditBtn" href="#" class="flex-1 py-2.5 bg-[#F4EFE6] hover:bg-[#EAE4D8] text-[#4A423B] font-bold text-xs rounded-2xl border border-[#DDD5C7] text-center shadow-xs transition-all flex items-center justify-center gap-1.5 text-decoration-none">
              <span>✏️ แก้ไขข้อมูลใบนี้</span>
            </a>
            <button type="button" onclick="closeAuditModal()" class="flex-1 py-2.5 bg-[#3A7D58] hover:bg-[#326C4C] text-[#FFFFFF] font-bold text-xs rounded-2xl shadow-xs transition-all cursor-pointer">
              ตกลง / ปิด
            </button>
          </div>
        </div>

      </div>
    </div>

  </div>

  <!-- Toast element -->
  <div id="toast" class="fixed top-4 z-50 bg-[#2A2421] text-[#F9F6F0] text-xs px-4 py-2 rounded-2xl shadow-lg opacity-0 transform -translate-y-3 transition-all pointer-events-none">
    ข้อความแจ้งเตือน
  </div>

  <script>
    var allEntries = {search_json};
    var currentAuditScale = 1.0;
    var currentAuditX = 0;
    var currentAuditY = 0;
    var isPanningAudit = false;
    var panStartX = 0;
    var panStartY = 0;
    var auditBoxCoords = null;

    function togglePhoneFrame() {{
      var app = document.getElementById('appContainer');
      var txt = document.getElementById('frameText');
      if (app.classList.contains('phone-frame')) {{
        app.classList.remove('phone-frame');
        app.classList.add('max-w-4xl', 'rounded-3xl', 'shadow-sm');
        if (txt) txt.innerText = 'สลับดูแบบจอมือถือ (Mobile)';
      }} else {{
        app.classList.add('phone-frame');
        app.classList.remove('max-w-4xl', 'rounded-3xl', 'shadow-sm');
        if (txt) txt.innerText = 'สลับดูแบบเต็มจอ (Desktop)';
      }}
    }}

    function switchTab(tabId) {{
      ['overview', 'table', 'prizes', 'admin'].forEach(function(t) {{
        var el = document.getElementById('tab-content-' + t);
        if (el) el.classList.add('hidden');
        var btn = document.getElementById('nav-btn-' + t);
        if (btn) {{
          btn.classList.remove('text-[#2D6A4F]');
          btn.classList.add('text-[#9E968D]');
        }}
      }});

      var target = document.getElementById('tab-content-' + tabId);
      if (target) target.classList.remove('hidden');

      var activeBtn = document.getElementById('nav-btn-' + tabId);
      if (activeBtn) {{
        activeBtn.classList.remove('text-[#9E968D]');
        activeBtn.classList.add('text-[#2D6A4F]');
      }}

      window.scrollTo({{ top: 0, behavior: 'smooth' }});
    }}

    function filterCol(col) {{
      var btns = ['btnColAll', 'btnColTop', 'btnColBot', 'btnColTopBot'];
      btns.forEach(function(id) {{
        var b = document.getElementById(id);
        if (b) {{
          b.classList.remove('bg-[#FFFFFF]', 'text-[#2A2421]', 'border', 'border-[#DDD5C7]', 'shadow-xs', 'font-semibold');
          b.classList.add('text-[#6D655E]', 'font-medium');
        }}
      }});

      var activeId = col === 'all' ? 'btnColAll' : (col === 'บน' ? 'btnColTop' : (col === 'ล่าง' ? 'btnColBot' : 'btnColTopBot'));
      var act = document.getElementById(activeId);
      if (act) {{
        act.classList.add('bg-[#FFFFFF]', 'text-[#2A2421]', 'border', 'border-[#DDD5C7]', 'shadow-xs', 'font-semibold');
        act.classList.remove('text-[#6D655E]', 'font-medium');
      }}

      var rows = document.querySelectorAll('.row-entry');
      rows.forEach(function(r) {{
        if (col === 'all') {{
          r.style.display = 'flex';
        }} else {{
          var cat = r.getAttribute('data-cat') || '';
          r.style.display = (cat === col) ? 'flex' : 'none';
        }}
      }});
      showToast('กรองตาราง: ' + (col === 'all' ? 'ทุกหมวด' : col));
    }}

    function openSearchModal() {{
      var modal = document.getElementById('searchModal');
      if (modal) modal.classList.remove('hidden');
      var inp = document.getElementById('instantSearchInput');
      if (inp) {{
        inp.focus();
        inp.select();
      }}
      renderQuickPills();
      doSearch();
    }}

    function closeSearchModal() {{
      var modal = document.getElementById('searchModal');
      if (modal) modal.classList.add('hidden');
    }}

    function renderQuickPills() {{
      var pillsBox = document.getElementById('searchQuickPills');
      if (!pillsBox) return;
      var topNums = [];
      var seen = {{}};
      for (var i = 0; i < allEntries.length; i++) {{
        var n = allEntries[i].num;
        if (n && !seen[n]) {{
          seen[n] = true;
          topNums.push(n);
          if (topNums.length >= 4) break;
        }}
      }}
      var html = '<span class="text-[#7D756D] text-[11px]">ตัวอย่าง:</span>';
      topNums.forEach(function(num) {{
        html += '<button onclick="setSearchNum(\\'' + num + '\\')" class="px-2 py-0.5 rounded-xl bg-[#E8F3EB] text-[#2D6A4F] font-mono text-xs border border-[#CFE4D4] cursor-pointer">' + num + '</button>';
      }});
      pillsBox.innerHTML = html;
    }}

    function setSearchNum(n) {{
      var inp = document.getElementById('instantSearchInput');
      if (inp) inp.value = n;
      doSearch();
    }}

    function quickSearchNum(n) {{
      openSearchModal();
      setSearchNum(n);
    }}

    function doSearch() {{
      var query = (document.getElementById('instantSearchInput').value || '').trim();
      var box = document.getElementById('searchResultsBox');
      if (!box) return;

      if (!query) {{
        box.innerHTML = '<div class="text-center py-5 text-[#8A8279] text-xs">พิมพ์ตัวเลขเพื่อค้นหาในงวดนี้ (' + allEntries.length + ' รายการ)</div>';
        return;
      }}

      var filtered = allEntries.filter(function(item) {{
        return item.num.indexOf(query) !== -1;
      }});

      if (filtered.length === 0) {{
        box.innerHTML = '<div class="text-center py-5 text-[#8A8279] text-xs">ไม่พบตัวเลข ' + query + ' ในงวดนี้</div>';
        return;
      }}

      var sumTotal = 0;
      filtered.forEach(function(item) {{
        var p = parseFloat((item.price || '').replace(/[^0-9.]/g, '')) || 0;
        sumTotal += p;
      }});

      var html = '<div class="text-[11px] text-[#7D756D] mb-1 font-medium flex items-center justify-between">' +
                    '<span>พบ ' + filtered.length + ' รายการ</span>' +
                    '<span class="font-mono text-[#2D6A4F] font-bold">รวม ' + sumTotal.toLocaleString() + ' ฿</span>' +
                 '</div>';

      filtered.forEach(function(item) {{
        var catClass = item.cat === 'บน' ? 'text-[#2D6A4F]' : (item.cat === 'ล่าง' ? 'text-[#6B5384]' : 'text-[#A35C2B]');
        var rawJson = JSON.stringify(item).replace(/"/g, '&quot;');
        html += '<div class="p-2.5 rounded-2xl bg-[#F9F6F0] border border-[#EAE3D8] flex items-center justify-between text-xs">' +
                  '<div>' +
                    '<div class="flex items-baseline gap-1.5 font-mono text-xs font-bold text-[#2A2421]">' +
                      '<span>' + item.num + '</span>' +
                      '<span class="text-[#A8A095] font-sans">=</span>' +
                      '<span class="text-[#2D6A4F]">' + item.price + '</span>' +
                    '</div>' +
                    '<div class="text-[10px] text-[#7D756D] mt-0.5">' +
                      '<span class="' + catClass + ' font-medium">[' + item.cat + ']</span> • ใบที่ ' + item.sheet + ' (' + item.worker + ')' +
                    '</div>' +
                  '</div>' +
                  '<button onclick="closeSearchModal(); inspectItemDirect(' + rawJson + ')" class="text-xs font-medium text-[#4A423B] hover:text-[#2A2421] bg-[#FFFFFF] px-2.5 py-1 rounded-xl border border-[#DDD5C7] shadow-xs cursor-pointer transition-colors">' +
                    'ส่องภาพ' +
                  '</button>' +
                '</div>';
      }});

      box.innerHTML = html;
    }}

    function inspectItemDirect(item) {{
      var fakeEl = {{
        getAttribute: function(attr) {{
          if (attr === 'data-num') return item.num + ' = ' + item.price;
          if (attr === 'data-sheet') return item.sheet;
          if (attr === 'data-worker') return item.worker;
          if (attr === 'data-catname') return '[' + item.cat + ']';
          if (attr === 'data-time') return item.time;
          if (attr === 'data-img') return item.img;
          if (attr === 'data-box') return item.box;
          if (attr === 'data-note') return item.note;
          return '';
        }}
      }};
      openAuditSpotlight(fakeEl);
    }}

    function openAuditSpotlight(el) {{
      var num = el.getAttribute('data-num') || '';
      var sheet = el.getAttribute('data-sheet') || '';
      var worker = el.getAttribute('data-worker') || '';
      var cat = el.getAttribute('data-catname') || '';
      var timeStr = el.getAttribute('data-time') || '';
      var img = el.getAttribute('data-img') || '';
      var box = el.getAttribute('data-box') || '';
      var note = el.getAttribute('data-note') || '';

      document.getElementById('auditNumTag').innerText = num;
      document.getElementById('auditSubMeta').innerText = 'ใบที่ ' + sheet + ' • ' + cat + ' • ' + worker;

      var banner = document.getElementById('auditBraceBanner');
      if (note && note.indexOf('ปีกกา') !== -1) {{
        document.getElementById('auditBraceDesc').innerText = note;
        banner.classList.remove('hidden');
      }} else {{
        banner.classList.add('hidden');
      }}

      auditBoxCoords = null;
      if (box && box.trim()) {{
        try {{
          var cleanBox = box.replace(/[\[\]]/g, '').trim();
          var parts = cleanBox.split(',').map(function(p) {{ return parseFloat(p.trim()); }});
          if (parts.length === 4 && !parts.some(isNaN)) {{
            auditBoxCoords = parts;
          }}
        }} catch(e) {{
          console.error(e);
        }}
      }}

      var imgEl = document.getElementById('auditImg');
      var placeholderEl = document.getElementById('auditEmptyPlaceholder');
      var zoomCont = document.getElementById('auditZoomContainer');
      var fullLink = document.getElementById('auditFullLink');

      if (img && img.trim()) {{
        placeholderEl.style.display = 'none';
        zoomCont.style.display = 'block';
        imgEl.src = img;
        if (fullLink) {{
          fullLink.href = img;
          fullLink.style.display = 'inline-block';
        }}
      }} else {{
        zoomCont.style.display = 'none';
        placeholderEl.style.display = 'block';
        if (fullLink) fullLink.style.display = 'none';
      }}

      var editBtn = document.getElementById('auditEditBtn');
      if (editBtn) {{
        if (sheet && sheet.trim()) {{
          editBtn.href = '/sheet/edit/' + encodeURIComponent(sheet.trim());
          editBtn.style.display = 'flex';
        }} else {{
          editBtn.style.display = 'none';
        }}
      }}

      document.getElementById('auditModal').classList.remove('hidden');
      document.body.style.overflow = 'hidden';
    }}

    function onAuditImageLoaded() {{
      var vp = document.getElementById('auditViewport');
      var ring = document.getElementById('auditSpotlightRing');
      var imgEl = document.getElementById('auditImg');
      var zoomCont = document.getElementById('auditZoomContainer');

      zoomCont.style.display = 'block';
      
      if (auditBoxCoords && auditBoxCoords.length === 4) {{
        var ymin = auditBoxCoords[0];
        var xmin = auditBoxCoords[1];
        var ymax = auditBoxCoords[2];
        var xmax = auditBoxCoords[3];
        currentAuditScale = 2.5;

        ring.style.top = (ymin / 10.0) + '%';
        ring.style.left = (xmin / 10.0) + '%';
        ring.style.height = ((ymax - ymin) / 10.0) + '%';
        ring.style.width = ((xmax - xmin) / 10.0) + '%';
        ring.style.display = 'block';

        var vpW = vp.clientWidth || 400;
        var vpH = vp.clientHeight || 250;
        var imgW = imgEl.clientWidth || 400;
        var imgH = imgEl.clientHeight || 600;

        var targetX = ((xmin + xmax) / 2000.0) * imgW;
        var targetY = ((ymin + ymax) / 2000.0) * imgH;

        currentAuditX = (vpW / 2) - (targetX * currentAuditScale);
        currentAuditY = (vpH / 2) - (targetY * currentAuditScale);
      }} else {{
        currentAuditScale = 1.0;
        currentAuditX = 0;
        currentAuditY = 0;
        ring.style.display = 'none';
      }}
      applyAuditTransform();
    }}

    function applyAuditTransform() {{
      var cont = document.getElementById('auditZoomContainer');
      if (cont) {{
        cont.style.transform = 'translate(' + currentAuditX + 'px, ' + currentAuditY + 'px) scale(' + currentAuditScale + ')';
      }}
      var ind = document.getElementById('auditZoomIndicator');
      if (ind) {{
        ind.innerText = 'ซูม ' + Math.round(currentAuditScale * 100) + '%';
      }}
    }}

    function zoomAudit(delta) {{
      currentAuditScale = Math.max(0.5, Math.min(6.0, currentAuditScale + delta));
      applyAuditTransform();
    }}

    function resetAuditZoom() {{
      currentAuditScale = 1.0;
      currentAuditX = 0;
      currentAuditY = 0;
      applyAuditTransform();
    }}

    function closeAuditModal() {{
      var modal = document.getElementById('auditModal');
      if (modal) modal.classList.add('hidden');
      document.body.style.overflow = '';
    }}

    function showToast(msg) {{
      var toast = document.getElementById('toast');
      if (!toast) return;
      toast.innerText = msg;
      toast.classList.remove('opacity-0', '-translate-y-3');
      toast.classList.add('opacity-100', 'translate-y-0');
      setTimeout(function() {{
        toast.classList.remove('opacity-100', 'translate-y-0');
        toast.classList.add('opacity-0', '-translate-y-3');
      }}, 2000);
    }}

    // Mouse & Touch Pan Listeners for Spotlight Canvas
    window.addEventListener('DOMContentLoaded', function() {{
      var vp = document.getElementById('auditViewport');
      if (!vp) return;

      vp.addEventListener('mousedown', function(e) {{
        isPanningAudit = true;
        panStartX = e.clientX - currentAuditX;
        panStartY = e.clientY - currentAuditY;
        vp.style.cursor = 'grabbing';
      }});

      window.addEventListener('mousemove', function(e) {{
        if (!isPanningAudit) return;
        currentAuditX = e.clientX - panStartX;
        currentAuditY = e.clientY - panStartY;
        applyAuditTransform();
      }});

      window.addEventListener('mouseup', function() {{
        isPanningAudit = false;
        if (vp) vp.style.cursor = 'grab';
      }});

      vp.addEventListener('touchstart', function(e) {{
        if (e.touches.length === 1) {{
          isPanningAudit = true;
          panStartX = e.touches[0].clientX - currentAuditX;
          panStartY = e.touches[0].clientY - currentAuditY;
        }}
      }}, {{ passive: true }});

      window.addEventListener('touchmove', function(e) {{
        if (!isPanningAudit || e.touches.length !== 1) return;
        currentAuditX = e.touches[0].clientX - panStartX;
        currentAuditY = e.touches[0].clientY - panStartY;
        applyAuditTransform();
      }}, {{ passive: true }});

      window.addEventListener('touchend', function() {{
        isPanningAudit = false;
      }});

      window.addEventListener('keydown', function(e) {{
        if (e.key === 'Escape') closeAuditModal();
      }});
    }});
  </script>
</body>
</html>
"""
    return html

class LineWebhookHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            import ocr_engine
            info = {
                "status": "ok",
                "version": "v2.3-confident-ocr",
                "primary_model": f"{ocr_engine.MODELS_CONFIG[0][0]}/{ocr_engine.MODELS_CONFIG[0][1]}",
                "models": ocr_engine.MODELS_CONFIG,
                "api_key_configured": bool(ocr_engine.API_KEY),
                "api_key_prefix": ocr_engine.API_KEY[:6] + "..." if ocr_engine.API_KEY else "none"
            }
            self.wfile.write(json.dumps(info).encode("utf-8"))
            return

        if path == "/test-gemini":
            import ocr_engine
            res = ocr_engine.test_gemini_connection()
            status_code = 200 if res.get("status") == "success" else 500
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(res, indent=2).encode("utf-8"))
            return

        if path == "/export":
            p_id = int(qs.get("period_id", [0])[0]) or None
            csv_path = database.export_csv(p_id)
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8-sig")
            self.send_header("Content-Disposition", 'attachment; filename="report_all.csv"')
            self.end_headers()
            with open(csv_path, "rb") as f:
                self.wfile.write(f.read())
            return

        if path.startswith("/uploads/"):
            clean_subpath = os.path.normpath(path.replace("/uploads/", "", 1))
            full_path = os.path.join(database.UPLOADS_DIR, clean_subpath)
            if not full_path.startswith(database.UPLOADS_DIR) or not os.path.isfile(full_path):
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Image not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            with open(full_path, "rb") as f:
                self.wfile.write(f.read())
            return

        if path == "/export-ai-dataset":
            p_id = int(qs.get("period_id", [0])[0]) or None
            conn = database.get_db_connection()
            cursor = conn.cursor()
            p_filter = "WHERE s.period_id = ?" if p_id else ""
            p_params = (p_id,) if p_id else ()
            cursor.execute(f"""
            SELECT s.sheet_id, s.period_id, s.worker_code, s.employee_name, s.image_path, s.raw_json, s.created_at
            FROM sheets s
            {p_filter}
            ORDER BY s.id DESC
            """, p_params)
            sheets_rows = cursor.fetchall()
            dataset = []
            for sr in sheets_rows:
                sid = sr["sheet_id"]
                cursor.execute("""
                SELECT category, set1, set3, set2, is_valid
                FROM entries
                WHERE sheet_id = ?
                """, (sid,))
                e_rows = [dict(er) for er in cursor.fetchall()]
                raw_ocr = {}
                try:
                    raw_ocr = json.loads(sr["raw_json"])
                except Exception:
                    pass
                dataset.append({
                    "sheet_id": sid,
                    "worker_code": sr["worker_code"],
                    "employee_name": sr["employee_name"],
                    "image_url": f"/{sr['image_path']}" if sr["image_path"] else "",
                    "raw_ocr": raw_ocr,
                    "ground_truth_entries": e_rows,
                    "created_at": sr["created_at"]
                })
            conn.close()
            payload = json.dumps(dataset, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="ai_training_dataset.json"')
            self.end_headers()
            self.wfile.write(payload)
            return

        if path == "/export-ai-bundle":
            p_id = int(qs.get("period_id", [0])[0]) or None
            import ai_training_pipeline
            zip_bytes = ai_training_pipeline.build_training_bundle(p_id)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="paper_ocr_training_bundle.zip"')
            self.send_header("Content-Length", str(len(zip_bytes)))
            self.end_headers()
            self.wfile.write(zip_bytes)
            return

        if path == "/colab-notebook":
            nb_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Paper_OCR_FineTune_Colab.ipynb")
            if os.path.exists(nb_path):
                with open(nb_path, "rb") as f:
                    nb_bytes = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ipynb+json; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="Paper_OCR_FineTune_Colab.ipynb"')
                self.send_header("Content-Length", str(len(nb_bytes)))
                self.end_headers()
                self.wfile.write(nb_bytes)
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Notebook not found")
            return


        if path.startswith("/edit/"):
            m = re.match(r"^/edit/(\d+)$", path)
            if m:
                scan_id = int(m.group(1))
                html = render_edit_page(scan_id).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html)
                return

        if path.startswith("/sheet/edit/"):
            sheet_id = urllib.parse.unquote(path.replace("/sheet/edit/", "", 1).strip())
            if sheet_id:
                html = render_confirmed_sheet_edit_page(sheet_id).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html)
                return
        if path == "/prizes":
            import prize_service
            p_id = int(qs.get("period_id", [0])[0]) or None
            top3 = qs.get("top3", [""])[0]
            bottom2 = qs.get("bottom2", [""])[0]
            custom_p = None
            if top3 or bottom2:
                custom_p = {"top3": top3, "bottom2": bottom2}
            html = prize_service.render_prizes_page(p_id, custom_p).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html)
            return

        if path == "/api/search":
            q_num = qs.get("q", [""])[0].strip()
            p_id = int(qs.get("period_id", [0])[0]) or None
            results = database.deep_search_set1(q_num, p_id)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(results, ensure_ascii=False).encode("utf-8"))
            return

        if path == "/setup-rich-menu":
            import setup_rich_menu
            res = setup_rich_menu.setup_rich_menu()
            if isinstance(res, tuple):
                success, msg_detail = res
            else:
                success, msg_detail = res, ""
                
            if success:
                status_header = "✅ ติดตั้ง LINE Rich Menu (3 ปุ่ม) สำเร็จเรียบร้อยแล้ว!"
                box_color = "#16a34a"
            else:
                status_header = "❌ การติดตั้งไม่สำเร็จ"
                box_color = "#dc2626"
                
            res_html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'><title>Setup Rich Menu</title>
<style>body{{font-family:sans-serif;background:#f8fafc;padding:40px;text-align:center;}}
.box{{background:white;padding:30px;border-radius:16px;max-width:550px;margin:auto;box-shadow:0 4px 6px rgba(0,0,0,0.05);}}
a{{display:inline-block;margin-top:20px;padding:10px 20px;background:#2563eb;color:white;text-decoration:none;border-radius:8px;font-weight:700;}}
</style></head>
<body><div class='box'>
    <h2 style='color:{box_color};'>{status_header}</h2>
    <p style='color:#475569; font-size:14px; background:#f1f5f9; padding:12px; border-radius:8px; word-break:break-all;'>{msg_detail}</p>
    <a href='/'>กลับสู่หน้าหลัก Dashboard</a>
</div></body></html>""".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(res_html)
            return

        if path == "/backup-gdrive":
            import gdrive_sync
            trigger = qs.get("trigger", ["0"])[0]
            msg = "☁️ ระบบสำรองข้อมูลอัตโนมัติ Google Drive 5TB"
            if trigger == "1":
                gdrive_sync._do_backup_db()
                msg = "✅ สั่งสำรอง records.db ขึ้น Google Drive เรียบร้อย!"
            
            configured = gdrive_sync.is_configured()
            cfg_badge = "<span style='color:#16a34a; font-weight:700;'>🟢 เชื่อมต่อ Google Apps Script แล้ว</span>" if configured else "<span style='color:#dc2626; font-weight:700;'>🔴 ยังไม่ได้ใส่ GDRIVE_SYNC_URL ใน Render</span>"
            script_code = gdrive_sync.GOOGLE_APPS_SCRIPT_CODE.strip()
            
            gdrive_html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'><title>Google Drive Backup</title>
<style>
body{{font-family:sans-serif;background:#f8fafc;padding:24px;margin:0;color:#1e293b;}}
.box{{background:white;padding:24px;border-radius:16px;max-width:680px;margin:auto;box-shadow:0 4px 6px rgba(0,0,0,0.05);}}
pre{{background:#0f172a;color:#38bdf8;padding:14px;border-radius:10px;font-size:12px;overflow-x:auto;text-align:left;}}
a.btn{{display:inline-block;padding:10px 18px;background:#2563eb;color:white;text-decoration:none;border-radius:8px;font-weight:700;margin-right:8px;}}
</style></head>
<body>
<div class='box'>
    <h2>☁️ สำรองข้อมูลอัตโนมัติ Google Drive 5TB</h2>
    <p style='margin:6px 0 14px 0; font-size:14px;'>{msg}</p>
    <p>สถานะการเชื่อมต่อ: {cfg_badge}</p>
    <div style='margin:16px 0;'>
        <a href='/backup-gdrive?trigger=1' class='btn' style='background:#059669;'>📤 สั่งสำรอง DB ตอนนี้ทันที</a>
        <a href='/' class='btn' style='background:#64748b;'>กลับหน้าหลัก Dashboard</a>
    </div>
    <div style='text-align:left; background:#eff6ff; padding:16px; border-radius:10px; font-size:13px; line-height:1.6;'>
        <b>📌 วิธีเชื่อมต่อ Google Drive 5TB แบบง่ายที่สุด (ทำครั้งเดียวเสร็จ):</b>
        <ol>
            <li>เข้าเว็บ <b>script.google.com</b> ด้วยบัญชี Google ที่มี Drive 5TB</li>
            <li>กด <b>New Project</b> แล้วนำโค้ดด้านล่างนี้ไปวางทับทั้งหมด</li>
            <li>กดปุ่ม <b>Deploy</b> ➔ <b>New deployment</b> ➔ เลือกชนิด <b>Web app</b></li>
            <li>ตั้งค่า: <i>Execute as: Me</i> และ <i>Who has access: Anyone</i></li>
            <li>กด Deploy แล้วคัดลอก URL เว็บแอป (https://script.google.com/macros/s/.../exec)</li>
            <li>นำ URL นั้นไปใส่ใน Environment Variables ของ Render ชื่อ <b>GDRIVE_SYNC_URL</b></li>
        </ol>
    </div>
    <h4 style='text-align:left; margin-top:20px;'>📋 โค้ด Google Apps Script (พร้อมใช้งานทันที):</h4>
    <pre>{script_code}</pre>
</div>
</body></html>""".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(gdrive_html)
            return

        # Default: Dashboard
        p_id = int(qs.get("period_id", [0])[0]) or None
        scope = qs.get("scope", ["period"])[0]
        tab = qs.get("tab", ["live"])[0]
        html_content = render_html_dashboard(p_id, scope, tab).encode('utf-8')
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html_content)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 1. Period Toggle Form POST
        if path == "/api/period/toggle":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            form = urllib.parse.parse_qs(body)
            action = form.get("action", [""])[0]
            if action == "close":
                database.close_active_period()
            elif action == "reopen":
                database.reopen_latest_period()
            elif action == "new":
                custom_name = form.get("period_name", [""])[0].strip()
                database.open_new_period(custom_name if custom_name else None)
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        # 1.5. User Approval Form POST
        if path == "/api/user/approve":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            form = urllib.parse.parse_qs(body)
            uid = form.get("user_id", [""])[0].strip()
            wcode = form.get("worker_code", ["A"])[0].strip().upper() or "A"
            if uid:
                updated = database.approve_user(uid, wcode)
                if updated:
                    push_line_message(updated["user_id"], f"🎉 คุณได้รับการอนุมัติให้ใช้งานระบบแล้วครับ!\n🏷️ รหัสประจำตัวของคุณคือ: [{wcode}]\n💡 คุณสามารถเริ่มส่งรูปกระดาษบันทึกได้เลยครับ")
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        if path == "/api/user/block":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            form = urllib.parse.parse_qs(body)
            uid = form.get("user_id", [""])[0].strip()
            if uid:
                database.block_user(uid)
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        # 2. Quick Edit Save Form POST (Pending scan)
        if path.startswith("/edit/"):
            m = re.match(r"^/edit/(\d+)$", path)
            if m:
                scan_id = int(m.group(1))
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length).decode("utf-8")
                form = urllib.parse.parse_qs(body)
                
                new_columns = parse_columns_from_form(form)
                database.update_pending_scan_items(scan_id, new_columns)
                scan = database.get_pending_scan(scan_id)
                sheet_id = scan.get("sheet_id", "A-01") if scan else "A-01"
                
                confirmed = database.confirm_pending_scan(scan_id)
                if confirmed:
                    import gdrive_sync
                    gdrive_sync.trigger_db_backup()
                
                success_html = render_edit_success_page(sheet_id, is_confirmed=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(success_html)
                return

        # 2.5. Confirmed Sheet Edit Save Form POST
        if path.startswith("/sheet/edit/"):
            sheet_id = urllib.parse.unquote(path.replace("/sheet/edit/", "", 1).strip())
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            form = urllib.parse.parse_qs(body)
            
            new_columns = parse_columns_from_form(form)
            updated = database.update_confirmed_sheet_entries(sheet_id, new_columns)
            if updated:
                import gdrive_sync
                gdrive_sync.trigger_db_backup()
                
            success_html = render_edit_success_page(sheet_id, is_confirmed=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(success_html)
            return

        # 3. LINE Webhook POST
        if path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return
            
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length)
        signature = self.headers.get("X-Line-Signature", "")
        
        if not verify_line_signature(body_bytes, signature):
            print("Invalid signature rejected!")
            self.send_response(403)
            self.end_headers()
            return
            
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
        
        try:
            payload = json.loads(body_bytes.decode('utf-8'))
            events = payload.get("events", [])
            for ev in events:
                ev_type = ev.get("type")
                reply_token = ev.get("replyToken")
                source = ev.get("source", {})
                user_id = source.get("userId", "")
                
                if not user_id:
                    continue
                    
                user = database.get_user(user_id)
                is_owner = (user_id == OWNER_USER_ID) or (user and user.get("role") == "owner")
                
                # Check for claiming ownership if not already owner
                if ev_type == "message":
                    msg_obj = ev.get("message", {})
                    if msg_obj.get("type") == "text":
                        txt_clean = (msg_obj.get("text") or "").strip()
                        if txt_clean in ["ฉันคือเจ้าของ", "owner", "admin 1234", "ตั้งเป็นเจ้าของ"]:
                            display_name = get_line_profile(user_id)
                            database.set_owner(user_id, display_name)
                            reply_line_message(reply_token, f"👑 ยินดีต้อนรับครับ!\nระบบได้ตั้งค่าให้คุณ ({display_name}) เป็น [เจ้าของระบบ (Owner)] เรียบร้อยแล้วครับ!\n💡 คุณสามารถสั่ง 'อนุมัติ', 'ปิดงวด', 'เปิดงวด' ได้ทุกคำสั่งครับ")
                            continue
                
                if not is_owner:
                    if user is None:
                        # New user — register and approve automatically
                        display_name = get_line_profile(user_id)
                        database.register_pending_user(user_id, display_name)
                        user = database.get_user(user_id)
                    elif user.get("status") == "BLOCKED":
                        continue

                if ev_type == "message":
                    msg = ev.get("message", {})
                    msg_type = msg.get("type")
                    if msg_type == "image":
                        with _user_jobs_mutex:
                            curr_jobs = _user_active_jobs.get(user_id, 0) + 1
                            _user_active_jobs[user_id] = curr_jobs
                            job_idx = curr_jobs

                        if job_idx > 1:
                            push_line_message(user_id, f"📥 ได้รับรูปที่ {job_idx} เรียบร้อยแล้วครับ!\n🔄 กำลังอ่านข้อมูลตามลำดับ กรุณารอสักครู่...")

                        # Process OCR asynchronously in background thread so webhook responds immediately
                        threading.Thread(
                            target=handle_image_message,
                            args=(msg.get("id"), reply_token, user_id, user or {}, job_idx),
                            daemon=True
                        ).start()
                    elif msg_type == "text":
                        handle_text_message(msg.get("text", ""), reply_token, user_id, is_owner, user or {})
                        
        except Exception as e:
            print(f"Error handling event: {e}")

def run_server():
    import gdrive_sync
    gdrive_sync.restore_db_from_gdrive()
    database.init_db()
    server_cls = getattr(http.server, "ThreadingHTTPServer", http.server.HTTPServer)
    server = server_cls(("0.0.0.0", PORT), LineWebhookHandler)
    print(f"🚀 LINE Bot Server running on port {PORT} (Threaded)...")
    server.serve_forever()

if __name__ == "__main__":
    run_server()
