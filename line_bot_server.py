import http.server
import json
import hmac
import hashlib
import base64
import urllib.request
import urllib.error
import re
import os
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

def reply_line_message(reply_token: str, text: str):
    url = "https://api.line.me/v2/bot/message/reply"
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}]
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
        print(f"Error sending LINE reply: {e}")

def push_line_message(to_user_id: str, text: str):
    url = "https://api.line.me/v2/bot/message/push"
    payload = {
        "to": to_user_id,
        "messages": [{"type": "text", "text": text}]
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
        print(f"Error sending LINE push: {e}")

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
    with urllib.request.urlopen(req) as resp:
        return resp.read()

def handle_image_message(message_id: str, reply_token: str, user_id: str, user_info: dict):
    try:
        worker_code = user_info.get("worker_code", "A")
        emp_name = user_info.get("display_name", "พนักงาน")

        print(f"Downloading image from {emp_name} ({worker_code})...")
        img_bytes = get_line_image_content(message_id)
        
        print("Processing OCR with Gemini 3.6 Flash...")
        ocr_result = ocr_engine.extract_from_image(img_bytes)
        
        print("Validating rules...")
        val_result = OCRValidator.validate_document(ocr_result)
        
        # Prepend worker code to sheet_id if not already there
        raw_sheet_id = ocr_result.get("header", {}).get("sheet_id", "").strip()
        if raw_sheet_id:
            formatted_sheet_id = f"{worker_code}-{raw_sheet_id}" if not raw_sheet_id.startswith(f"{worker_code}-") else raw_sheet_id
        else:
            formatted_sheet_id = f"{worker_code}-N/A"
        ocr_result["header"]["sheet_id"] = formatted_sheet_id
        ocr_result["header"]["customer_name"] = emp_name

        # Save to DB
        sheet_id = database.save_document(ocr_result, val_result)
        
        cols = val_result.get("validated_columns", {})
        top_items = cols.get("top", [])
        bot_items = cols.get("bottom", [])
        topbot_items = cols.get("top_bottom", [])
        total_items = len(top_items) + len(bot_items) + len(topbot_items)
        
        lines = []
        if val_result.get("is_all_valid"):
            lines.append(f"✅ บันทึกเรียบร้อย [ใบที่: {formatted_sheet_id}]")
        else:
            lines.append(f"⚠️ บันทึกแล้ว แต่พบจุดผิดสังเกต [ใบที่: {formatted_sheet_id}]:")
            for err in val_result.get("errors", [])[:4]:
                lines.append(f"  • {err}")
                
        lines.append("─────────────────────────")
        
        def format_item_list(items, title):
            res = []
            if items:
                res.append(f"📌 หมวด [{title}] ({len(items)} รายการ):")
                for idx, itm in enumerate(items, 1):
                    s1 = itm.get('set1', '')
                    s3 = f"{itm.get('set3')} " if itm.get('set3') else ""
                    s2 = itm.get('set2', '')
                    err_icon = " ❌" if not itm.get('is_valid') else ""
                    res.append(f" {idx}. {s1} = {s3}{s2}{err_icon}".strip())
                res.append("")
            return res

        if top_items:
            lines.extend(format_item_list(top_items, "บน"))
        if bot_items:
            lines.extend(format_item_list(bot_items, "ล่าง"))
        if topbot_items:
            lines.extend(format_item_list(topbot_items, "บนล่าง"))
            
        lines.append("─────────────────────────")
        lines.append(f"📊 รวมทั้งหมด: {total_items} รายการ")
        lines.append(f"👤 ผู้ส่ง: {emp_name} (รหัส: {worker_code})")
        lines.append("")
        lines.append(f"🌐 ดูกระดานสรุปสด: {BASE_URL}")
        
        reply_line_message(reply_token, "\n".join(lines).strip())
        
    except Exception as e:
        print(f"Error handling image: {e}")
        reply_line_message(reply_token, f"❌ ขออภัย ระบบอ่านภาพขัดข้อง: {e}\nกรุณาลองถ่ายภาพส่งใหม่อีกครั้งครับ")

def handle_owner_command(text: str, reply_token: str) -> bool:
    clean = text.strip()
    # Check for approval commands: "อนุมัติ [ชื่อ/ID] [รหัส A/B/C]"
    m = re.match(r"^อนุมัติ\s+(\S+)\s+([A-Za-z0-9]+)$", clean)
    if m:
        target = m.group(1)
        code = m.group(2).upper()
        updated = database.approve_user(target, code)
        if updated:
            reply_line_message(reply_token, f"✅ อนุมัติเรียบร้อย!\n👤 {updated['display_name']}\n🏷️ ได้รับรหัสพนักงาน: [{code}]")
            # Notify the worker
            push_line_message(updated["user_id"], f"🎉 คุณได้รับการอนุมัติให้ใช้งานระบบแล้วครับ!\n🏷️ รหัสประจำตัวของคุณคือ: [{code}]\n💡 คุณสามารถเริ่มส่งรูปกระดาษบันทึกได้เลยครับ")
        else:
            reply_line_message(reply_token, f"❌ ไม่พบผู้ใช้งานที่ระบุ '{target}' ในระบบ")
        return True
        
    # Check for block command: "บล็อก [ชื่อ/ID]"
    m_block = re.match(r"^บล็อก\s+(\S+)$", clean)
    if m_block:
        target = m_block.group(1)
        blocked = database.block_user(target)
        if blocked:
            reply_line_message(reply_token, f"⛔️ ระงับสิทธิ์การใช้งานของ {blocked['display_name']} เรียบร้อยแล้ว")
        else:
            reply_line_message(reply_token, f"❌ ไม่พบผู้ใช้งานที่ระบุ '{target}'")
        return True

    return False

def handle_text_message(text: str, reply_token: str, user_id: str, is_owner: bool):
    clean_text = text.strip()
    
    # 0. Owner Approval check
    if is_owner and handle_owner_command(clean_text, reply_token):
        return

    # 1. Search Query command: "เช็ค 310"
    if clean_text.startswith("เช็ค") or clean_text.lower().startswith("check"):
        numbers = re.findall(r"\d+", clean_text)
        if not numbers:
            reply_line_message(reply_token, "⚠️ กรุณาระบุตัวเลขที่ต้องการค้นหา เช่น 'เช็ค 310' หรือ 'เช็ค 401 377'")
            return
        result_msg = query_service.format_search_results(numbers)
        reply_line_message(reply_token, result_msg)
        return
        
    # 2. Status / Summary command
    if clean_text in ["สถานะ", "status", "ยอด", "สรุป", "ตาราง", "dashboard"]:
        status_msg = query_service.format_daily_status()
        status_msg += f"\n\n🌐 ดูกระดานสรุป Real-Time:\n{BASE_URL}"
        reply_line_message(reply_token, status_msg)
        return
        
    # 3. Help menu
    if clean_text in ["เมนู", "menu", "help", "?"]:
        help_msg = (
            "📋 เมนูการใช้งานระบบ\n"
            "─────────────────────────\n"
            "1️⃣ ส่งรูปกระดาษ ➔ ระบบอ่านและบันทึกข้อมูล\n"
            "2️⃣ พิมพ์ 'เช็ค [เลข]' ➔ ค้นหาเลขชุดที่ 1\n"
            "3️⃣ พิมพ์ 'สรุป' ➔ ดูยอดรวมและลิงก์ตารางสรุปสด\n"
        )
        if is_owner:
            help_msg += (
                "─────────────────────────\n"
                "👑 คำสั่งเจ้าของระบบ:\n"
                "• 'อนุมัติ [ชื่อ] [รหัส A/B/C]' เพื่อเปิดสิทธิ์ให้พนักงาน\n"
                "• 'บล็อก [ชื่อ]' เพื่อตัดสิทธิ์\n"
            )
        help_msg += f"─────────────────────────\n🌐 ตารางสรุปสด:\n{BASE_URL}"
        reply_line_message(reply_token, help_msg)
        return
        
    reply_line_message(
        reply_token,
        f"💡 คุณสามารถถ่ายรูปกระดาษส่งเข้ามาได้เลยครับ\nหรือพิมพ์ 'เช็ค [ตัวเลข]' เพื่อค้นหา\nหรือพิมพ์ 'สรุป' เพื่อดูกระดานข้อมูลสด\n🌐 {BASE_URL}"
    )

def render_html_dashboard() -> str:
    summary = database.get_daily_summary()
    conn = database.get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT category, set1, set3, set2, is_valid
    FROM entries
    ORDER BY sheet_db_id ASC, id ASC
    """)
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
        table_rows_html = "<tr><td colspan='4' style='text-align:center; padding:40px; color:#94a3b8;'>ยังไม่มีข้อมูล ส่งรูปถ่ายผ่าน LINE เข้ามาได้เลยครับ</td></tr>"

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ระบบกระดานสรุปข้อมูล Real-Time</title>
    <meta http-equiv="refresh" content="15">
    <style>
        * {{ box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Prompt", "Segoe UI", Roboto, sans-serif; }}
        body {{ background: #f8fafc; color: #1e293b; margin: 0; padding: 16px; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }}
        .title {{ font-size: 22px; font-weight: 700; color: #0f172a; margin: 0; }}
        .live-tag {{ background: #dcfce7; color: #15803d; padding: 4px 10px; border-radius: 999px; font-size: 13px; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; }}
        .live-dot {{ width: 8px; height: 8px; background: #22c55e; border-radius: 50%; display: inline-block; animation: pulse 1.5s infinite; }}
        @keyframes pulse {{ 0% {{ opacity: 1; }} 50% {{ opacity: 0.3; }} 100% {{ opacity: 1; }} }}
        
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; margin-bottom: 20px; }}
        .stat-card {{ background: white; border: 1px solid #e2e8f0; border-radius: 12px; padding: 14px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
        .stat-num {{ font-size: 24px; font-weight: 800; color: #0f172a; margin-top: 4px; }}
        .stat-label {{ font-size: 12px; color: #64748b; font-weight: 600; text-transform: uppercase; }}

        .search-box {{ margin-bottom: 16px; display: flex; gap: 10px; }}
        .search-input {{ flex: 1; padding: 12px 16px; border: 1px solid #cbd5e1; border-radius: 10px; font-size: 15px; outline: none; background: white; }}
        .search-input:focus {{ border-color: #2563eb; box-shadow: 0 0 0 3px rgba(37,99,235,0.1); }}
        .btn-export {{ background: #059669; color: white; border: none; padding: 12px 18px; border-radius: 10px; font-weight: 600; text-decoration: none; font-size: 14px; display: inline-flex; align-items: center; gap: 6px; cursor: pointer; white-space: nowrap; }}
        .btn-export:hover {{ background: #047857; }}

        .table-container {{ background: white; border: 1px solid #e2e8f0; border-radius: 12px; overflow-x: auto; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
        table {{ width: 100%; border-collapse: collapse; text-align: left; font-size: 15px; }}
        th {{ background: #f1f5f9; padding: 14px 18px; font-weight: 800; color: #1e293b; border-bottom: 2px solid #cbd5e1; }}
        th.th-top {{ color: #2563eb; font-size: 16px; }}
        th.th-bot {{ color: #dc2626; font-size: 16px; }}
        th.th-topbot {{ color: #7c3aed; font-size: 16px; }}
        td {{ padding: 12px 18px; border-bottom: 1px solid #f1f5f9; }}
        tr:hover {{ background: #f8fafc; }}
        .col-val {{ font-size: 15px; }}

        .footer-note {{ text-align: center; color: #94a3b8; font-size: 12px; margin-top: 20px; }}
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1 class="title">📋 กระดานสรุปตัวเลข Real-Time (รูปแบบ 3 คอลัมน์)</h1>
            <span class="live-tag"><span class="live-dot"></span> อัปเดตสดอัตโนมัติ (ทุก 15 วินาที)</span>
        </div>
        <a href="/export" class="btn-export" download>📥 ดาวน์โหลดไฟล์ Excel (.CSV)</a>
    </div>

    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-label">เอกสารทั้งหมด</div>
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
        <input type="text" id="searchInput" class="search-input" placeholder="🔍 พิมพ์ค้นหาตัวเลข เช่น 401, 370, 12..." onkeyup="filterTable()">
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
        💡 ระบบเชื่อมต่อกับ LINE Official Account: ส่งรูปถ่ายเพื่อเพิ่มข้อมูลได้ตลอด 24 ชั่วโมง
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
        if self.path == "/export":
            csv_path = database.export_csv()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8-sig")
            self.send_header("Content-Disposition", 'attachment; filename="report_all.csv"')
            self.end_headers()
            with open(csv_path, "rb") as f:
                self.wfile.write(f.read())
            return

        html_content = render_html_dashboard().encode('utf-8')
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html_content)

    def do_POST(self):
        if self.path != "/webhook":
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
                    
                # 1. Check User Security Status
                user = database.get_user(user_id)
                is_owner = (user_id == OWNER_USER_ID)
                
                if not is_owner:
                    if user is None:
                        # New unknown user -> Register as PENDING and alert owner
                        display_name = get_line_profile(user_id)
                        database.register_pending_user(user_id, display_name)
                        
                        # Notify Owner
                        push_line_message(
                            OWNER_USER_ID,
                            f"🔔 มีผู้ขอเข้าใช้งานระบบใหม่!\n👤 ชื่อ: {display_name}\n🆔 ID: {user_id}\n\nพิมพ์สั่งอนุมัติได้เลยครับ เช่น:\n'อนุมัติ {display_name} A'\n'อนุมัติ {display_name} B'\nหรือ 'บล็อก {display_name}'"
                        )
                        reply_line_message(reply_token, f"🔒 ขออภัยครับ บัญชีนี้เป็นระบบเฉพาะภายใน\nระบบได้ส่งคำขอไปยังเจ้าของระบบแล้ว กรุณารอการอนุมัติครับ")
                        continue
                        
                    elif user.get("status") == "PENDING":
                        reply_line_message(reply_token, "🔒 บัญชีของคุณอยู่ระหว่างรอเจ้าของระบบอนุมัติครับ กรุณารอสักครู่ครับ")
                        continue
                        
                    elif user.get("status") == "BLOCKED":
                        continue  # Silently ignore blocked users

                # 2. Approved User / Owner -> Process events
                if ev_type == "message":
                    msg = ev.get("message", {})
                    msg_type = msg.get("type")
                    
                    if msg_type == "image":
                        handle_image_message(msg.get("id"), reply_token, user_id, user or {})
                    elif msg_type == "text":
                        handle_text_message(msg.get("text", ""), reply_token, user_id, is_owner)
                        
        except Exception as e:
            print(f"Error handling event: {e}")

def run_server():
    server = http.server.HTTPServer(("0.0.0.0", PORT), LineWebhookHandler)
    print(f"🚀 LINE Bot Server running on port {PORT}...")
    server.serve_forever()

if __name__ == "__main__":
    run_server()
