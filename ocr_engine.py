import json
import base64
import urllib.request
import urllib.error
from config import GEMINI_API_KEY as API_KEY, MODEL

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

def extract_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    img_b64 = base64.b64encode(image_bytes).decode("utf-8")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={API_KEY}"
    
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
    
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)

def extract_from_file(file_path: str) -> dict:
    with open(file_path, "rb") as f:
        data = f.read()
    mime = "image/png" if file_path.lower().endswith(".png") else "image/jpeg"
    return extract_from_image(data, mime)
