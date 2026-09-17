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
    
    top_items = columns.get("top", [])
    if top_items:
        lines.append("[บน]")
        for itm in top_items:
            s1 = itm.get("set1", "").strip()
            s3 = itm.get("set3", "").strip()
            s3_part = f"{s3} " if s3 else ""
            s2 = itm.get("set2", "").strip()
            lines.append(f"{s1} = {s3_part}{s2}".strip())
        lines.append("")
        
    bot_items = columns.get("bottom", [])
    if bot_items:
        lines.append("[ล่าง]")
        for itm in bot_items:
            s1 = itm.get("set1", "").strip()
            s3 = itm.get("set3", "").strip()
            s3_part = f"{s3} " if s3 else ""
            s2 = itm.get("set2", "").strip()
            lines.append(f"{s1} = {s3_part}{s2}".strip())
        lines.append("")
        
    topbot_items = columns.get("top_bottom", [])
    if topbot_items:
        lines.append("[บนล่าง]")
        for itm in topbot_items:
            s1 = itm.get("set1", "").strip()
            s3 = itm.get("set3", "").strip()
            s3_part = f"{s3} " if s3 else ""
            s2 = itm.get("set2", "").strip()
            lines.append(f"{s1} = {s3_part}{s2}".strip())
        lines.append("")
        
    return "\n".join(lines).strip()

def parse_entry_line(line: str) -> Optional[dict]:
    line = line.strip()
    if not line:
        return None
    parts = line.split("=", 1)
    if len(parts) == 2:
        s1 = parts[0].strip()
        right = parts[1].strip()
    else:
        m = re.match(r"^(\d+)\s+(.+)$", line)
        if m:
            s1 = m.group(1).strip()
            right = m.group(2).strip()
        else:
            if re.match(r"^\d{2,4}$", line):
                return {"set1": line, "set2": "", "set3": "", "raw_text": line}
            return None
            
    s3 = ""
    s2 = right
    m_s3 = re.search(r"(ก3|ก6)", right)
    if m_s3:
        s3 = m_s3.group(1)
        s2 = right.replace(s3, "").strip()
        
    s2 = s2.replace("X", "x").replace(" ", "")
    s3_part = f"{s3} " if s3 else ""
    raw = f"{s1} = {s3_part}{s2}".strip()
    return {"set1": s1, "set2": s2, "set3": s3, "raw_text": raw}

def parse_sheet_text(text: str) -> Optional[dict]:
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    if not lines:
        return None
        
    sheet_id = None
    current_col = "top"
    columns = {"top": [], "bottom": [], "top_bottom": []}
    has_entries = False
    
    for line in lines:
        m_sheet = re.search(r"ใบที่(?:\s*[:\-]?\s*)([A-Za-z0-9\-_]+)", line)
        if m_sheet:
            found_id = m_sheet.group(1).strip()
            if not sheet_id or (found_id and "-" in found_id):
                sheet_id = found_id
            continue
            
        line_clean = line.replace("[", "").replace("]", "").replace(":", "").strip()
        if line_clean in ["บนล่าง", "หมวดบนล่าง", "หมวด บนล่าง"]:
            current_col = "top_bottom"
            continue
        elif line_clean in ["บน", "หมวดบน", "หมวด บน"]:
            current_col = "top"
            continue
        elif line_clean in ["ล่าง", "หมวดล่าง", "หมวด ล่าง"]:
            current_col = "bottom"
            continue
            
        entry = parse_entry_line(line)
        if entry:
            columns[current_col].append(entry)
            has_entries = True
            
    if not has_entries:
        return None
        
    return {"sheet_id": sheet_id, "columns": columns}

