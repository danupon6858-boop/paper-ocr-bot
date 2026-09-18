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
from typing import List, Dict, Optional, Tuple
from config import LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, OWNER_USER_ID
import ocr_engine
from validator import OCRValidator
import database
import query_service

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


def handle_image_message(message_id: str, reply_token: str, user_id: str, user_info: dict):
    # 1. Check Period Status
    active_p = database.get_active_period()
    if not active_p:
        msg = (
            "⛔️ ขออภัยครับ ขณะนี้ระบบปิดรับข้อมูล (ยังไม่เปิดงวดใหม่)\n"
            "กรุณารอเจ้าของเปิดงวดก่อนส่งรูปครับ\n\n"
            f"🌐 ตรวจสอบสถานะงวดได้ที่:\n{BASE_URL}"
        )
        deliver_message(user_id, reply_token, msg)
        return

    # Start 60s typing indicator (native animation in LINE chat; no chat clutter, preserves replyToken)
    send_line_loading_indicator(user_id, 60)

    try:
        worker_code = user_info.get("worker_code", "A")
        emp_name = user_info.get("display_name", "พนักงาน")

        print(f"Downloading image from {emp_name} ({worker_code})...")
        img_bytes = get_line_image_content(message_id)
        
        print("Processing OCR with Gemini 3.6 Flash...")
        ocr_start_time = time.time()
        ocr_result = ocr_engine.extract_from_image(img_bytes)
        ocr_latency_ms = int((time.time() - ocr_start_time) * 1000)
        
        # Check if any data extracted
        raw_cols = ocr_result.get("columns", {}) if ocr_result else {}
        total_raw = sum(len(items) for items in raw_cols.values())
        if total_raw == 0:
            no_data_msg = (
                "⚠️ ภาพนี้ระบบอ่านตัวเลขไม่พบ หรือลายมือไม่ชัดเจนครับ\n"
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

        print("Validating rules...")
        val_result = OCRValidator.validate_document(ocr_result)
        
        # Prepend worker code to sheet_id if not already there
        header = ocr_result.get("header") if isinstance(ocr_result.get("header"), dict) else {}
        ocr_result["header"] = header
        raw_sheet_id = str(header.get("sheet_id") or "").strip()
        if raw_sheet_id and raw_sheet_id.lower() not in ["none", "null", "n/a", ""]:
            formatted_sheet_id = f"{worker_code}-{raw_sheet_id}" if not raw_sheet_id.startswith(f"{worker_code}-") else raw_sheet_id
        else:
            formatted_sheet_id = f"{worker_code}-1"
            raw_sheet_id = "1"
            
        ocr_result["header"]["sheet_id"] = formatted_sheet_id
        ocr_result["header"]["customer_name"] = emp_name

        # Save uploaded image to disk for training archive and dashboard viewing
        image_path = database.save_uploaded_image(img_bytes, active_p["id"], formatted_sheet_id)
        if image_path:
            import gdrive_sync
            gdrive_sync.trigger_image_backup(image_path)

        # 2. Check Duplicate Sheet
        is_dup, dup_msg = database.check_duplicate_sheet(
            active_p["id"], formatted_sheet_id, val_result.get("validated_columns", {})
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

        # 3. Create Pending Scan
        scan_id = database.create_pending_scan(
            user_id, worker_code, emp_name, active_p["id"], formatted_sheet_id, ocr_result, val_result,
            image_path=image_path, ocr_latency_ms=ocr_latency_ms
        )


        valid_cols = val_result.get("validated_columns", {})
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

        # Combine summary header, clean editable numbers, and instructions into 1 single message bubble
        clean_text = format_clean_editable_text(raw_sheet_id, valid_cols)
        
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
        
        quick_replies = [
            ("✅ ยืนยัน", f"ยืนยัน {scan_id}"),
            ("❌ ยกเลิก", f"ยกเลิก {scan_id}")
        ]
        
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

    # 1. Confirm Pending Scan: "ยืนยัน 5" or "ยืนยัน"
    m_conf = re.match(r"^ยืนยัน(?:\s+(\d+))?$", clean_text)
    if m_conf:
        scan_id_str = m_conf.group(1)
        if scan_id_str:
            confirmed = database.confirm_pending_scan(int(scan_id_str))
        else:
            scan = database.get_latest_pending_scan(user_id)
            confirmed = database.confirm_pending_scan(scan["id"]) if scan else None
            
        if confirmed:
            import gdrive_sync
            gdrive_sync.trigger_db_backup()
            active_p = database.get_active_period()
            p_name = active_p["name"] if active_p else "งวดปัจจุบัน"
            msg = (
                f"✅ ยืนยันบันทึกข้อมูลเรียบร้อยแล้วครับ!\n"
                f"📋 [ใบที่: {confirmed['sheet_id']}]\n"
                f"📌 ข้อมูลถูกบันทึกลงใน: {p_name}\n\n"
                f"🌐 ดูกระดานสรุปสด: {BASE_URL}"
            )
            reply_line_message(reply_token, msg)
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
            reply_line_message(reply_token, "🗑️ ยกเลิกข้อมูลเรียบร้อยครับ สามารถถ่ายรูปใหม่ได้เลยครับ")
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
def render_edit_page(scan_id: int) -> str:
    scan = database.get_pending_scan(scan_id)
    if not scan:
        return "<h3>❌ ไม่พบรายการนี้ หรือรายการนี้ถูกบันทึกไปแล้ว</h3>"
    if scan["status"] != "PENDING":
        return f"<h3>⚠️ รายการนี้อยู่ในสถานะ '{scan['status']}' แล้ว ไม่สามารถแก้ไขซ้ำได้</h3>"
        
    val_data = json.loads(scan["val_json"])
    cols = val_data.get("validated_columns", {})
    sheet_id = scan["sheet_id"]
    emp_name = scan["emp_name"]
    
    # Render interactive input boxes
    def render_inputs(items, col_name, col_key):
        h = f"<div class='section-title'>{col_name} ({len(items)} รายการ)</div>"
        if not items:
            h += "<div style='color:#94a3b8; margin-bottom:10px;'>ไม่มีรายการในหมวดนี้</div>"
        for idx, itm in enumerate(items):
            s1 = itm.get('set1', '')
            s3 = itm.get('set3', '')
            s2 = itm.get('set2', '')
            h += f"""
            <div class="row-box">
                <span class="row-idx">#{idx+1}</span>
                <input type="text" name="{col_key}_set1_{idx}" value="{s1}" placeholder="ชุด 1" class="inp-set1">
                <span>=</span>
                <input type="text" name="{col_key}_set3_{idx}" value="{s3}" placeholder="ก3/ก6" class="inp-set3">
                <input type="text" name="{col_key}_set2_{idx}" value="{s2}" placeholder="ชุด 2" class="inp-set2">
            </div>
            """
        return h

    top_html = render_inputs(cols.get("top", []), "หมวด [ บน ]", "top")
    bot_html = render_inputs(cols.get("bottom", []), "หมวด [ ล่าง ]", "bottom")
    topbot_html = render_inputs(cols.get("top_bottom", []), "หมวด [ บนล่าง ]", "top_bottom")

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>แก้ไขตัวเลข - ใบที่ {sheet_id}</title>
    <style>
        * {{ box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Prompt", "Segoe UI", Roboto, sans-serif; }}
        body {{ background: #f1f5f9; padding: 12px; margin: 0; color: #1e293b; }}
        .header {{ background: white; padding: 14px; border-radius: 12px; margin-bottom: 14px; border: 1px solid #e2e8f0; }}
        .title {{ font-size: 18px; font-weight: 700; color: #0f172a; margin: 0; }}
        .sub {{ font-size: 13px; color: #64748b; margin-top: 4px; }}
        .section-title {{ font-weight: 700; font-size: 15px; margin: 16px 0 8px 0; color: #2563eb; }}
        .row-box {{ background: white; padding: 10px; border-radius: 10px; margin-bottom: 8px; display: flex; align-items: center; gap: 6px; border: 1px solid #e2e8f0; }}
        .row-idx {{ width: 30px; font-size: 13px; color: #94a3b8; font-weight: 600; }}
        input {{ padding: 10px 8px; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 16px; font-weight: 700; text-align: center; outline: none; }}
        input:focus {{ border-color: #2563eb; background: #eff6ff; }}
        .inp-set1 {{ width: 75px; }}
        .inp-set3 {{ width: 65px; color: #d97706; }}
        .inp-set2 {{ flex: 1; }}
        .btn-save {{ width: 100%; background: #059669; color: white; padding: 15px; border-radius: 12px; font-size: 17px; font-weight: 700; border: none; margin-top: 20px; cursor: pointer; }}
        .btn-cancel {{ width: 100%; background: #94a3b8; color: white; padding: 12px; border-radius: 12px; font-size: 15px; font-weight: 600; border: none; margin-top: 10px; cursor: pointer; text-align: center; text-decoration: none; display: block; }}
    </style>
</head>
<body>
    <div class="header">
        <h1 class="title">✏️ แตะแก้ไขตัวเลข [ใบที่: {sheet_id}]</h1>
        <div class="sub">ผู้ส่ง: {emp_name} | เอานิ้วจิ้มในช่องแล้วพิมพ์แก้ตัวเลขได้ทันที</div>
    </div>

    <form method="POST" action="/edit/{scan_id}">
        {top_html}
        {bot_html}
        {topbot_html}
        <button type="submit" class="btn-save">✅ บันทึกการแก้ไขและยืนยันข้อมูล</button>
        <a href="https://line.me" class="btn-cancel">กลับไปที่ LINE</a>
    </form>
</body>
</html>
"""
    return html

def render_html_dashboard(period_id: Optional[int] = None, scope: str = "period", active_tab: str = "live") -> str:
    all_periods = database.get_all_periods()
    active_period = database.get_active_period()
    latest_period = database.get_latest_period()
    
    # Determine which period to view
    if period_id:
        selected_p = next((p for p in all_periods if p["id"] == period_id), latest_period)
    else:
        selected_p = active_period or latest_period
        
    p_id = selected_p["id"] if selected_p else 1
    p_name = selected_p["name"] if selected_p else "งวดปัจจุบัน"
    p_is_open = (selected_p and selected_p.get("status") == "OPEN")

    summary = database.get_daily_summary(p_id)
    conn = database.get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT category, set1, set3, set2, is_valid
    FROM entries
    WHERE period_id = ?
    ORDER BY sheet_db_id ASC, id ASC
    """, (p_id,))
    rows = cursor.fetchall()

    # Recent sheet images
    cursor.execute("""
    SELECT sheet_id, employee_name, worker_code, image_path, created_at
    FROM sheets
    WHERE period_id = ? AND image_path != ''
    ORDER BY id DESC LIMIT 12
    """, (p_id,))
    recent_sheets = cursor.fetchall()
    conn.close()

    # Data for Tab 1 (Live Board)
    top_items = []
    bot_items = []
    topbot_items = []

    for r in rows:
        cat = r["category"]
        s1 = r["set1"].strip()
        s2 = r["set2"].strip()
        s3 = r["set3"].strip()
        s3_part = f"<span style='color:#d97706;font-weight:700'>{s3}</span> " if s3 else ""
        item_html = f"<strong>{s1}</strong> = {s3_part}{s2}"
        
        if cat == "บน":
            top_items.append(item_html)
        elif cat == "ล่าง":
            bot_items.append(item_html)
        elif cat == "บนล่าง":
            topbot_items.append(item_html)

    max_len = max(len(top_items), len(bot_items), len(topbot_items), 1)

    table_rows_html = ""
    if rows:
        for i in range(max_len):
            row_num = i + 1
            top_v = top_items[i] if i < len(top_items) else ""
            bot_v = bot_items[i] if i < len(bot_items) else ""
            topbot_v = topbot_items[i] if i < len(topbot_items) else ""
            table_rows_html += f"""
            <tr class="entry-row">
                <td style="color:#64748b; font-weight:600; text-align:center">{row_num}</td>
                <td class="col-val">{top_v}</td>
                <td class="col-val">{bot_v}</td>
                <td class="col-val">{topbot_v}</td>
            </tr>
            """
    else:
        table_rows_html = f"<tr><td colspan='4' style='text-align:center; padding:40px; color:#94a3b8;'>ยังไม่มีข้อมูลใน {p_name} ส่งรูปถ่ายผ่าน LINE เข้ามาได้เลยครับ</td></tr>"

    # Recent sheet photos HTML
    sheet_photos_html = ""
    if recent_sheets:
        badges = []
        for s in recent_sheets:
            sid = s["sheet_id"]
            img_url = f"/{s['image_path']}"
            w_code = s["worker_code"] or "A"
            badges.append(f'<a href="{img_url}" target="_blank" class="sheet-photo-badge">📷 {sid} ({w_code})</a>')
        sheet_photos_html = f"""
        <div class="sheet-photos-bar">
            <span style="font-size:13px; font-weight:700; color:#475569;">🖼️ รูปกระดาษจริง ({len(recent_sheets)} ใบล่าสุด):</span>
            <div class="sheet-photos-list">{' '.join(badges)}</div>
        </div>
        """

    # Data for Tab 2 (Set 1 Insights)
    is_all_time = (scope == "all")
    target_pid = None if is_all_time else p_id
    s1_stats = database.get_set1_analytics(target_pid)
    tot_s1 = s1_stats["total_entries"] or 1

    cnt2 = s1_stats["length_counts"].get("2", 0)
    cnt3 = s1_stats["length_counts"].get("3", 0)
    cnt4 = s1_stats["length_counts"].get("4", 0)
    pct2 = round(cnt2 / tot_s1 * 100, 1)
    pct3 = round(cnt3 / tot_s1 * 100, 1)
    pct4 = round(cnt4 / tot_s1 * 100, 1)

    pat_counts = s1_stats["pattern_counts"]
    
    top20_html = ""
    for idx, item in enumerate(s1_stats["top_by_freq"], 1):
        num = item["set1"]
        pat = database.classify_number_pattern(num)
        cnt = item["count"]
        vol_str = f"{int(item['volume']):,}" if item["volume"] > 0 else "-"
        top20_html += f"""
        <tr>
            <td style="text-align:center; font-weight:700; color:#64748b;">#{idx}</td>
            <td><strong style="font-size:18px; color:#0f172a; font-family:monospace;">{num}</strong></td>
            <td><span class="badge-pat">{pat}</span></td>
            <td style="text-align:center; font-weight:800; color:#2563eb;">{cnt} ครั้ง</td>
            <td style="text-align:center; color:#1d4ed8; font-weight:600;">{item['top']}</td>
            <td style="text-align:center; color:#dc2626; font-weight:600;">{item['bottom']}</td>
            <td style="text-align:center; color:#7c3aed; font-weight:600;">{item['top_bottom']}</td>
            <td style="text-align:right; font-weight:700; color:#059669;">{vol_str}</td>
        </tr>
        """
    if not top20_html:
        top20_html = "<tr><td colspan='8' style='text-align:center; color:#94a3b8; padding:20px;'>ยังไม่มีข้อมูลตัวเลข</td></tr>"

    # Data for Tab 3 (AI Feedback & Accuracy)
    ai_metrics = database.get_ai_feedback_metrics(target_pid)
    
    misread_html = ""
    for pair, count in ai_metrics["top_misread"]:
        misread_html += f"""
        <tr>
            <td><strong style="color:#dc2626; font-size:16px;">{pair}</strong></td>
            <td style="text-align:center; font-weight:800; color:#0f172a;">{count} ครั้ง</td>
        </tr>
        """
    if not misread_html:
        misread_html = "<tr><td colspan='2' style='text-align:center; color:#16a34a; padding:20px;'>✅ ยอดเยี่ยม! ยังไม่พบประวัติการอ่านผิดซ้ำในชุดข้อมูลนี้</td></tr>"

    corr_html = ""
    for c in ai_metrics["recent_corrections"][:30]:
        t_str = c["created_at"][11:16] if len(c.get("created_at", "")) >= 16 else ""
        sid = c["sheet_id"] or "-"
        w_code = c["worker_code"] or "A"
        act = c["action_type"]
        act_badge = '<span class="badge-edit">แก้ไข</span>' if act == 'SINGLE_EDIT' else ('<span class="badge-del">ลบออก</span>' if act == 'DELETE' else '<span class="badge-bulk">แก้ทั้งใบ</span>')
        old_val = c.get("old_set1", "") or "-"
        new_val = c.get("new_set1", "") or "-"
        detail = f"{old_val} ➔ <strong>{new_val}</strong>" if act == 'SINGLE_EDIT' else (f"ลบเลข {old_val}" if act == 'DELETE' else "แก้ข้อมูลตาราง")
        corr_html += f"""
        <tr>
            <td style="color:#64748b; font-size:12px;">{t_str}</td>
            <td><strong>{sid}</strong></td>
            <td style="text-align:center;"><span class="badge-worker">{w_code}</span></td>
            <td style="text-align:center;">{act_badge}</td>
            <td>{detail}</td>
            <td style="color:#64748b; font-size:12px;">{c.get('note', '')}</td>
        </tr>
        """
    if not corr_html:
        corr_html = "<tr><td colspan='6' style='text-align:center; color:#94a3b8; padding:20px;'>ยังไม่มีบันทึกการแก้ไข</td></tr>"

    # Data for Tab 4 (Staff & Peak Hours)
    staff_metrics = database.get_staff_and_peak_metrics(target_pid)
    
    hourly_bars_html = ""
    max_hour_count = max(staff_metrics["hourly"].values(), default=1) or 1
    for h_int in range(6, 23):
        h_str = f"{h_int:02d}"
        cnt = staff_metrics["hourly"].get(h_str, 0)
        h_pct = int(cnt / max_hour_count * 100) if max_hour_count > 0 else 0
        hourly_bars_html += f"""
        <div class="bar-col">
            <div class="bar-val">{cnt if cnt > 0 else ''}</div>
            <div class="bar-fill" style="height: {max(h_pct, 4)}%;"></div>
            <div class="bar-label">{h_str}</div>
        </div>
        """

    workers_html = ""
    for w in staff_metrics["workers"]:
        workers_html += f"""
        <tr>
            <td style="text-align:center;"><strong style="color:#2563eb; font-size:16px;">{w['worker_code']}</strong></td>
            <td><strong>{w['employee_name']}</strong></td>
            <td style="text-align:right; font-weight:700; color:#0f172a;">{w['total_sheets']} แผ่น</td>
        </tr>
        """
    if not workers_html:
        workers_html = "<tr><td colspan='3' style='text-align:center; color:#94a3b8; padding:20px;'>ยังไม่มีข้อมูลพนักงานส่งงาน</td></tr>"

    # User Approval & Period controls HTML
    pending_users = database.get_pending_users()
    pending_users_html = ""
    if pending_users:
        next_code = database.get_next_worker_code()
        cards_html = ""
        for pu in pending_users:
            cards_html += f"""
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; background:white; padding:10px 14px; border-radius:8px; border:1px solid #fed7aa; margin-top:8px;">
                <div>
                    <strong style="color:#0f172a;">👤 {pu['display_name']}</strong> <span style="color:#64748b; font-size:12px;">(ID: {pu['user_id'][:10]}...)</span>
                </div>
                <div style="display:flex; gap:6px; align-items:center;">
                    <form method="POST" action="/api/user/approve" style="margin:0; display:flex; gap:6px; align-items:center;">
                        <input type="hidden" name="user_id" value="{pu['user_id']}">
                        <label style="font-size:12px; font-weight:bold; color:#475569;">รหัส:</label>
                        <input type="text" name="worker_code" value="{next_code}" style="width:45px; text-align:center; padding:5px; font-weight:bold; border:1px solid #cbd5e1; border-radius:6px;">
                        <button type="submit" class="btn-toggle" style="background:#16a34a; color:white; padding:6px 12px; font-size:13px; cursor:pointer;">✅ อนุมัติ</button>
                    </form>
                    <form method="POST" action="/api/user/block" style="margin:0;">
                        <input type="hidden" name="user_id" value="{pu['user_id']}">
                        <button type="submit" class="btn-toggle" style="background:#dc2626; color:white; padding:6px 12px; font-size:13px; cursor:pointer;" onclick="return confirm('ยืนยันบล็อกผู้ใช้นี้?')">⛔️ บล็อก</button>
                    </form>
                </div>
            </div>
            """
        pending_users_html = f"""
        <div style="background:#fff7ed; border:1px solid #ffedd5; border-radius:12px; padding:14px; margin-bottom:16px;">
            <div style="font-weight:bold; color:#c2410c; font-size:15px;">🔔 มีผู้ขอเข้าใช้งานรอการอนุมัติ ({len(pending_users)} ท่าน)</div>
            {cards_html}
        </div>
        """

    if p_is_open:
        period_control_html = f"""
        <div class="period-banner banner-open">
            <div>
                <span class="status-indicator dot-open"></span>
                <strong>🟢 สถานะ: กำลังเปิดรับข้อมูล</strong> — {p_name}
            </div>
            <form method="POST" action="/api/period/toggle" style="margin:0;">
                <input type="hidden" name="action" value="close">
                <button type="submit" class="btn-toggle btn-close-period" onclick="return confirm('คุณต้องการปิดงวดนี้ใช่หรือไม่? (หลังจากปิด ระบบจะไม่รับรูปอีก)')">🔴 ปิดงวดนี้</button>
            </form>
        </div>
        """
    else:
        period_control_html = f"""
        <div class="period-banner banner-closed">
            <div>
                <span class="status-indicator dot-closed"></span>
                <strong>🔴 สถานะ: ปิดรับข้อมูลชั่วคราว</strong> — {p_name}
            </div>
            <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
                <form method="POST" action="/api/period/toggle" style="margin:0;">
                    <input type="hidden" name="action" value="reopen">
                    <button type="submit" class="btn-toggle btn-open-period">🟢 เปิดงวดเดิมต่อ (รับข้อมูลต่อ)</button>
                </form>
                <form method="POST" action="/api/period/toggle" style="margin:0;" onsubmit="let name = prompt('กรุณาตั้งชื่องวดใหม่ (เว้นว่างไว้เพื่อใช้วันที่วันนี้):'); if(name === null) return false; document.getElementById('new_p_name').value = name;">
                    <input type="hidden" name="action" value="new">
                    <input type="hidden" name="period_name" id="new_p_name" value="">
                    <button type="submit" class="btn-toggle" style="background:#2563eb; color:white;">➕ เริ่มเปิดงวดใหม่</button>
                </form>
            </div>
        </div>
        """

    # Period Dropdown Options
    period_options_html = ""
    for p in all_periods:
        sel = "selected" if p["id"] == p_id else ""
        st_tag = " (เปิด)" if p["status"] == "OPEN" else " (ปิดแล้ว)"
        period_options_html += f"<option value='{p['id']}' {sel}>{p['name']}{st_tag}</option>"

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ระบบจัดการตัวเลข & มอนิเตอร์ AI</title>
    <style>
        * {{ box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Prompt", "Segoe UI", Roboto, sans-serif; }}
        body {{ background: #f8fafc; color: #1e293b; margin: 0; padding: 16px; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 16px; }}
        .title {{ font-size: 22px; font-weight: 800; color: #0f172a; margin: 0; }}
        
        .tab-nav {{ display: flex; gap: 8px; border-bottom: 2px solid #e2e8f0; margin-bottom: 20px; overflow-x: auto; padding-bottom: 2px; }}
        .tab-btn {{ padding: 10px 18px; border: none; background: none; font-size: 15px; font-weight: 700; color: #64748b; cursor: pointer; border-radius: 8px 8px 0 0; white-space: nowrap; transition: all 0.15s; }}
        .tab-btn:hover {{ color: #2563eb; background: #eff6ff; }}
        .tab-btn.active {{ color: #2563eb; border-bottom: 3px solid #2563eb; background: #eff6ff; }}

        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}

        .period-banner {{ padding: 14px 18px; border-radius: 12px; margin-bottom: 16px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }}
        .banner-open {{ background: #dcfce7; border: 1px solid #bbf7d0; color: #166534; }}
        .banner-closed {{ background: #fee2e2; border: 1px solid #fecaca; color: #991b1b; }}
        .status-indicator {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 6px; }}
        .dot-open {{ background: #22c55e; }}
        .dot-closed {{ background: #ef4444; }}
        .btn-toggle {{ padding: 9px 16px; border-radius: 8px; font-weight: 700; font-size: 14px; border: none; cursor: pointer; }}
        .btn-close-period {{ background: #dc2626; color: white; }}
        .btn-open-period {{ background: #16a34a; color: white; }}

        .toolbar {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }}
        .period-select {{ padding: 10px 14px; border: 1px solid #cbd5e1; border-radius: 10px; font-size: 14px; font-weight: 600; background: white; }}

        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; margin-bottom: 20px; }}
        .stat-card {{ background: white; border: 1px solid #e2e8f0; border-radius: 12px; padding: 14px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
        .stat-num {{ font-size: 24px; font-weight: 800; color: #0f172a; margin-top: 4px; }}
        .stat-label {{ font-size: 12px; color: #64748b; font-weight: 600; text-transform: uppercase; }}

        .search-box {{ margin-bottom: 16px; display: flex; gap: 10px; }}
        .search-input {{ flex: 1; padding: 12px 16px; border: 1px solid #cbd5e1; border-radius: 10px; font-size: 15px; outline: none; background: white; }}
        .search-input:focus {{ border-color: #2563eb; box-shadow: 0 0 0 3px rgba(37,99,235,0.1); }}

        .btn-export {{ background: #059669; color: white; border: none; padding: 10px 16px; border-radius: 10px; font-weight: 600; text-decoration: none; font-size: 13px; display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }}
        .btn-dataset {{ background: #4f46e5; color: white; border: none; padding: 10px 16px; border-radius: 10px; font-weight: 600; text-decoration: none; font-size: 13px; display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }}

        .table-container {{ background: white; border: 1px solid #e2e8f0; border-radius: 12px; overflow-x: auto; box-shadow: 0 1px 3px rgba(0,0,0,0.05); margin-bottom: 20px; }}
        table {{ width: 100%; border-collapse: collapse; text-align: left; font-size: 14px; }}
        th {{ background: #f1f5f9; padding: 12px 16px; font-weight: 800; color: #1e293b; border-bottom: 2px solid #cbd5e1; }}
        td {{ padding: 10px 16px; border-bottom: 1px solid #f1f5f9; }}
        tr:hover {{ background: #f8fafc; }}

        .sheet-photos-bar {{ background: white; padding: 12px 16px; border-radius: 12px; border: 1px solid #e2e8f0; margin-bottom: 16px; }}
        .sheet-photos-list {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }}
        .sheet-photo-badge {{ background: #eff6ff; color: #1d4ed8; text-decoration: none; font-size: 12px; font-weight: 700; padding: 5px 10px; border-radius: 6px; border: 1px solid #bfdbfe; transition: all 0.15s; }}
        .sheet-photo-badge:hover {{ background: #2563eb; color: white; }}

        .scope-pills {{ display: inline-flex; background: #e2e8f0; padding: 4px; border-radius: 10px; gap: 4px; margin-bottom: 16px; }}
        .scope-pill {{ padding: 6px 14px; border-radius: 8px; font-size: 13px; font-weight: 700; text-decoration: none; color: #64748b; }}
        .scope-pill.active {{ background: white; color: #0f172a; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}

        .badge-pat {{ background: #f3e8ff; color: #7e22ce; font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 6px; }}
        .badge-edit {{ background: #fef3c7; color: #b45309; font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 6px; }}
        .badge-del {{ background: #fee2e2; color: #b91c1c; font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 6px; }}
        .badge-bulk {{ background: #e0e7ff; color: #3730a3; font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 6px; }}
        .badge-worker {{ background: #e2e8f0; color: #334155; font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 6px; }}

        .bars-container {{ display: flex; align-items: flex-end; gap: 8px; height: 160px; padding: 16px 8px 8px 8px; background: white; border-radius: 12px; border: 1px solid #e2e8f0; overflow-x: auto; margin-bottom: 20px; }}
        .bar-col {{ flex: 1; min-width: 28px; display: flex; flex-direction: column; align-items: center; height: 100%; justify-content: flex-end; }}
        .bar-fill {{ width: 100%; background: linear-gradient(180deg, #3b82f6 0%, #1d4ed8 100%); border-radius: 4px 4px 0 0; min-height: 4px; transition: height 0.3s; }}
        .bar-val {{ font-size: 10px; font-weight: 700; color: #64748b; margin-bottom: 4px; }}
        .bar-label {{ font-size: 11px; color: #94a3b8; margin-top: 6px; }}

        .footer-note {{ text-align: center; color: #94a3b8; font-size: 12px; margin-top: 20px; }}
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1 class="title">📊 กระดานจัดการตัวเลข & มอนิเตอร์ AI</h1>
            <div style="font-size: 13px; color:#64748b; margin-top:4px;">ระบบวิเคราะห์ตัวเลขชุดที่ 1 & ตรวจจับความแม่นยำ AI เรียลไทม์</div>
        </div>
        <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <a href="/prizes?period_id={p_id}" style="background:#dc2626; color:white; border:none; padding:10px 16px; border-radius:10px; font-weight:700; text-decoration:none; font-size:13px; display:inline-flex; align-items:center; gap:6px; box-shadow:0 2px 4px rgba(220,38,38,0.2);">🎰 ตรวจผลรางวัล</a>
            <a href="/backup-gdrive" style="background:#0284c7; color:white; border:none; padding:10px 16px; border-radius:10px; font-weight:700; text-decoration:none; font-size:13px; display:inline-flex; align-items:center; gap:6px;">☁️ สำรอง Google Drive</a>
            <a href="/export?period_id={p_id}" class="btn-export" download>📥 ดาวน์โหลด Excel (.CSV)</a>
            <a href="/export-ai-dataset?period_id={p_id}" class="btn-dataset" download>💾 ดาวน์โหลด AI Dataset (.JSON)</a>
        </div>
    </div>

    {pending_users_html}
    {period_control_html}

    <div class="tab-nav">
        <button class="tab-btn active" id="btn-tab-live" onclick="switchTab('live')">📋 กระดานสด (Live Board)</button>
        <button class="tab-btn" id="btn-tab-set1" onclick="switchTab('set1')">📈 สถิติชุดที่ 1 (Set 1 Insights)</button>
        <button class="tab-btn" id="btn-tab-ai" onclick="switchTab('ai')">🎯 ประสิทธิภาพ AI (AI Feedback)</button>
        <button class="tab-btn" id="btn-tab-staff" onclick="switchTab('staff')">👥 ทีมงาน & ชั่วโมงพีค</button>
    </div>

    <!-- ==================== TAB 1: LIVE BOARD ==================== -->
    <div id="tab-live" class="tab-content active">
        {sheet_photos_html}

        <div class="toolbar">
            <div>
                <label style="font-size:13px; font-weight:700; color:#475569;">📁 เลือกดูงวด:</label>
                <select class="period-select" onchange="window.location='/?period_id=' + this.value + '&tab=live'">
                    {period_options_html}
                </select>
            </div>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">เอกสารงวดนี้</div>
                <div class="stat-num" style="color:#2563eb">{summary['total_sheets']} แผ่น</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">รายการทั้งหมด</div>
                <div class="stat-num" style="color:#059669">{summary['total_entries']} รายการ</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">หมวด "บน"</div>
                <div class="stat-num" style="color:#2563eb">{len(top_items)}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">หมวด "ล่าง"</div>
                <div class="stat-num" style="color:#dc2626">{len(bot_items)}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">หมวด "บนล่าง"</div>
                <div class="stat-num" style="color:#7c3aed">{len(topbot_items)}</div>
            </div>
        </div>

        <div class="search-box" style="display:flex; gap:8px; align-items:center;">
            <input type="text" id="searchInput" class="search-input" placeholder="🔍 ค้นหาเลขในตารางสด หรือพิมพ์เลขแล้วกดเจาะลึก (เช่น 401, 370, 12)..." onkeyup="filterTable()" onkeypress="if(event.key==='Enter') executeDeepSearch()" style="flex:1;">
            <button type="button" onclick="executeDeepSearch()" style="background:#2563eb; color:white; border:none; padding:12px 18px; border-radius:10px; font-weight:700; font-size:14px; cursor:pointer; display:inline-flex; align-items:center; gap:6px; white-space:nowrap; box-shadow:0 2px 4px rgba(37,99,235,0.2);">
                🔎 เจาะลึกเลขนี้
            </button>
        </div>
        <div id="deepSearchResult" style="display:none; margin-bottom:16px; background:white; border:2px solid #3b82f6; border-radius:12px; padding:16px; box-shadow:0 4px 6px -1px rgba(0,0,0,0.1);"></div>

        <div class="table-container">
            <table id="dataTable">
                <thead>
                    <tr>
                        <th style="width: 80px; text-align: center;">ลำดับ</th>
                        <th style="color:#2563eb;">บน ({len(top_items)})</th>
                        <th style="color:#dc2626;">ล่าง ({len(bot_items)})</th>
                        <th style="color:#7c3aed;">บนล่าง ({len(topbot_items)})</th>
                    </tr>
                </thead>
                <tbody>
                    {table_rows_html}
                </tbody>
            </table>
        </div>
    </div>

    <!-- ==================== TAB 2: SET 1 INSIGHTS ==================== -->
    <div id="tab-set1" class="tab-content">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:14px;">
            <div class="scope-pills">
                <a href="/?period_id={p_id}&scope=period&tab=set1" class="scope-pill {'active' if not is_all_time else ''}">📍 เฉพาะงวดนี้ ({p_name})</a>
                <a href="/?period_id={p_id}&scope=all&tab=set1" class="scope-pill {'active' if is_all_time else ''}">🌐 ตลอดกาล (All-Time)</a>
            </div>
            <span style="font-size:13px; color:#64748b; font-weight:600;">ชุดตัวเลขที่นำมาวิเคราะห์: {tot_s1} รายการ</span>
        </div>

        <h3 style="margin:0 0 10px 0; font-size:16px; color:#0f172a;">📊 สัดส่วนจำนวนหลักของตัวเลขชุดที่ 1</h3>
        <div class="stats-grid" style="grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));">
            <div class="stat-card" style="border-left: 4px solid #2563eb;">
                <div class="stat-label">เลข 2 หลัก</div>
                <div class="stat-num" style="color:#2563eb">{cnt2:,} <span style="font-size:14px; color:#64748b;">({pct2}%)</span></div>
            </div>
            <div class="stat-card" style="border-left: 4px solid #059669;">
                <div class="stat-label">เลข 3 หลัก</div>
                <div class="stat-num" style="color:#059669">{cnt3:,} <span style="font-size:14px; color:#64748b;">({pct3}%)</span></div>
            </div>
            <div class="stat-card" style="border-left: 4px solid #7c3aed;">
                <div class="stat-label">เลข 4 หลัก</div>
                <div class="stat-num" style="color:#7c3aed">{cnt4:,} <span style="font-size:14px; color:#64748b;">({pct4}%)</span></div>
            </div>
        </div>

        <h3 style="margin:16px 0 10px 0; font-size:16px; color:#0f172a;">🎲 จำแนกตามรูปแบบตัวเลขพิเศษ</h3>
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">เลขเบิ้ล (11, 22)</div>
                <div class="stat-num" style="color:#d97706">{pat_counts.get('เลขเบิ้ล', 0):,}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">เลขหาม (121, 505)</div>
                <div class="stat-num" style="color:#0284c7">{pat_counts.get('เลขหาม', 0):,}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">เลขตอง (111, 777)</div>
                <div class="stat-num" style="color:#dc2626">{pat_counts.get('เลขตอง', 0):,}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">เลขเรียง (123, 789)</div>
                <div class="stat-num" style="color:#16a34a">{pat_counts.get('เลขเรียง', 0):,}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">เลขทั่วไป</div>
                <div class="stat-num" style="color:#64748b">{pat_counts.get('เลขทั่วไป', 0):,}</div>
            </div>
        </div>

        <h3 style="margin:16px 0 10px 0; font-size:16px; color:#0f172a;">🏆 Top 20 เลขยอดฮิตที่มีคนส่งเยอะที่สุด ({'ตลอดกาล' if is_all_time else p_name})</h3>
        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        <th style="width:60px; text-align:center;">อันดับ</th>
                        <th>เลขชุดที่ 1</th>
                        <th>รูปแบบ</th>
                        <th style="text-align:center;">จำนวนครั้งที่พบ</th>
                        <th style="text-align:center;">หมวด บน</th>
                        <th style="text-align:center;">หมวด ล่าง</th>
                        <th style="text-align:center;">หมวด บนล่าง</th>
                        <th style="text-align:right;">ยอดรวม Set 2</th>
                    </tr>
                </thead>
                <tbody>
                    {top20_html}
                </tbody>
            </table>
        </div>
    </div>

    <!-- ==================== TAB 3: AI FEEDBACK & ACCURACY ==================== -->
    <div id="tab-ai" class="tab-content">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:14px;">
            <div class="scope-pills">
                <a href="/?period_id={p_id}&scope=period&tab=ai" class="scope-pill {'active' if not is_all_time else ''}">📍 เฉพาะงวดนี้</a>
                <a href="/?period_id={p_id}&scope=all&tab=ai" class="scope-pill {'active' if is_all_time else ''}">🌐 ตลอดกาล (All-Time)</a>
            </div>
        </div>

        <div class="stats-grid" style="grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));">
            <div class="stat-card" style="border-left: 4px solid #16a34a;">
                <div class="stat-label">ความแม่นยำรอบแรก (Clean Rate)</div>
                <div class="stat-num" style="color:#16a34a;">{ai_metrics['clean_rate']}%</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">กระดาษที่ผ่าน 100% โดยไม่แก้</div>
                <div class="stat-num" style="color:#0f172a;">{ai_metrics['clean_scans']} / {ai_metrics['total_scans']} <span style="font-size:12px; color:#64748b;">แผ่น</span></div>
            </div>
            <div class="stat-card">
                <div class="stat-label">เวลาประมวลผล AI เฉลี่ย</div>
                <div class="stat-num" style="color:#2563eb;">{ai_metrics['avg_latency_s']} <span style="font-size:12px; color:#64748b;">วินาที</span></div>
            </div>
            <div class="stat-card">
                <div class="stat-label">รายการที่มนุษย์แก้ไข</div>
                <div class="stat-num" style="color:#d97706;">{len(ai_metrics['recent_corrections']):,} <span style="font-size:12px; color:#64748b;">ครั้ง</span></div>
            </div>
        </div>

        <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap:16px;">
            <div>
                <h3 style="margin:10px 0; font-size:16px; color:#0f172a;">🔴 คู่ตัวเลขที่ AI มักสับสน/อ่านผิดบ่อยที่สุด</h3>
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>คู่ตัวเลขที่อ่านผิด (AI ➔ ความจริง)</th>
                                <th style="text-align:center; width:120px;">จำนวนครั้ง</th>
                            </tr>
                        </thead>
                        <tbody>
                            {misread_html}
                        </tbody>
                    </table>
                </div>
            </div>

            <div>
                <h3 style="margin:10px 0; font-size:16px; color:#0f172a;">📝 ประวัติการแก้ไขล่าสุดของพนักงาน (Feedback Log)</h3>
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>เวลา</th>
                                <th>ใบที่</th>
                                <th>พนักงาน</th>
                                <th>การกระทำ</th>
                                <th>รายละเอียด</th>
                                <th>โน้ต</th>
                            </tr>
                        </thead>
                        <tbody>
                            {corr_html}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <!-- ==================== TAB 4: STAFF & PEAK HOURS ==================== -->
    <div id="tab-staff" class="tab-content">
        <h3 style="margin:0 0 10px 0; font-size:16px; color:#0f172a;">⏰ กราฟช่วงเวลาส่งงานหนาแน่นรายชั่วโมง (Peak Hours)</h3>
        <div class="bars-container">
            {hourly_bars_html}
        </div>

        <h3 style="margin:16px 0 10px 0; font-size:16px; color:#0f172a;">👥 สถิติการส่งงานแยกรายพนักงาน</h3>
        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        <th style="width:80px; text-align:center;">รหัส</th>
                        <th>ชื่อพนักงาน</th>
                        <th style="text-align:right;">จำนวนแผ่นที่บันทึกสำเร็จ</th>
                    </tr>
                </thead>
                <tbody>
                    {workers_html}
                </tbody>
            </table>
        </div>
    </div>

    <div class="footer-note">
        💡 ระบบเชื่อมต่อกับ LINE Official Account: พนักงานส่งรูปถ่ายได้เมื่อสถานะเป็น "กำลังเปิดรับข้อมูล"
    </div>

    <script>
        function switchTab(tabId) {{
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            
            const btn = document.getElementById('btn-tab-' + tabId);
            const content = document.getElementById('tab-' + tabId);
            if (btn && content) {{
                btn.classList.add('active');
                content.classList.add('active');
            }}
        }}

        // Read active tab from URL query
        const urlParams = new URLSearchParams(window.location.search);
        const requestedTab = urlParams.get('tab') || '{active_tab}';
        if (requestedTab && requestedTab !== 'live') {{
            switchTab(requestedTab);
        }}

        function filterTable() {{
            var input = document.getElementById('searchInput');
            var filter = input.value.toUpperCase();
            var rows = document.getElementsByClassName('entry-row');
            for (var i = 0; i < rows.length; i++) {{
                var rowText = rows[i].innerText || rows[i].textContent;
                if (rowText.toUpperCase().indexOf(filter) > -1) {{
                    rows[i].style.display = "";
                }} else {{
                    rows[i].style.display = "none";
                }}
            }}
        }}

        async function executeDeepSearch() {{
            var q = document.getElementById('searchInput').value.trim();
            var resBox = document.getElementById('deepSearchResult');
            if (!q) {{
                alert('กรุณาพิมพ์เลขที่ต้องการค้นหาก่อน เช่น 401 หรือ 12');
                return;
            }}
            resBox.style.display = 'block';
            resBox.innerHTML = '<div style="text-align:center; padding:12px; color:#64748b;">⏳ กำลังสแกนค้นหาเลข ' + q + ' ในทุกใบของงวดนี้...</div>';
            
            try {{
                const resp = await fetch('/api/search?q=' + encodeURIComponent(q) + '&period_id={p_id}');
                const data = await resp.json();
                
                if (!data.matches || data.matches.length === 0) {{
                    resBox.innerHTML = '<div style="display:flex; justify-content:space-between; align-items:center;">' +
                        '<span style="font-weight:700; color:#dc2626;">❌ ไม่พบเลข "' + data.query + '" ในงวดนี้</span>' +
                        '<button onclick="document.getElementById(\'deepSearchResult\').style.display=\'none\'" style="background:none; border:none; font-size:16px; cursor:pointer; color:#94a3b8;">✕ ปิด</button>' +
                    '</div>';
                    return;
                }}
                
                let matchesHtml = data.matches.map(function(m) {{
                    let imgHtml = m.image_path ? '<a href="' + m.image_path + '" target="_blank" style="background:#eff6ff; color:#2563eb; padding:4px 10px; border-radius:6px; font-size:12px; font-weight:700; text-decoration:none; border:1px solid #bfdbfe;">📷 ดูรูปหลักฐาน</a>' : '<span style="color:#94a3b8; font-size:12px;">ไม่มีรูป</span>';
                    let s3Html = m.set3 ? ' <small style="color:#d97706">(' + m.set3 + ')</small>' : '';
                    return '<tr style="border-bottom:1px solid #f1f5f9;">' +
                        '<td style="padding:8px 12px; font-weight:700; color:#0f172a;">ใบที่ ' + m.sheet_id + ' (' + (m.worker_code || m.employee_name) + ')</td>' +
                        '<td style="padding:8px 12px;"><span style="background:#e0e7ff; color:#3730a3; padding:2px 8px; border-radius:6px; font-size:12px; font-weight:700;">' + m.category + '</span></td>' +
                        '<td style="padding:8px 12px; font-size:16px; font-weight:800; color:#2563eb;">' + m.set1 + '</td>' +
                        '<td style="padding:8px 12px; font-weight:700; color:#059669;">' + m.set2 + s3Html + '</td>' +
                        '<td style="padding:8px 12px; text-align:right;">' + imgHtml + '</td>' +
                    '</tr>';
                }}).join('');

                resBox.innerHTML = '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">' +
                    '<div>' +
                        '<span style="font-size:18px; font-weight:800; color:#1e40af;">🔍 ผลการเจาะลึกเลข "' + data.query + '"</span>' +
                        '<span style="margin-left:8px; font-size:13px; color:#64748b;">(พบทั้งหมด <b>' + data.total_count + '</b> จุด | ยอดรวม <b>' + Number(data.total_volume).toLocaleString() + '</b> บาท)</span>' +
                    '</div>' +
                    '<button onclick="document.getElementById(\'deepSearchResult\').style.display=\'none\'" style="background:#f1f5f9; border:none; border-radius:6px; padding:4px 10px; font-size:13px; cursor:pointer; color:#475569; font-weight:700;">✕ ปิดหน้าต่างนี้</button>' +
                '</div>' +
                '<div style="display:flex; gap:8px; margin-bottom:12px; flex-wrap:wrap;">' +
                    '<span style="background:#eff6ff; color:#1d4ed8; padding:4px 10px; border-radius:6px; font-size:12px; font-weight:700;">บน: ' + (data.cat_breakdown['บน'] || 0) + '</span>' +
                    '<span style="background:#fef2f2; color:#b91c1c; padding:4px 10px; border-radius:6px; font-size:12px; font-weight:700;">ล่าง: ' + (data.cat_breakdown['ล่าง'] || 0) + '</span>' +
                    '<span style="background:#f5f3ff; color:#6d28d9; padding:4px 10px; border-radius:6px; font-size:12px; font-weight:700;">บนล่าง: ' + (data.cat_breakdown['บนล่าง'] || 0) + '</span>' +
                '</div>' +
                '<div style="max-height:280px; overflow-y:auto; border:1px solid #e2e8f0; border-radius:8px;">' +
                    '<table style="width:100%; border-collapse:collapse; font-size:13px;">' +
                        '<thead style="background:#f8fafc; position:sticky; top:0;">' +
                            '<tr>' +
                                '<th style="padding:8px 12px; text-align:left;">ใบที่ / ผู้ส่ง</th>' +
                                '<th style="padding:8px 12px; text-align:left;">หมวด</th>' +
                                '<th style="padding:8px 12px; text-align:left;">เลขชุด 1</th>' +
                                '<th style="padding:8px 12px; text-align:left;">ยอด (Set 2)</th>' +
                                '<th style="padding:8px 12px; text-align:right;">หลักฐาน</th>' +
                            '</tr>' +
                        '</thead>' +
                        '<tbody>' +
                            matchesHtml +
                        '</tbody>' +
                    '</table>' +
                '</div>';
            }} catch (err) {{
                resBox.innerHTML = '<div style="color:#dc2626; padding:10px;">❌ เกิดข้อผิดพลาดในการดึงข้อมูล: ' + err.message + '</div>';
            }}
        }}
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


        if path.startswith("/edit/"):
            m = re.match(r"^/edit/(\d+)$", path)
            if m:
                scan_id = int(m.group(1))
                html = render_edit_page(scan_id).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html)
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
            success = setup_rich_menu.setup_rich_menu()
            status_msg = "✅ ติดตั้ง LINE Rich Menu (3 ปุ่ม) สำเร็จเรียบร้อยแล้ว!" if success else "❌ การติดตั้งไม่สำเร็จ กรุณาตรวจเช็ค LOG ใน Render หรือ LINE_CHANNEL_ACCESS_TOKEN"
            res_html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'><title>Setup Rich Menu</title>
<style>body{{font-family:sans-serif;background:#f8fafc;padding:40px;text-align:center;}}
.box{{background:white;padding:30px;border-radius:16px;max-width:500px;margin:auto;box-shadow:0 4px 6px rgba(0,0,0,0.05);}}
a{{display:inline-block;margin-top:20px;padding:10px 20px;background:#2563eb;color:white;text-decoration:none;border-radius:8px;font-weight:700;}}
</style></head>
<body><div class='box'><h2>{status_msg}</h2><a href='/'>กลับสู่หน้าหลัก Dashboard</a></div></body></html>""".encode("utf-8")
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

        # 2. Quick Edit Save Form POST
        if path.startswith("/edit/"):
            m = re.match(r"^/edit/(\d+)$", path)
            if m:
                scan_id = int(m.group(1))
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length).decode("utf-8")
                form = urllib.parse.parse_qs(body)
                
                # Reconstruct columns structure
                new_columns = {"top": [], "bottom": [], "top_bottom": []}
                for col_key, col_label in [("top", "บน"), ("bottom", "ล่าง"), ("top_bottom", "บนล่าง")]:
                    idx = 0
                    while True:
                        s1_key = f"{col_key}_set1_{idx}"
                        if s1_key not in form:
                            break
                        s1 = form.get(s1_key, [""])[0].strip()
                        s3 = form.get(f"{col_key}_set3_{idx}", [""])[0].strip()
                        s2 = form.get(f"{col_key}_set2_{idx}", [""])[0].strip()
                        if s1 or s2:
                            new_columns[col_key].append({
                                "set1": s1,
                                "set3": s3,
                                "set2": s2,
                                "raw_text": f"{s1} = {s3} {s2}".strip()
                            })
                        idx += 1
                        
                database.update_pending_scan_items(scan_id, new_columns)
                confirmed = database.confirm_pending_scan(scan_id)
                if confirmed:
                    import gdrive_sync
                    gdrive_sync.trigger_db_backup()
                
                success_html = """<!DOCTYPE html><html lang="th"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>บันทึกสำเร็จ</title><style>body{background:#f8fafc;font-family:sans-serif;padding:30px;text-align:center;}.card{background:white;padding:30px;border-radius:16px;max-width:400px;margin:auto;box-shadow:0 2px 5px rgba(0,0,0,0.1);}</style></head><body><div class="card"><h1 style="color:#16a34a;margin:0;">✅ บันทึกสำเร็จ!</h1><p style="color:#64748b;margin:15px 0;">ข้อมูลของคุณได้รับการแก้ไขและยืนยันเข้าระบบเรียบร้อยแล้ว</p><a href="https://line.me" style="display:inline-block;background:#06c755;color:white;padding:12px 24px;border-radius:10px;text-decoration:none;font-weight:700;">กลับไปที่ LINE</a></div></body></html>""".encode("utf-8")
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
                        # Process OCR asynchronously in background thread so webhook responds immediately
                        threading.Thread(
                            target=handle_image_message,
                            args=(msg.get("id"), reply_token, user_id, user or {}),
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
