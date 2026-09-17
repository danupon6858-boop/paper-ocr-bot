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
        "messages": [
            {
                "type": "text",
                "text": text
            }
        ]
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

def get_line_image_content(message_id: str) -> bytes:
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
        }
    )
    with urllib.request.urlopen(req) as resp:
        return resp.read()

def handle_image_message(message_id: str, reply_token: str, user_id: str):
    try:
        # 1. Download image
        print(f"Downloading image message {message_id}...")
        img_bytes = get_line_image_content(message_id)
        
        # 2. Extract with Gemini
        print("Processing OCR with Gemini 3.6 Flash...")
        ocr_result = ocr_engine.extract_from_image(img_bytes)
        
        # 3. Validate
        print("Validating rules...")
        val_result = OCRValidator.validate_document(ocr_result)
        
        # 4. Save to DB
        sheet_id = database.save_document(ocr_result, val_result)
        
        # 5. Build response message
        header = ocr_result.get("header", {})
        sheet_no = header.get("sheet_id") or f"แผ่นที่ #{sheet_id}"
        emp_name = header.get("customer_name") or "-"
        date_str = header.get("date") or "-"
        
        cols = ocr_result.get("columns", {})
        top_items = cols.get("top", [])
        bot_items = cols.get("bottom", [])
        topbot_items = cols.get("top_bottom", [])
        
        total_items = len(top_items) + len(bot_items) + len(topbot_items)
        
        lines = []
        if val_result.get("is_all_valid"):
            lines.append("✅ บันทึกข้อมูลเรียบร้อย 100%")
        else:
            lines.append("⚠️ อ่านข้อมูลได้ แต่พบจุดที่ต้องตรวจทาน:")
            for err in val_result.get("errors", [])[:5]:
                lines.append(f"  • {err}")
            if len(val_result.get("errors", [])) > 5:
                lines.append(f"  (และอีก {len(val_result.get('errors', [])) - 5} จุด)")
                
        lines.append("─────────────────────────")
        lines.append(f"📋 ใบที่: {sheet_no}")
        if emp_name != "-":
            lines.append(f"👤 ผู้บันทึก: {emp_name}")
        if date_str != "-":
            lines.append(f"📅 วันที่: {date_str}")
        lines.append(f"📊 รายการทั้งหมด: {total_items} รายการ")
        lines.append(f"  • บน: {len(top_items)} รายการ")
        lines.append(f"  • ล่าง: {len(bot_items)} รายการ")
        if topbot_items:
            lines.append(f"  • บนล่าง: {len(topbot_items)} รายการ")
            
        reply_line_message(reply_token, "\n".join(lines))
        
    except Exception as e:
        print(f"Error handling image: {e}")
        reply_line_message(reply_token, f"❌ ขออภัย ระบบอ่านภาพขัดข้อง: {e}\nกรุณาลองถ่ายภาพส่งใหม่อีกครั้งครับ")

def handle_text_message(text: str, reply_token: str, user_id: str):
    clean_text = text.strip()
    
    # 1. Search Query command: "เช็ค 310", "เช็ค 401 377", "check 310"
    if clean_text.startswith("เช็ค") or clean_text.lower().startswith("check"):
        numbers = re.findall(r"\d+", clean_text)
        if not numbers:
            reply_line_message(reply_token, "⚠️ กรุณาระบุตัวเลขที่ต้องการค้นหา เช่น\n'เช็ค 310' หรือ 'เช็ค 401 377'")
            return
        result_msg = query_service.format_search_results(numbers)
        reply_line_message(reply_token, result_msg)
        return
        
    # 2. Status command
    if clean_text in ["สถานะ", "status", "ยอด", "สรุป"]:
        status_msg = query_service.format_daily_status()
        reply_line_message(reply_token, status_msg)
        return
        
    # 3. Help menu
    if clean_text in ["เมนู", "menu", "help", "?"]:
        help_msg = (
            "📋 เมนูการใช้งานระบบ\n"
            "─────────────────────────\n"
            "1️⃣ ส่งรูปถ่ายกระดาษ ➔ ระบบอ่านและบันทึกอัตโนมัติ\n"
            "2️⃣ พิมพ์ 'เช็ค [เลข]' ➔ ค้นหาชุด 1 เช่น เช็ค 310 หรือ เช็ค 401 370\n"
            "3️⃣ พิมพ์ 'สถานะ' ➔ ดูยอดรวมและใบที่เข้าระบบแล้ววันนี้\n"
            "─────────────────────────\n"
            "💡 พนักงานส่งรูปถ่ายเข้ามาได้เลยครับ"
        )
        reply_line_message(reply_token, help_msg)
        return
        
    # Default message
    reply_line_message(
        reply_token,
        "💡 คุณสามารถส่งรูปกระดาษบันทึกเข้ามาได้เลยครับ\nหรือพิมพ์ 'เช็ค [ตัวเลข]' เพื่อค้นหาข้อมูล\n(พิมพ์ 'เมนู' เพื่อดูคำสั่งทั้งหมด)"
    )

class LineWebhookHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "running", "service": "Paper-OCR-Bot"}).encode())

    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return
            
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length)
        signature = self.headers.get("X-Line-Signature", "")
        
        # Verify signature
        if not verify_line_signature(body_bytes, signature):
            print("Invalid signature rejected!")
            self.send_response(403)
            self.end_headers()
            return
            
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
        
        # Process events
        try:
            payload = json.loads(body_bytes.decode('utf-8'))
            events = payload.get("events", [])
            for ev in events:
                ev_type = ev.get("type")
                reply_token = ev.get("replyToken")
                source = ev.get("source", {})
                user_id = source.get("userId", "")
                
                if ev_type == "message":
                    msg = ev.get("message", {})
                    msg_type = msg.get("type")
                    
                    if msg_type == "image":
                        handle_image_message(msg.get("id"), reply_token, user_id)
                    elif msg_type == "text":
                        handle_text_message(msg.get("text", ""), reply_token, user_id)
        except Exception as e:
            print(f"Error handling event: {e}")

def run_server():
    server = http.server.HTTPServer(("0.0.0.0", PORT), LineWebhookHandler)
    print(f"🚀 LINE Bot Server running on port {PORT}...")
    server.serve_forever()

if __name__ == "__main__":
    run_server()
