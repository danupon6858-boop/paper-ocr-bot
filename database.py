import sqlite3
import json
import csv
import io
import os
from typing import List, Dict, Optional, Tuple
from config import DB_PATH

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Table for Sheets (1 paper sheet = 1 row)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sheets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sheet_id TEXT,
        employee_name TEXT,
        date_str TEXT,
        total_amount TEXT,
        image_path TEXT,
        raw_json TEXT,
        status TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # Table for Entries (each line item in บน / ล่าง / บนล่าง)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sheet_db_id INTEGER,
        sheet_id TEXT,
        employee_name TEXT,
        date_str TEXT,
        category TEXT,
        set1 TEXT,
        set2 TEXT,
        set3 TEXT,
        raw_text TEXT,
        is_valid INTEGER,
        validation_error TEXT,
        FOREIGN KEY (sheet_db_id) REFERENCES sheets (id)
    )
    """)
    
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_entries_set1 ON entries (set1)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sheets_date ON sheets (date_str)")
    
    conn.commit()
    conn.close()

def save_document(ocr_data: Dict, val_data: Dict, image_path: str = "") -> int:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    header = ocr_data.get("header", {})
    sheet_id = header.get("sheet_id", "").strip() or "N/A"
    employee_name = header.get("customer_name", "").strip()
    date_str = header.get("date", "").strip()
    total_amount = header.get("total_amount", "").strip()
    
    status = "VERIFIED" if val_data.get("is_all_valid") else "NEEDS_REVIEW"
    
    cursor.execute("""
    INSERT INTO sheets (sheet_id, employee_name, date_str, total_amount, image_path, raw_json, status)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        sheet_id,
        employee_name,
        date_str,
        total_amount,
        image_path,
        json.dumps(ocr_data, ensure_ascii=False),
        status
    ))
    sheet_db_id = cursor.lastrowid
    
    columns_map = [("top", "บน"), ("bottom", "ล่าง"), ("top_bottom", "บนล่าง")]
    val_cols = val_data.get("validated_columns", {})
    
    for col_key, col_label in columns_map:
        items = val_cols.get(col_key, [])
        for itm in items:
            set1 = itm.get("set1", "")
            set2 = itm.get("set2", "")
            set3 = itm.get("set3", "")
            raw_text = itm.get("raw_text", "")
            is_valid = 1 if itm.get("is_valid") else 0
            err_msg = "; ".join(itm.get("errors", []))
            
            cursor.execute("""
            INSERT INTO entries (sheet_db_id, sheet_id, employee_name, date_str, category, set1, set2, set3, raw_text, is_valid, validation_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sheet_db_id,
                sheet_id,
                employee_name,
                date_str,
                col_label,
                set1,
                set2,
                set3,
                raw_text,
                is_valid,
                err_msg
            ))
            
    conn.commit()
    conn.close()
    return sheet_db_id

def search_set1(numbers: List[str]) -> Dict[str, List[Dict]]:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    results = {}
    
    for num in numbers:
        clean_num = num.strip()
        if not clean_num:
            continue
        cursor.execute("""
        SELECT e.sheet_id, e.employee_name, e.date_str, e.category, e.set1, e.set2, e.set3, e.raw_text, s.id as sheet_db_id
        FROM entries e
        JOIN sheets s ON e.sheet_db_id = s.id
        WHERE e.set1 = ?
        ORDER BY s.id DESC
        """, (clean_num,))
        rows = [dict(r) for r in cursor.fetchall()]
        results[clean_num] = rows
        
    conn.close()
    return results

def get_daily_summary(date_str: Optional[str] = None) -> Dict:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if date_str:
        cursor.execute("SELECT * FROM sheets WHERE date_str = ? ORDER BY id ASC", (date_str,))
    else:
        cursor.execute("SELECT * FROM sheets ORDER BY id DESC")
    sheets = [dict(r) for r in cursor.fetchall()]
    
    cursor.execute("""
    SELECT count(*) as total_entries, sum(is_valid) as valid_entries
    FROM entries
    """)
    stats = cursor.fetchone()
    
    conn.close()
    return {
        "total_sheets": len(sheets),
        "sheets": sheets,
        "total_entries": stats["total_entries"] if stats else 0,
        "valid_entries": stats["valid_entries"] if stats else 0
    }

def export_csv(date_str: Optional[str] = None, output_path: Optional[str] = None) -> str:
    """
    Exports in 'Option 1' vertical format:
    Headers: ลำดับ | บน | ล่าง | บนล่าง
    Items ordered vertically row by row.
    """
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    query = """
    SELECT category, set1, set3, set2, raw_text
    FROM entries
    """
    params = []
    if date_str:
        query += " WHERE date_str = ?"
        params.append(date_str)
        
    query += " ORDER BY sheet_db_id ASC, id ASC"
    cursor.execute(query, params)
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
        s3_part = f"{s3} " if s3 else ""
        item_str = f"{s1} = {s3_part}{s2}".strip()
        
        if cat == "บน":
            top_items.append(item_str)
        elif cat == "ล่าง":
            bot_items.append(item_str)
        elif cat == "บนล่าง":
            topbot_items.append(item_str)
            
    max_len = max(len(top_items), len(bot_items), len(topbot_items), 1)
    
    if not output_path:
        filename = f"report_vertical_{date_str.replace('/', '-') if date_str else 'all'}.csv"
        output_path = os.path.join(os.path.dirname(__file__), filename)
        
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["ลำดับ", "บน", "ล่าง", "บนล่าง"])
        for i in range(max_len):
            row_num = i + 1
            top_val = top_items[i] if i < len(top_items) else ""
            bot_val = bot_items[i] if i < len(bot_items) else ""
            topbot_val = topbot_items[i] if i < len(topbot_items) else ""
            writer.writerow([row_num, top_val, bot_val, topbot_val])
            
    return output_path
