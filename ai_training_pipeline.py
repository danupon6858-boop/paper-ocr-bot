"""
AI Training Pipeline & Dataset Packaging for Paper OCR Bot
Packages scanned paper images and verified entries into standard
Vision-Language Model (VLM) training format for Qwen2-VL / LLaMA-Factory / Colab fine-tuning.
"""

import os
import io
import re
import json
import time
import zipfile
from typing import Optional, Dict, List, Tuple
import database

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def get_dataset_readiness(period_id: Optional[int] = None) -> Dict:
    """
    Evaluates dataset readiness for VLM fine-tuning.
    Returns counts of sheets with images, total verified entries, and readiness status.
    """
    database.init_db()
    conn = database.get_db_connection()
    cursor = conn.cursor()
    
    if period_id:
        cursor.execute("SELECT id, name FROM periods WHERE id = ?", (period_id,))
        p_row = cursor.fetchone()
        p_name = p_row["name"] if p_row else f"งวดที่ {period_id}"
        
        cursor.execute("""
        SELECT id, sheet_id, image_path, total_amount, worker_code, employee_name, created_at
        FROM sheets
        WHERE period_id = ? AND image_path IS NOT NULL AND image_path != ''
        """, (period_id,))
        sheets = cursor.fetchall()
        
        cursor.execute("""
        SELECT count(*) as count FROM entries
        WHERE period_id = ? AND is_valid = 1
        """, (period_id,))
        total_entries = cursor.fetchone()["count"]
    else:
        p_name = "ทุกงวดสะสม (All-Time)"
        cursor.execute("""
        SELECT id, sheet_id, image_path, total_amount, worker_code, employee_name, created_at
        FROM sheets
        WHERE image_path IS NOT NULL AND image_path != ''
        """)
        sheets = cursor.fetchall()
        
        cursor.execute("""
        SELECT count(*) as count FROM entries
        WHERE is_valid = 1
        """)
        total_entries = cursor.fetchone()["count"]
        
    conn.close()
    
    total_sheets = len(sheets)
    min_recommended = 30
    readiness_pct = min(100, int((total_sheets / min_recommended) * 100)) if min_recommended > 0 else 100
    
    if total_sheets >= 30:
        status_level = "ready"
        status_badge = "🟢 พร้อมเทรนระดับสูง (High Readiness)"
        status_desc = f"สะสมได้ {total_sheets} ใบ (เกินเป้าหมายขั้นต่ำ 30 ใบ) แนะนำเทรน LoRA 5 Epochs เพื่อผลลัพธ์สูงสุด"
    elif total_sheets >= 10:
        status_level = "moderate"
        status_badge = "🟡 พอเริ่มทดสอบได้ (Moderate)"
        status_desc = f"สะสมได้ {total_sheets}/30 ใบ ({readiness_pct}%) สามารถรัน LoRA เบื้องต้นเพื่อดูแนวโน้มได้ แต่ถ้าสะสมครบ 30 ใบจะแม่นยำยิ่งขึ้น"
    elif total_sheets > 0:
        status_level = "collecting"
        status_badge = "🔵 กำลังสะสมข้อมูล (Collecting)"
        status_desc = f"มีข้อมูลแล้ว {total_sheets}/30 ใบ ({readiness_pct}%) สามารถดาวน์โหลดชุด Bundle ไปทดสอบได้ทันที หรือถ่ายรูปโพยเพิ่มเพื่อความแม่นยำ"
    else:
        status_level = "empty"
        status_badge = "⚪️ ยังไม่มีภาพต้นฉบับ"
        status_desc = "ยังไม่มีรูปโพยที่บันทึกไว้ในระบบ กรุณาส่งรูปผ่าน LINE เพื่อเริ่มสะสม Dataset"

    return {
        "period_id": period_id,
        "period_name": p_name,
        "total_sheets": total_sheets,
        "total_entries": total_entries,
        "min_recommended": min_recommended,
        "readiness_pct": readiness_pct,
        "status_level": status_level,
        "status_badge": status_badge,
        "status_desc": status_desc
    }

def _resolve_image_bytes(image_path: str) -> Optional[Tuple[bytes, str]]:
    """Resolves image bytes and file extension from local path."""
    if not image_path:
        return None
        
    candidate_paths = [
        image_path,
        os.path.join(BASE_DIR, image_path),
        os.path.join(BASE_DIR, image_path.lstrip("/")),
    ]
    
    for path in candidate_paths:
        if os.path.exists(path) and os.path.isfile(path):
            try:
                ext = os.path.splitext(path)[1].lower() or ".jpg"
                with open(path, "rb") as f:
                    return f.read(), ext
            except Exception as e:
                print(f"Error reading image {path}: {e}")
                
    return None

