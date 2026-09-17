import json
import base64
import time
import re
import urllib.request
import urllib.error
from config import GEMINI_API_KEY as API_KEY

SYSTEM_PROMPT = """คุณคือ AI ผู้เชี่ยวชาญด้านการถอดลายมือภาษาไทยและตัวเลขจากเอกสารบันทึกข้อมูล
หน้าที่ของคุณคืออ่านข้อมูลในกระดาษแล้วแปลงเป็น JSON ตามโครงสร้างที่กำหนดอย่างเคร่งครัด 100%

โครงสร้างข้อมูลในแต่ละรายการ:
- แต่ละรายการจะประกอบด้วย:
  1. set1 (ชุดที่ 1): ตัวเลขล้วน (2-4 หลัก) เช่น 401, 370, 690, 377, 450, 12, 43, 68, 19
  2. set3 (ชุดที่ 3): ตัวอักษร "ก" คู่กับเลข 3 หรือ 6 เท่านั้น (คือ "ก3" หรือ "ก6") หากไม่มีให้ใส่ string ว่าง ""
  3. set2 (ชุดที่ 2): ตัวเลขเดี่ยว (เช่น 12, 24, 80) หรือ ตัวเลข x ตัวเลข (เช่น 120x120, 36x36, 12x12, 24x24)
- กฎเหล็กสำคัญมาก:
  * ถ้ามี set3 (ก3 หรือ ก6) -> set2 จะต้องเป็นตัวเลขเดี่ยวเท่านั้นเสมอ (เช่น "12") จะไม่มีเครื่องหมาย x
  * ตัวคั่นระหว่างชุดที่ 1 กับชุดที่ 2 อาจเขียนเป็น '=', '-', หรือ ',' ให้ถอดเฉพาะค่าของ set1, set3, set2
- แยกหมวดหมู่ตามหัวคอลัมน์:
  * "บน" (เช่น ฝั่งซ้าย หรือตามหัวข้อ "บน")
  * "ล่าง" (เช่น ฝั่งขวา หรือตามหัวข้อ "ล่าง")
  * "บนล่าง" (ถ้ามี)
- สำคัญมาก: ต้องสแกนอ่านให้ครบทุกคอลัมน์ทั่วทั้งแผ่น (ทั้งฝั่งซ้ายและฝั่งขวา) ห้ามมองข้ามคอลัมน์ใดคอลัมน์หนึ่ง
- หากมีข้อมูลหัวเอกสาร เช่น ชื่อลูกค้า/พนักงาน, วันที่, ใบที่ ให้สกัดออกมาด้วย (ถ้าไม่มีให้ใส่ "")
- ข้อกำหนดสำหรับภาพที่อ่านยากหรือไม่ชัด:
  * ให้อ่านเฉพาะรายการที่เห็นตัวเลขชัดเจน ส่วนที่เบลอหรืออ่านไม่ออกให้ข้ามไป
  * ห้ามตอบข้อความบรรยาย คำอธิบาย หรือข้อความอื่นนอกเหนือจากรูปแบบ JSON อย่างเด็ดขาด
"""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "header": {
            "type": "OBJECT",
            "properties": {
                "sheet_id": {"type": "STRING", "description": "เลขที่ใบ / ใบที่ (เช่น 96/17 หรือ A3)"},
                "customer_name": {"type": "STRING", "description": "ชื่อลูกค้า / ชื่อพนักงาน"},
                "date": {"type": "STRING", "description": "วันที่"},
                "total_amount": {"type": "STRING", "description": "ยอดรวมหรือจำนวนเงินที่เขียนไว้มุมบนหรือหัวกระดาษ (ถ้ามี)"}
            },
            "required": ["sheet_id", "date"]
        },
        "columns": {
            "type": "OBJECT",
            "properties": {
                "top": {
                    "type": "ARRAY",
                    "description": "รายการในหมวด 'บน'",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "set1": {"type": "STRING", "description": "ชุด 1: ตัวเลข 2-4 หลัก"},
                            "set3": {"type": "STRING", "description": "ชุด 3: 'ก3' หรือ 'ก6' เท่านั้น หรือว่างถ้าไม่มี"},
                            "set2": {"type": "STRING", "description": "ชุด 2: ตัวเลขเดี่ยว หรือ NxN"},
                            "raw_text": {"type": "STRING", "description": "ข้อความดิบที่เห็น"}
                        },
                        "required": ["set1", "set3", "set2"]
                    }
                },
                "bottom": {
                    "type": "ARRAY",
                    "description": "รายการในหมวด 'ล่าง'",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "set1": {"type": "STRING", "description": "ชุด 1: ตัวเลข 2-4 หลัก"},
                            "set3": {"type": "STRING", "description": "ชุด 3: 'ก3' หรือ 'ก6' เท่านั้น หรือว่างถ้าไม่มี"},
                            "set2": {"type": "STRING", "description": "ชุด 2: ตัวเลขเดี่ยว หรือ NxN"},
                            "raw_text": {"type": "STRING", "description": "ข้อความดิบที่เห็น"}
                        },
                        "required": ["set1", "set3", "set2"]
                    }
                },
                "top_bottom": {
                    "type": "ARRAY",
                    "description": "รายการในหมวด 'บนล่าง'",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "set1": {"type": "STRING", "description": "ชุด 1: ตัวเลข 2-4 หลัก"},
                            "set3": {"type": "STRING", "description": "ชุด 3: 'ก3' หรือ 'ก6' เท่านั้น หรือว่างถ้าไม่มี"},
                            "set2": {"type": "STRING", "description": "ชุด 2: ตัวเลขเดี่ยว หรือ NxN"},
                            "raw_text": {"type": "STRING", "description": "ข้อความดิบที่เห็น"}
                        },
                        "required": ["set1", "set3", "set2"]
                    }
                }
            },
            "required": ["top", "bottom", "top_bottom"]
        }
    },
    "required": ["header", "columns"]
}