def handle_delete_command(text: str, user_id: str, reply_token: str) -> bool:
    m = re.match(r"^ลบ\s+(\d+)(?:[=\s].*)?$", text.strip())
    if not m:
        return False
    target_num = m.group(1).strip()
    
    scan = database.get_latest_pending_scan(user_id)
    if not scan:
        reply_line_message(reply_token, "⚠️ ไม่พบรายการที่รอยืนยันอยู่ในขณะนี้ครับ")
        return True
        
    val_data = json.loads(scan["val_json"])
    cols = val_data.get("validated_columns", {})
    
    found = False
    new_cols = {"top": [], "bottom": [], "top_bottom": []}
    for c in ["top", "bottom", "top_bottom"]:
        for itm in cols.get(c, []):
            if itm.get("set1") == target_num and not found:
                found = True
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
    m = re.match(r"^แก้\s+(\d+(?:[=\s][^\s]+)?)\s+เป็น\s+(.+)$", text.strip())
    if not m:
        return False
    old_target = m.group(1).strip()
    new_target = m.group(2).strip()
    
    scan = database.get_latest_pending_scan(user_id)
    if not scan:
        reply_line_message(reply_token, "⚠️ ไม่พบรายการที่รอยืนยันอยู่ในขณะนี้ครับ")
        return True
        
    val_data = json.loads(scan["val_json"])
    cols = val_data.get("validated_columns", {})
    
    old_s1 = old_target.split("=")[0].strip() if "=" in old_target else old_target.split()[0].strip()
    parsed_new = parse_entry_line(new_target)
    if not parsed_new:
        reply_line_message(reply_token, f"❌ รูปแบบเลขใหม่ '{new_target}' ไม่ถูกต้อง (เช่น 375 หรือ 375=36x36)")
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
                new_cols[c].append(updated_itm)
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
        ocr_result = ocr_engine.extract_from_image(img_bytes)
        
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
            user_id, worker_code, emp_name, active_p["id"], formatted_sheet_id, ocr_result, val_result
        )

        valid_cols = val_result.get("validated_columns", {})
        top_items = valid_cols.get("top", [])
        bot_items = valid_cols.get("bottom", [])
        topbot_items = valid_cols.get("top_bottom", [])
        total_items = len(top_items) + len(bot_items) + len(topbot_items)
        
        # Combine summary header, clean editable numbers, and instructions into 1 single message bubble
        clean_text = format_clean_editable_text(raw_sheet_id, valid_cols)
        
        combined_text = (
            f"📌 ข้อมูลนี้จะถูกบันทึกลงใน: {active_p['name']}\n"
            f"📋 อ่านข้อมูลได้ [ใบที่: {formatted_sheet_id}]\n"
            f"📊 รวมทั้งหมด: {total_items} รายการ | ผู้ส่ง: {emp_name} ({worker_code})\n"
            f"─────────────────────────\n"
            f"{clean_text}\n"
            f"─────────────────────────\n"
            f"✅ หากถูกต้อง: กดปุ่ม [ ยืนยัน ] ด้านล่างได้เลยครับ\n"
            f"✏️ หากต้องการแก้ไข: คัดลอกข้อความนี้ไปแก้/ลบตัวเลข แล้วส่งกลับมาได้ทันทีครับ"
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
    if clean_text in ["เมนู", "menu", "help", "?"]:
        help_msg = (
            "📋 เมนูการใช้งานระบบ\n"
            "─────────────────────────\n"
            "1️⃣ ถ่ายรูปกระดาษส่งเข้ามา ➔ ระบบอ่านและส่งสรุปให้ตรวจ\n"
            "2️⃣ ก๊อปปี้ข้อความตัวเลขไปแก้ไข/ลบ แล้วส่งกลับมา ➔ ระบบบันทึกทันที\n"
            "3️⃣ หรือพิมพ์ 'ลบ [เลข]' / 'แก้ [เลขเดิม] เป็น [เลขใหม่]'\n"
            "4️⃣ พิมพ์ 'เช็ค [เลข]' ➔ ค้นหาเลขชุดที่ 1 ในงวดนี้\n"
            "5️⃣ พิมพ์ 'รีเช็ค' ➔ ตรวจสอบลำดับแผ่นและแผ่นที่ตกหล่น\n"
            "6️⃣ พิมพ์ 'สถานะ' ➔ ดูยอดรวมและสถานะงวด\n"
        )
        if is_owner:
            help_msg += (
                "─────────────────────────\n"
                "👑 คำสั่งเจ้าของระบบ:\n"
                "• 'ปิดงวด' / 'เปิดงวด' / 'เปิดงวดใหม่' เพื่อเปิด-ปิดงวด\n"
                "• 'อนุมัติ [ชื่อ] [รหัส A/B/C]' เพื่อเปิดสิทธิ์ให้พนักงาน\n"
                "• 'บล็อก [ชื่อ]' เพื่อตัดสิทธิ์\n"
            )
        help_msg += f"─────────────────────────\n🌐 ตารางสรุปสดและเปิดปิดงวด:\n{BASE_URL}"
        reply_line_message(reply_token, help_msg)
        return

    # 9. Check if Worker Pasted an Edited Sheet Text Block
    parsed_sheet = parse_sheet_text(clean_text)
    if parsed_sheet and any(len(items) > 0 for items in parsed_sheet["columns"].values()):
        # Validate edited entries
        val_result = OCRValidator.validate_document({"columns": parsed_sheet["columns"]})
        if not val_result["is_all_valid"]:
            err_lines = ["⚠️ ตัวเลขที่แก้ไขมีจุดที่ไม่ตรงตามกฎ:"]
            for err in val_result["errors"][:5]:
                err_lines.append(f"• {err}")
            err_lines.append("\n💡 กรุณาแก้ไขตัวเลขให้ถูกต้องแล้วส่งใหม่อีกครั้งครับ")
            reply_line_message(reply_token, "\n".join(err_lines))
            return
            
        # Valid edited data!
        active_p = database.get_active_period()
        if not active_p:
            reply_line_message(reply_token, f"⛔️ ขณะนี้ระบบปิดรับข้อมูล (ยังไม่เปิดงวดใหม่)\n🌐 {BASE_URL}")
            return
            
        worker_code = user_info.get("worker_code", "A")
        emp_name = user_info.get("display_name", "พนักงาน")
        
        # Check if there is an existing pending scan
        pending_scan = database.get_latest_pending_scan(user_id)
        if pending_scan:
            sheet_id = pending_scan["sheet_id"]
            database.update_pending_scan_items(pending_scan["id"], parsed_sheet["columns"])
            database.confirm_pending_scan(pending_scan["id"])
        else:
            raw_s = parsed_sheet.get("sheet_id") or "1"
            sheet_id = f"{worker_code}-{raw_s}" if not raw_s.startswith(f"{worker_code}-") else raw_s
            ocr_obj = {"header": {"sheet_id": sheet_id, "customer_name": emp_name}, "columns": parsed_sheet["columns"]}
            new_id = database.create_pending_scan(user_id, worker_code, emp_name, active_p["id"], sheet_id, ocr_obj, val_result)
            database.confirm_pending_scan(new_id)
            
        top_c = len(parsed_sheet["columns"].get("top", []))
        bot_c = len(parsed_sheet["columns"].get("bottom", []))
        topbot_c = len(parsed_sheet["columns"].get("top_bottom", []))
        total_c = top_c + bot_c + topbot_c
        
        reply_msg = (
            f"✅ บันทึกข้อมูลที่แก้ไขเรียบร้อยแล้วครับ!\n"
            f"📋 ใบที่: {sheet_id}\n"
            f"📌 ข้อมูลถูกบันทึกลงใน: {active_p['name']}\n"
            f"📊 บันทึกทั้งหมด: {total_c} รายการ (บน {top_c} | ล่าง {bot_c} | บนล่าง {topbot_c})\n\n"
            f"🌐 ดูตารางสด: {BASE_URL}"
        )
        reply_line_message(reply_token, reply_msg)
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

def render_html_dashboard(period_id: Optional[int] = None) -> str:
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
    conn.close()

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

    # Pending Users Approval Section
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

    # Period Toggle Button
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
    <title>กระดานสรุปตัวเลข Real-Time</title>
    <meta http-equiv="refresh" content="15">
    <style>
        * {{ box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Prompt", "Segoe UI", Roboto, sans-serif; }}
        body {{ background: #f8fafc; color: #1e293b; margin: 0; padding: 16px; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 16px; }}
        .title {{ font-size: 22px; font-weight: 700; color: #0f172a; margin: 0; }}
        
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

        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 12px; margin-bottom: 20px; }}
        .stat-card {{ background: white; border: 1px solid #e2e8f0; border-radius: 12px; padding: 14px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
        .stat-num {{ font-size: 24px; font-weight: 800; color: #0f172a; margin-top: 4px; }}
        .stat-label {{ font-size: 12px; color: #64748b; font-weight: 600; text-transform: uppercase; }}

        .search-box {{ margin-bottom: 16px; display: flex; gap: 10px; }}
        .search-input {{ flex: 1; padding: 12px 16px; border: 1px solid #cbd5e1; border-radius: 10px; font-size: 15px; outline: none; background: white; }}
        .search-input:focus {{ border-color: #2563eb; box-shadow: 0 0 0 3px rgba(37,99,235,0.1); }}
        .btn-export {{ background: #059669; color: white; border: none; padding: 12px 18px; border-radius: 10px; font-weight: 600; text-decoration: none; font-size: 14px; display: inline-flex; align-items: center; gap: 6px; cursor: pointer; white-space: nowrap; }}

        .table-container {{ background: white; border: 1px solid #e2e8f0; border-radius: 12px; overflow-x: auto; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
        table {{ width: 100%; border-collapse: collapse; text-align: left; font-size: 15px; }}
        th {{ background: #f1f5f9; padding: 14px 18px; font-weight: 800; color: #1e293b; border-bottom: 2px solid #cbd5e1; }}
        th.th-top {{ color: #2563eb; font-size: 16px; }}
        th.th-bot {{ color: #dc2626; font-size: 16px; }}
        th.th-topbot {{ color: #7c3aed; font-size: 16px; }}
        td {{ padding: 12px 18px; border-bottom: 1px solid #f1f5f9; }}
        tr:hover {{ background: #f8fafc; }}
        .footer-note {{ text-align: center; color: #94a3b8; font-size: 12px; margin-top: 20px; }}
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1 class="title">📋 กระดานสรุปตัวเลข Real-Time</h1>
            <div style="font-size: 13px; color:#64748b; margin-top:4px;">ระบบบันทึกลายมืออัตโนมัติ 24 ชม.</div>
        </div>
        <div style="display:flex; gap:8px;">
            <a href="/export?period_id={p_id}" class="btn-export" download>📥 ดาวน์โหลด Excel (.CSV)</a>
        </div>
    </div>

    {pending_users_html}
    {period_control_html}

    <div class="toolbar">
        <div>
            <label style="font-size:13px; font-weight:700; color:#475569;">📁 เลือกดูงวด:</label>
            <select class="period-select" onchange="window.location='/?period_id=' + this.value">
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

    <div class="search-box">
        <input type="text" id="searchInput" class="search-input" placeholder="🔍 พิมพ์ค้นหาตัวเลขในงวดนี้ เช่น 401, 370, 12..." onkeyup="filterTable()">
    </div>

    <div class="table-container">
        <table id="dataTable">
            <thead>
                <tr>
                    <th style="width: 80px; text-align: center;">ลำดับ</th>
                    <th class="th-top">บน ({len(top_items)})</th>
                    <th class="th-bot">ล่าง ({len(bot_items)})</th>
                    <th class="th-topbot">บนล่าง ({len(topbot_items)})</th>
                </tr>
            </thead>
            <tbody>
                {table_rows_html}
            </tbody>
        </table>
    </div>

    <div class="footer-note">
        💡 ระบบเชื่อมต่อกับ LINE Official Account: พนักงานส่งรูปถ่ายได้เมื่อสถานะเป็น "กำลังเปิดรับข้อมูล"
    </div>

    <script>
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
                "version": "v2.1-quick-approve",
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

        # Default: Dashboard
        p_id = int(qs.get("period_id", [0])[0]) or None
        html_content = render_html_dashboard(p_id).encode('utf-8')
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
                        display_name = get_line_profile(user_id)
                        database.register_pending_user(user_id, display_name)
                        next_code = database.get_next_worker_code()
                        
                        admin_msg = (
                            f"🔔 [มีผู้ขอเข้าใช้งานระบบใหม่!]\n"
                            f"👤 ชื่อ LINE: {display_name}\n"
                            f"🆔 ID: {user_id}\n\n"
                            f"💡 แตะปุ่มด้านล่างเพื่ออนุมัติได้ทันทีครับ:"
                        )
                        quick_replies = [
                            (f"✅ อนุมัติ ({next_code})", f"อนุมัติ id:{user_id} {next_code}"),
                            ("✅ อนุมัติ (A)", f"อนุมัติ id:{user_id} A"),
                            ("✅ อนุมัติ (B)", f"อนุมัติ id:{user_id} B"),
                            ("✅ อนุมัติ (C)", f"อนุมัติ id:{user_id} C"),
                            ("⛔️ บล็อก", f"บล็อก id:{user_id}")
                        ]
                        seen = set()
                        uniq_replies = []
                        for lbl, val in quick_replies:
                            if lbl not in seen:
                                seen.add(lbl)
                                uniq_replies.append((lbl, val))
                                
                        deliver_message(OWNER_USER_ID, None, admin_msg, uniq_replies)
                        reply_line_message(reply_token, "🔒 ขออภัยครับ บัญชีนี้เป็นระบบเฉพาะภายใน\nระบบได้ส่งคำขอไปยังเจ้าของระบบแล้ว กรุณารอการอนุมัติสักครู่ครับ")
                        continue
                    elif user.get("status") == "PENDING":
                        reply_line_message(reply_token, "🔒 บัญชีของคุณอยู่ระหว่างรอเจ้าของระบบอนุมัติครับ กรุณารอสักครู่ครับ")
                        continue
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
    server_cls = getattr(http.server, "ThreadingHTTPServer", http.server.HTTPServer)
    server = server_cls(("0.0.0.0", PORT), LineWebhookHandler)
    print(f"🚀 LINE Bot Server running on port {PORT} (Threaded)...")
    server.serve_forever()

if __name__ == "__main__":
    run_server()