def build_training_bundle(period_id: Optional[int] = None) -> bytes:
    """
    Compiles all scanned paper sheets and ground-truth verified entries into a complete,
    ready-to-train ZIP archive containing images/, dataset.jsonl (ShareGPT/Qwen2-VL format),
    dataset_plain.json, metadata.json, README.md, and Paper_OCR_FineTune_Colab.ipynb.
    """
    database.init_db()
    conn = database.get_db_connection()
    cursor = conn.cursor()
    
    if period_id:
        cursor.execute("SELECT id, name FROM periods WHERE id = ?", (period_id,))
        p_row = cursor.fetchone()
        p_name = p_row["name"] if p_row else f"Period {period_id}"
        cursor.execute("""
        SELECT id, sheet_id, image_path, total_amount, worker_code, employee_name, created_at
        FROM sheets
        WHERE period_id = ? AND image_path IS NOT NULL AND image_path != ''
        ORDER BY id ASC
        """, (period_id,))
        sheets = cursor.fetchall()
    else:
        p_name = "All-Time Dataset"
        cursor.execute("""
        SELECT id, sheet_id, image_path, total_amount, worker_code, employee_name, created_at
        FROM sheets
        WHERE image_path IS NOT NULL AND image_path != ''
        ORDER BY id ASC
        """)
        sheets = cursor.fetchall()

    zip_buffer = io.BytesIO()
    
    dataset_sharegpt = []
    dataset_plain = []
    total_entries_count = 0
    exported_images_count = 0
    
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for s in sheets:
            sheet_db_id = s["id"]
            sheet_id = s["sheet_id"]
            img_path = s["image_path"]
            
            # Fetch entries for this sheet
            cursor.execute("""
            SELECT category, set1, set3, set2, is_valid
            FROM entries
            WHERE sheet_db_id = ? AND is_valid = 1
            ORDER BY id ASC
            """, (sheet_db_id,))
            entries = cursor.fetchall()
            
            if not entries:
                continue
                
            total_entries_count += len(entries)
            
            # Resolve image
            resolved = _resolve_image_bytes(img_path)
            safe_sid = re.sub(r'[^a-zA-Z0-9_\-]', '_', str(sheet_id))
            
            if resolved:
                img_data, ext = resolved
                arc_img_name = f"{safe_sid}_{sheet_db_id}{ext}"
                arc_img_rel = f"images/{arc_img_name}"
                zf.writestr(arc_img_rel, img_data)
                exported_images_count += 1
            else:
                arc_img_rel = f"images/{safe_sid}_{sheet_db_id}.jpg"
                
            # Structure category outputs
            cat_entries = {"บน": [], "ล่าง": [], "บนล่าง": []}
            text_lines = []
            
            for e in entries:
                cat = e["category"] or "บน"
                s1 = str(e["set1"] or "").strip()
                s3 = str(e["set3"] or "").strip()
                s2 = str(e["set2"] or "").strip()
                
                cat_entries.setdefault(cat, []).append({
                    "set1": s1,
                    "set3": s3,
                    "set2": s2
                })
                
            for cat_name in ["บน", "ล่าง", "บนล่าง"]:
                items = cat_entries.get(cat_name, [])
                if items:
                    text_lines.append(f"หมวด {cat_name}:")
                    for it in items:
                        s3_str = f" {it['set3']}" if it['set3'] else ""
                        text_lines.append(f"{it['set1']} ={s3_str} {it['set2']}")
            
            plain_text_gt = "\n".join(text_lines)
            json_gt = json.dumps(cat_entries, ensure_ascii=False, indent=2)
            
            # ShareGPT VLM standard format
            sharegpt_item = {
                "id": f"sheet_{sheet_db_id}_{safe_sid}",
                "image": arc_img_rel,
                "sheet_id": sheet_id,
                "conversations": [
                    {
                        "from": "human",
                        "value": "<image>\nตรวจจับตัวเลขจากรูปถ่ายโพยกระดาษ และแยกข้อมูลออกเป็น 3 หมวด (บน, ล่าง, บนล่าง) พร้อมระบุเลขชุดที่ 1, ชุดที่ 3 (ถ้ามี), และชุดที่ 2 ในรูปแบบ JSON"
                    },
                    {
                        "from": "gpt",
                        "value": json_gt
                    }
                ]
            }
            dataset_sharegpt.append(sharegpt_item)
            
            # Plain format for quick lookup or lightweight models
            dataset_plain.append({
                "sheet_id": sheet_id,
                "image": arc_img_rel,
                "ground_truth_text": plain_text_gt,
                "ground_truth_json": cat_entries
            })

        # Write dataset.jsonl
        jsonl_str = "\n".join(json.dumps(row, ensure_ascii=False) for row in dataset_sharegpt)
        zf.writestr("dataset.jsonl", jsonl_str.encode("utf-8"))
        
        # Write dataset_plain.json
        plain_json_str = json.dumps(dataset_plain, ensure_ascii=False, indent=2)
        zf.writestr("dataset_plain.json", plain_json_str.encode("utf-8"))
        
        # Write metadata.json
        meta = {
            "title": "Paper OCR VLM Fine-Tuning Dataset",
            "scope": p_name,
            "period_id": period_id,
            "total_sheets": len(dataset_sharegpt),
            "total_entries": total_entries_count,
            "exported_images": exported_images_count,
            "target_model": "Qwen/Qwen2-VL-2B-Instruct",
            "quantization": "4-bit BitsAndBytes (NF4)",
            "adapter": "LoRA (PEFT)",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        zf.writestr("metadata.json", json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))
        
        # Write README.md
        readme_content = f"""# 🧠 Paper OCR Fine-Tuning Bundle
**ชุดข้อมูลฝึกฝนโมเดล AI สำหรับอ่านโพยกระดาษ (VLM Fine-Tuning)**
- งวดที่: {p_name}
- จำนวนใบโพย: {len(dataset_sharegpt)} แผ่น
- จำนวนรายการตัวเลข: {total_entries_count} รายการ
- รูปภาพที่รวบรวม: {exported_images_count} ภาพ

---

## 🚀 ขั้นตอนการนำไปเทรนบน Google Colab (ฟรี GPU T4):
1. เปิด **Google Colab** (https://colab.research.google.com)
2. อัปโหลดไฟล์สมุดงาน **`Paper_OCR_FineTune_Colab.ipynb`** เข้าไปใน Colab
3. ในหน้า Colab ด้านซ้าย ให้กดรูปโฟลเดอร์ (Files) แล้วลากไฟล์ zip ก้อนนี้ไปวาง
4. รันคำสั่งแตกไฟล์ใน Colab:
   ```bash
   !unzip -q paper_ocr_training_bundle.zip -d ./data
   ```
5. กด **Run All (Ctrl+F9)** ใน Colab
   - ระบบจะดาวน์โหลดโมเดล **Qwen2-VL-2B-Instruct** (2 พันล้านพารามิเตอร์ ขนาดกะทัดรัด)
   - ปรับแต่งโมเดลด้วยเทคนิค **4-bit QLoRA** บน GPU T4 ใช้เวลาประมาณ 10-15 นาที
   - เมื่อเสร็จสิ้น จะได้โฟลเดอร์ LoRA Adapter เพื่อนำกลับมาใส่ในระบบ OCR ของเราให้อ่านลายมือแม่นยำขึ้น 100%!

---

## 📁 โครงสร้างไฟล์ในชุดข้อมูลนี้:
- `images/`: ภาพถ่ายโพยกระดาษจริงจากกล้อง/มือถือ
- `dataset.jsonl`: ชุดข้อมูลมาตรฐาน ShareGPT VLM Format พร้อม prompt และคำตอบ JSON เฉลย
- `dataset_plain.json`: ชุดข้อมูลแบบข้อความอ่านง่าย
- `metadata.json`: ข้อมูลสรุปจำนวนแถวและวันที่สร้าง
- `Paper_OCR_FineTune_Colab.ipynb`: สมุดโค้ดพร้อมรันบน Colab ทันที
"""
        zf.writestr("README.md", readme_content.encode("utf-8"))
        
        # Also bundle the Colab notebook if available on disk
        colab_path = os.path.join(BASE_DIR, "Paper_OCR_FineTune_Colab.ipynb")
        if os.path.exists(colab_path):
            with open(colab_path, "rb") as nb_f:
                zf.writestr("Paper_OCR_FineTune_Colab.ipynb", nb_f.read())

    conn.close()
    zip_buffer.seek(0)
    return zip_buffer.getvalue()
