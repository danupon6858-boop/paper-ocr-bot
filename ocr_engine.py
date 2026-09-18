import json
import base64
import time
import re
import urllib.request
import urllib.error
from config import GEMINI_API_KEY as _RAW_KEY
API_KEY = (_RAW_KEY or "").strip()

SYSTEM_PROMPT = """คุณคือ AI ผู้เชี่ยวชาญระดับสูงสุดด้านการถอดลายมือภาษาไทยและตัวเลขจากเอกสารบันทึกข้อมูล
หน้าที่ของคุณคืออ่านข้อมูลในกระดาษอย่างละเอียดถี่ถ้วน และแปลงเป็นโครงสร้าง JSON ตามที่กำหนดอย่างเคร่งครัด 100%

=======================================================
1. ลำดับการกวาดสายตา (SPATIAL SCANNING ORDER) — สำคัญที่สุด:
=======================================================
- กฎเหล็ก: ห้ามอ่านกวาดสายตาตามแนวนอนข้ามระหว่างคอลัมน์เด็ดขาด (ห้ามดึงตัวเลขจากคอลัมน์ขวามาต่อท้ายหรือปนกับคอลัมน์ซ้าย)
- ต้องอ่านทีละคอลัมน์แนวดิ่งตามลำดับนี้:
  1. หัวกระดาษ: สกัด sheet_id (เลขที่ใบ), customer_name (ชื่อลูกค้า/พนักงาน), date (วันที่)
  2. คอลัมน์ "บน" (มักอยู่ฝั่งซ้ายของกระดาษ หรืออาจเขียนย่อเป็น "บ"): ให้อ่านไล่จากแถวบนสุดลงมาทีละแถวจนถึงแถวล่างสุดของฝั่งซ้าย ห้ามมองข้ามไปฝั่งขวา
  3. คอลัมน์ "ล่าง" (มักอยู่ฝั่งขวาของกระดาษ หรืออาจเขียนย่อเป็น "ล"): ให้อ่านไล่จากแถวบนสุดลงมาทีละแถวจนถึงแถวล่างสุดของฝั่งขวา
  4. คอลัมน์ "บนล่าง" (ถ้ามีระบุหัวข้อชัดเจน หรืออาจเขียนย่อเป็น "บ-ล", "บล", "บ/ล" หรืออยู่โซนพิเศษ)
- หากกระดาษมีเพียงคอลัมน์เดียว หรือมีตัวเลขน้อย ให้อ่านตามลำดับบรรทัดจากบนลงล่างตามปกติ

=======================================================
2. ตัวอักษรที่ปรากฏในเอกสารนี้ (CHARACTER WHITELIST):
=======================================================
เอกสารนี้มีเฉพาะ:
- ตัวเลขอาราบิก 0-9 เท่านั้น (ไม่มีเลขไทย ๑-๙)
- อักษรไทยที่ปรากฏมีเพียง: "บน" (ย่อเป็น "บ"), "ล่าง" (ย่อเป็น "ล"), "บนล่าง" (ย่อเป็น "บ-ล", "บล", "บ/ล") (หัวคอลัมน์) และ "ก" (ใน ก3 หรือ ก6 เท่านั้น)
- ถ้าเห็นสัญลักษณ์อื่นที่ไม่ใช่นี้ ให้ตีความว่าเป็นตัวเลขที่เขียนไม่ชัด ไม่ใช่อักษรอื่น

กฎการแยกแยะตัวเลขที่หน้าตาคล้ายกัน (เขียนด้วยมือ):
- 0 vs 6: เลข 0 คือวงรีปิดสนิท ไม่มีหาง / เลข 6 มีหางลงซ้ายล่าง หรือเส้นวงด้านบนเปิด
- 0 vs 8: เลข 8 มีสองวงซ้อน บน-ล่าง / เลข 0 มีวงเดียว
- 1 vs 7: เลข 7 มีขีดบนยื่นออกด้านขวา หรือมีขีดกลาง / เลข 1 เส้นตรงลงมาเท่านั้น
- 3 vs 8: เลข 3 มีด้านซ้ายเปิด / เลข 8 ปิดทั้งสองวง
- 5 vs 6: เลข 5 หัวตัดแนวนอน / เลข 6 หัวโค้งวนลงมาปิด
- 2 vs 7: เลข 2 มีเส้นล่างนอน / เลข 7 ไม่มีเส้นล่าง เส้นแทยงลง

=======================================================
3. โครงสร้างและการแยกคู่ตัวเลข (NUMBER-PRICE EXTRACTION):
=======================================================
แต่ละรายการประกอบด้วย:
- set1 (ชุดที่ 1): ตัวเลขหวย 2-4 หลัก (เช่น 401, 370, 605, 12, 43, 68, 19)
- set3 (ชุดที่ 3): รหัสพิเศษ "ก3" หรือ "ก6" เท่านั้น (ถ้าไม่มีให้ใส่ "")
- set2 (ชุดที่ 2): ยอดเงิน สามารถเป็นเลขเดี่ยว (เช่น 12, 100) หรือ NxN (เช่น 120x120, 200x300, 36x36, 50x50, 5x5)

กฎเหล็ก:
- ถ้ามี set3 (ก3 หรือ ก6) -> set2 จะต้องเป็นตัวเลขเดี่ยวเท่านั้นเสมอ (เช่น "12") จะไม่มีเครื่องหมาย x
- ตัวคั่นระหว่าง set1 และ set2 อาจเขียนเป็น '=', '-', ':', '/', หรือแค่เว้นวรรค (space) หรือมีระยะห่างที่บอกว่าเป็นคนละชุด ให้แยกตัวเลขหวย (set1: 2-4 หลัก) กับยอดเงิน (set2) ออกจากกันให้ถูกต้อง
- หากในบรรทัดเดียวกันมีตัวเลขหลายคู่ (เช่น "605=200 609=100" หรือ "12-20, 34-30" หรือ "12 20 34 30") ให้แยกออกเป็นคนละรายการใน Array ทันที ห้ามรวมกันในชุดเดียว

=======================================================
4. กฎการกรองข้อมูลที่ไม่เกี่ยวข้อง (NOISE FILTERING) — สำคัญมาก:
=======================================================
- ข้อความที่พิมพ์มาจากฟอร์ม (ข้อความสำเร็จรูป, หัวตาราง, โลโก้, เส้นตาราง): ห้ามนำมาใส่ใน JSON เด็ดขาด
- รายการที่ถูกขีดฆ่า หรือมีเส้นทับผ่าน: ให้ข้ามทั้งรายการนั้น (ถือว่ายกเลิก)
- ลายเซ็น, วงกลม, ลูกศร, เครื่องหมายอื่นๆ ที่ไม่ใช่ตัวเลข: ให้ข้าม
- รายการที่ไม่มี set1 (ตัวเลข 2-4 หลัก) + set2 (ยอดเงิน) ครบคู่: ให้ข้ามรายการนั้น ห้ามเดา
- ถ้าบรรทัดใดอ่านไม่ออกจริงๆ ให้ข้ามเฉพาะบรรทัดนั้น และอ่านบรรทัดอื่นต่อ

=======================================================
5. กฎสำหรับภาพหนาแน่น หรือลายมือเขียนชิดกัน (HIGH-DENSITY SHEETS):
=======================================================
- หากตัวเลขเขียนติดหรือเบียดกัน ให้เพ่งดูลายเส้นปากกาแยกแต่ละหลักอย่างอิสระ
- หากหางตัวเลขบรรทัดบนลากมาแตะหัวเลขบรรทัดล่าง ให้อ่านแยกแถวกันตามระดับบรรทัด
- หากมีรายการที่เบลอจนอ่านไม่ออก ให้ข้ามเฉพาะจุดนั้นไป และอ่านรายการอื่นๆ ให้ครบถ้วน

=======================================================
6. โครงสร้าง JSON ที่ต้องส่งออก (ตอบเป็น JSON ล้วนเท่านั้น):
=======================================================
{
  "header": {
    "sheet_id": "1",
    "customer_name": "",
    "date": "",
    "total_amount": ""
  },
  "columns": {
    "top": [
      {"set1": "401", "set3": "", "set2": "120x120", "raw_text": "401 = 120x120"}
    ],
    "bottom": [
      {"set1": "12", "set3": "", "set2": "50", "raw_text": "12 = 50"}
    ],
    "top_bottom": []
  }
}
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
    ("v1beta", "gemini-flash-lite-latest", 2),
    ("v1beta", "gemini-3.5-flash-lite", 1),
    ("v1beta", "gemini-3.5-flash", 1),
    ("v1beta", "gemini-flash-latest", 1),
    ("v1beta", "gemini-pro-latest", 1),
    ("v1beta", "gemini-3.8-flash", 1)
]
GLOBAL_TIMEOUT_SECONDS = 50
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

THAI_DIGITS_TABLE = str.maketrans("๑๒๓๔๕๖๗๘๙๐", "1234567890")

def auto_repair_extracted_data(ocr_result: dict) -> dict:
    if not isinstance(ocr_result, dict):
        ocr_result = {}
        
    # Guarantee header dictionary and string fields (protect against None/null)
    header = ocr_result.get("header")
    if not isinstance(header, dict):
        header = {}
    ocr_result["header"] = {
        "sheet_id": str(header.get("sheet_id") or "").strip(),
        "customer_name": str(header.get("customer_name") or "").strip(),
        "date": str(header.get("date") or "").strip(),
        "total_amount": str(header.get("total_amount") or "").strip(),
    }
    
    columns = ocr_result.get("columns")
    if not isinstance(columns, dict):
        columns = {}
        
    repaired_cols = {}
    for col_key in ["top", "bottom", "top_bottom"]:
        items = columns.get(col_key)
        if not isinstance(items, list):
            items = []
        new_items = []
        for itm in items:
            if not isinstance(itm, dict):
                continue
                
            s1 = str(itm.get("set1") or "").translate(THAI_DIGITS_TABLE).strip()
            s2 = str(itm.get("set2") or "").translate(THAI_DIGITS_TABLE).strip()
            s3 = str(itm.get("set3") or "").strip()
            raw = str(itm.get("raw_text") or "").strip()
            
            # Skip completely empty entries
            if not s1 and not s2:
                continue
                
            # Check if s1 contains an embedded price or separator (e.g. s1="401=120", "401-120", "401:120", "401 120")
            if not s2:
                m_split = re.match(r"^(\d{2,4})\s*[=\-:/ ]\s*(.+)$", s1)
                if m_split:
                    s1 = m_split.group(1).strip()
                    s2 = m_split.group(2).strip()
                    
            # Check if s1 contains embedded special code (e.g. "401ก350" or "401 ก3 50")
            m_s3_in_s1 = re.match(r"^(\d{2,4})\s*(ก[36])\s*(.*)$", s1)
            if m_s3_in_s1:
                s1 = m_s3_in_s1.group(1).strip()
                s3 = m_s3_in_s1.group(2).strip()
                if not s2 and m_s3_in_s1.group(3):
                    s2 = m_s3_in_s1.group(3).strip()
                
            # Repeatedly split dense pairs written on the same line (e.g. "50x50 377=12 450=30")
            curr_s1 = s1
            curr_s2 = s2
            curr_s3 = s3
            
            while True:
                m = re.search(r"^(\d+(?:[xX]\d+)?)\s*[,;/ ]+\s*(\d{2,4})\s*[=\-:/ ]\s*(.+)$", curr_s2)
                if m:
                    first_s2 = m.group(1).replace("X", "x").strip()
                    next_s1 = m.group(2).strip()
                    next_s2 = m.group(3).strip()
                    new_items.append({"set1": curr_s1, "set2": first_s2, "set3": curr_s3, "raw_text": f"{curr_s1} = {first_s2}"})
                    curr_s1 = next_s1
                    curr_s2 = next_s2
                    curr_s3 = ""
                else:
                    final_s2 = curr_s2.replace("X", "x").replace(" ", "").rstrip(",.-;/ ")
                    # Check if s3 is in final_s2 (e.g. "ก312")
                    if not curr_s3:
                        m_s3 = re.search(r"(ก3|ก6)", final_s2)
                        if m_s3:
                            curr_s3 = m_s3.group(1)
                            final_s2 = final_s2.replace(curr_s3, "").strip()

                    # ── Post-processing validation: drop garbage entries ──
                    # set1 must be 2-4 Arabic digits only
                    if not re.fullmatch(r"\d{2,4}", curr_s1):
                        break
                    # set2 must be a plain number OR NxN format
                    if not re.fullmatch(r"\d+(?:x\d+)?", final_s2):
                        break
                    # set3 must be empty, ก3, or ก6 only
                    if curr_s3 and curr_s3 not in ("ก3", "ก6"):
                        curr_s3 = ""

                    new_items.append({"set1": curr_s1, "set2": final_s2, "set3": curr_s3, "raw_text": raw or f"{curr_s1} = {final_s2}"})
                    break
                    
        repaired_cols[col_key] = new_items
        
    ocr_result["columns"] = repaired_cols
    return ocr_result

def extract_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    if not API_KEY or not str(API_KEY).strip():
        raise ValueError("ยังไม่ได้ตั้งค่า GEMINI_API_KEY ใน Render Environment Variables")

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
            "temperature": 0.1
        }
    }
    
    payload_bytes = json.dumps(payload).encode("utf-8")
    last_exception = None
    last_error_body = ""
    start_time = time.time()

    # Retry loop with fast failover and strict global timeout
    for api_ver, model_name, max_attempts in MODELS_CONFIG:
        for attempt in range(max_attempts):
            elapsed = time.time() - start_time
            if elapsed >= GLOBAL_TIMEOUT_SECONDS:
                print(f"Global timeout budget ({GLOBAL_TIMEOUT_SECONDS}s) reached. Aborting OCR.")
                raise TimeoutError("AI ใช้เวลาประมวลผลภาพนี้นานเกินกำหนด (ภาพอ่านยาก)")

            url = f"https://generativelanguage.googleapis.com/{api_ver}/models/{model_name}:generateContent?key={API_KEY}"
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
                    parsed = clean_and_parse_json(text)
                    return auto_repair_extracted_data(parsed)
            except urllib.error.HTTPError as e:
                last_exception = e
                err_body = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else ""
                last_error_body = err_body
                print(f"[{api_ver}/{model_name}] HTTP Error {e.code} on attempt {attempt+1}: {e.reason} - {err_body}")
                if e.code in [500, 502, 503, 504, 429]:
                    if time.time() - start_time + 1.5 < GLOBAL_TIMEOUT_SECONDS:
                        time.sleep(1.0 * (attempt + 1))
                        continue
                if e.code in [401, 403]:
                    msg = "GEMINI_API_KEY ไม่ถูกต้องหรือหมดอายุ"
                    try:
                        ed = json.loads(err_body)
                        msg = ed.get("error", {}).get("message", msg)
                    except Exception:
                        pass
                    raise RuntimeError(f"Google Gemini Auth ({e.code}): {msg}")
                break
            except Exception as e:
                last_exception = e
                print(f"[{api_ver}/{model_name}] Error on attempt {attempt+1}: {e}")
                if time.time() - start_time + 1.5 < GLOBAL_TIMEOUT_SECONDS:
                    time.sleep(1.0)
                    continue
                break

    if last_exception:
        err_msg = str(last_exception)
        if last_error_body:
            try:
                ed = json.loads(last_error_body)
                err_msg = ed.get("error", {}).get("message", last_error_body)
            except Exception:
                err_msg = last_error_body[:120]
        raise RuntimeError(f"Google Gemini ({getattr(last_exception, 'code', 'Error')}): {err_msg[:120]}")
    raise RuntimeError("Unable to extract data from image after retries.")

def extract_from_file(file_path: str) -> dict:
    with open(file_path, "rb") as f:
        data = f.read()
    mime = "image/png" if file_path.lower().endswith(".png") else "image/jpeg"
    return extract_from_image(data, mime)

def list_gemini_models() -> list:
    if not API_KEY or not str(API_KEY).strip():
        return []
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={API_KEY}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return [m.get("name", "").replace("models/", "") for m in data.get("models", [])]
    except Exception as e:
        return [f"Error: {e}"]

def test_gemini_connection() -> dict:
    if not API_KEY or not str(API_KEY).strip():
        return {"status": "error", "message": "GEMINI_API_KEY is not set"}
    
    available = list_gemini_models()
    
    dummy_png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    test_payload = {
        "contents": [
            {
                "parts": [
                    {"text": "Ping test. Please reply with the word PONG"},
                    {
                        "inline_data": {
                            "mime_type": "image/png",
                            "data": dummy_png_b64
                        }
                    }
                ]
            }
        ]
    }
    payload_bytes = json.dumps(test_payload).encode("utf-8")
    attempts = []
    
    for api_ver, model_name, _ in MODELS_CONFIG:
        t0 = time.time()
        url = f"https://generativelanguage.googleapis.com/{api_ver}/models/{model_name}:generateContent?key={API_KEY}"
        req = urllib.request.Request(
            url,
            data=payload_bytes,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                latency_ms = int((time.time() - t0) * 1000)
                text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                return {
                    "status": "success",
                    "working_model": f"{api_ver}/{model_name}",
                    "latency_ms": latency_ms,
                    "response": text.strip()[:100],
                    "available_models": available,
                    "all_attempts": attempts
                }
        except urllib.error.HTTPError as e:
            err_b = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else ""
            attempts.append({"model": f"{api_ver}/{model_name}", "http_code": e.code, "error": err_b[:200]})
        except Exception as e:
            attempts.append({"model": f"{api_ver}/{model_name}", "error": str(e)[:200]})
            
    return {
        "status": "failed",
        "error": "All models failed ping test",
        "available_models": available,
        "attempts": attempts
    }