MODELS_CONFIG = [
    ("gemini-3.6-flash", 2),
    ("gemini-flash-latest", 1)
]
GLOBAL_TIMEOUT_SECONDS = 28
PER_REQUEST_TIMEOUT = 20

def clean_and_parse_json(text: str) -> dict:
    text = text.strip()
    if not text:
        raise ValueError("Empty response from Gemini")
    
    # Strip markdown codeblocks
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
        
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
        
    # Search for first outermost { ... }
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
            
    raise ValueError(f"Could not parse valid JSON from text: {text[:100]}")

def extract_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    img_b64 = base64.b64encode(image_bytes).decode("utf-8")
    
    payload = {
        "system_instruction": {
            "parts": [{"text": SYSTEM_PROMPT}]
        },
        "contents": [
            {
                "parts": [
                    {"text": "กรุณาอ่านข้อมูลจากภาพนี้อย่างละเอียด และแปลงเป็นโครงสร้าง JSON ตามที่กำหนด"},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": img_b64
                        }
                    }
                ]
            }
        ],
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": RESPONSE_SCHEMA,
            "temperature": 0.1
        }
    }
    
    payload_bytes = json.dumps(payload).encode("utf-8")
    last_exception = None
    start_time = time.time()

    # Retry loop with fast failover and strict global timeout
    for model_name, max_attempts in MODELS_CONFIG:
        for attempt in range(max_attempts):
            elapsed = time.time() - start_time
            if elapsed >= GLOBAL_TIMEOUT_SECONDS:
                print(f"Global timeout budget ({GLOBAL_TIMEOUT_SECONDS}s) reached. Aborting OCR.")
                raise TimeoutError("AI ใช้เวลาประมวลผลภาพนี้นานเกินกำหนด (ภาพอ่านยาก)")

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={API_KEY}"
            req = urllib.request.Request(
                url,
                data=payload_bytes,
                headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(req, timeout=PER_REQUEST_TIMEOUT) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    candidates = data.get("candidates", [])
                    if not candidates:
                        raise ValueError("No candidates returned from Gemini")
                    
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if not parts:
                        raise ValueError("Empty content parts in Gemini response")
                    
                    text = parts[0].get("text", "")
                    return clean_and_parse_json(text)
            except urllib.error.HTTPError as e:
                last_exception = e
                err_body = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else ""
                print(f"[{model_name}] HTTP Error {e.code} on attempt {attempt+1}: {e.reason} - {err_body}")
                if e.code in [500, 502, 503, 504, 429]:
                    if time.time() - start_time + 1.5 < GLOBAL_TIMEOUT_SECONDS:
                        time.sleep(1.0)
                        continue
                break
            except Exception as e:
                last_exception = e
                print(f"[{model_name}] Error on attempt {attempt+1}: {e}")
                if time.time() - start_time + 1.5 < GLOBAL_TIMEOUT_SECONDS:
                    time.sleep(1.0)
                    continue
                break

    if last_exception:
        raise last_exception
    raise RuntimeError("Unable to extract data from image after retries.")

def extract_from_file(file_path: str) -> dict:
    with open(file_path, "rb") as f:
        data = f.read()
    mime = "image/png" if file_path.lower().endswith(".png") else "image/jpeg"
    return extract_from_image(data, mime)
