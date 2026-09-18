import sqlite3
import json
import csv
import io
import os
import hashlib
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from config import DB_PATH, OWNER_USER_ID, PRE_APPROVED_USERS


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Periods (งวด)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS periods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        status TEXT, -- 'OPEN', 'CLOSED'
        opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        closed_at TIMESTAMP
    )
    """)
    
    # Check if there is at least one period, if not create default open period
    cursor.execute("SELECT count(*) as count FROM periods")
    if cursor.fetchone()["count"] == 0:
        cursor.execute("""
        INSERT INTO periods (name, status)
        VALUES ('งวดวันที่ 16 กันยายน 2569', 'OPEN')
        """)

    # 2. Sheets
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sheets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        period_id INTEGER,
        sheet_id TEXT,
        employee_name TEXT,
        worker_code TEXT,
        date_str TEXT,
        total_amount TEXT,
        image_path TEXT,
        content_hash TEXT,
        raw_json TEXT,
        status TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (period_id) REFERENCES periods (id)
    )
    """)
    
    # 3. Entries
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        period_id INTEGER,
        sheet_db_id INTEGER,
        sheet_id TEXT,
        employee_name TEXT,
        worker_code TEXT,
        date_str TEXT,
        category TEXT,
        set1 TEXT,
        set2 TEXT,
        set3 TEXT,
        raw_text TEXT,
        is_valid INTEGER,
        validation_error TEXT,
        FOREIGN KEY (period_id) REFERENCES periods (id),
        FOREIGN KEY (sheet_db_id) REFERENCES sheets (id)
    )
    """)
    
    # 4. Pending Scans (รอกดยืนยัน / ขอแก้ไข)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS pending_scans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        worker_code TEXT,
        emp_name TEXT,
        period_id INTEGER,
        sheet_id TEXT,
        ocr_json TEXT,
        val_json TEXT,
        status TEXT, -- 'PENDING', 'CONFIRMED', 'CANCELED'
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 5. Authorized Users (Security Whitelist)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        display_name TEXT,
        role TEXT,
        worker_code TEXT,
        status TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # Safe column migrations for existing database files
    def ensure_columns(table_name, col_defs):
        cursor.execute(f"PRAGMA table_info({table_name})")
        existing_cols = {row["name"] for row in cursor.fetchall()}
        for col_name, col_type in col_defs:
            if col_name not in existing_cols:
                try:
                    cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}")
                except Exception as e:
                    print(f"Migration notice ({table_name}.{col_name}): {e}")

    ensure_columns("sheets", [
        ("period_id", "INTEGER DEFAULT 1"),
        ("worker_code", "TEXT DEFAULT 'A'"),
        ("content_hash", "TEXT")
    ])
    ensure_columns("entries", [
        ("period_id", "INTEGER DEFAULT 1"),
        ("worker_code", "TEXT DEFAULT 'A'")
    ])

    cursor.execute("""
    INSERT OR IGNORE INTO users (user_id, display_name, role, worker_code, status)
    VALUES (?, 'เจ้าของระบบ (Owner)', 'owner', 'ADMIN', 'APPROVED')
    """, (OWNER_USER_ID,))

    # Seed pre-approved users from PRE_APPROVED_USERS env var (survives redeploys)
    for i, pre_uid in enumerate(PRE_APPROVED_USERS):
        if not pre_uid:
            continue
        # Assign letter codes A, B, C... automatically
        letter = chr(ord('A') + i) if i < 26 else 'Z'
        cursor.execute("""
        INSERT OR IGNORE INTO users (user_id, display_name, role, worker_code, status)
        VALUES (?, ?, 'worker', ?, 'APPROVED')
        """, (pre_uid, f"พนักงาน {letter}", letter))
        # If user already exists but is PENDING, upgrade to APPROVED
        cursor.execute("""
        UPDATE users SET status = 'APPROVED', worker_code = ?
        WHERE user_id = ? AND status = 'PENDING'
        """, (letter, pre_uid))

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_entries_set1 ON entries (set1)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_entries_period ON entries (period_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sheets_period ON sheets (period_id)")
    
    conn.commit()
    conn.close()

# ==================== PERIOD MANAGEMENT ====================
def get_active_period() -> Optional[Dict]:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM periods WHERE status = 'OPEN' ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_latest_period() -> Optional[Dict]:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM periods ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_all_periods() -> List[Dict]:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM periods ORDER BY id DESC")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def open_new_period(name: Optional[str] = None) -> Dict:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE periods SET status = 'CLOSED', closed_at = CURRENT_TIMESTAMP WHERE status = 'OPEN'")
    today_str = datetime.now().strftime("%d/%m/%Y")
    period_name = name.strip() if name else f"งวดของวันที่ {today_str}"
    cursor.execute("INSERT INTO periods (name, status, opened_at) VALUES (?, 'OPEN', CURRENT_TIMESTAMP)", (period_name,))
    new_id = cursor.lastrowid
    conn.commit()
    cursor.execute("SELECT * FROM periods WHERE id = ?", (new_id,))
    p = dict(cursor.fetchone())
    conn.close()
    return p

def close_active_period() -> Optional[Dict]:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    active = get_active_period()
    if not active:
        conn.close()
        return None
    cursor.execute("UPDATE periods SET status = 'CLOSED', closed_at = CURRENT_TIMESTAMP WHERE id = ?", (active["id"],))
    conn.commit()
    cursor.execute("SELECT * FROM periods WHERE id = ?", (active["id"],))
    p = dict(cursor.fetchone())
    conn.close()
    return p

def reopen_latest_period() -> Optional[Dict]:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    latest = get_latest_period()
    if not latest:
        conn.close()
        return None
    cursor.execute("UPDATE periods SET status = 'OPEN', closed_at = NULL WHERE id = ?", (latest["id"],))
    conn.commit()
    cursor.execute("SELECT * FROM periods WHERE id = ?", (latest["id"],))
    p = dict(cursor.fetchone())
    conn.close()
    return p

# ==================== DUPLICATE SHEET DETECTION ====================
def compute_sheet_content_hash(columns_data: dict) -> str:
    """Calculates a deterministic signature based on all entries in a sheet"""
    all_items = []
    for col in ["top", "bottom", "top_bottom"]:
        items = columns_data.get(col, [])
        for itm in items:
            s1 = itm.get("set1", "").strip()
            s2 = itm.get("set2", "").strip().lower()
            s3 = itm.get("set3", "").strip()
            all_items.append(f"{col}:{s1}:{s3}:{s2}")
    if not all_items:
        return ""
    all_items.sort()
    return hashlib.md5("|".join(all_items).encode("utf-8")).hexdigest()

def check_duplicate_sheet(period_id: int, sheet_id: str, columns_data: dict) -> Tuple[bool, str]:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Check exact sheet_id match in this period
    if sheet_id and sheet_id != "N/A" and not sheet_id.endswith("-N/A"):
        cursor.execute("SELECT sheet_id, employee_name, created_at FROM sheets WHERE period_id = ? AND sheet_id = ?", (period_id, sheet_id))
        row = cursor.fetchone()
        if row:
            conn.close()
            return True, f"ตรวจพบ 'ใบที่ {sheet_id}' เคยบันทึกไปแล้ว (โดย {row['employee_name']})"

    # 2. Check content signature match
    content_hash = compute_sheet_content_hash(columns_data)
    if content_hash:
        cursor.execute("SELECT sheet_id, employee_name, created_at FROM sheets WHERE period_id = ? AND content_hash = ?", (period_id, content_hash))
        row = cursor.fetchone()
        if row:
            conn.close()
            return True, f"ตรวจพบรายการตัวเลขในแผ่นนี้ ตรงกับ 'ใบที่ {row['sheet_id']}' ถึง 100% (อาจเป็นรูปถ่ายซ้ำ)"

    conn.close()
    return False, ""

# ==================== PENDING SCAN LIFECYCLE ====================
def create_pending_scan(user_id: str, worker_code: str, emp_name: str, period_id: int, sheet_id: str, ocr_result: dict, val_result: dict) -> int:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO pending_scans (user_id, worker_code, emp_name, period_id, sheet_id, ocr_json, val_json, status)
    VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING')
    """, (
        user_id,
        worker_code,
        emp_name,
        period_id,
        sheet_id,
        json.dumps(ocr_result, ensure_ascii=False),
        json.dumps(val_result, ensure_ascii=False)
    ))
    scan_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return scan_id

def get_pending_scan(scan_id: int) -> Optional[Dict]:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM pending_scans WHERE id = ?", (scan_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_latest_pending_scan(user_id: str) -> Optional[Dict]:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT * FROM pending_scans
    WHERE user_id = ? AND status = 'PENDING'
    ORDER BY id DESC LIMIT 1
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def update_pending_scan_items(scan_id: int, new_columns: dict) -> bool:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT ocr_json FROM pending_scans WHERE id = ?", (scan_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
        
    ocr_data = json.loads(row["ocr_json"])
    ocr_data["columns"] = new_columns
    
    from validator import OCRValidator
    val_data = OCRValidator.validate_document(ocr_data)
    
    cursor.execute("""
    UPDATE pending_scans
    SET ocr_json = ?, val_json = ?
    WHERE id = ?
    """, (json.dumps(ocr_data, ensure_ascii=False), json.dumps(val_data, ensure_ascii=False), scan_id))
    conn.commit()
    conn.close()
    return True

def confirm_pending_scan(scan_id: int) -> Optional[Dict]:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM pending_scans WHERE id = ? AND status = 'PENDING'", (scan_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
        
    scan = dict(row)
    ocr_data = json.loads(scan["ocr_json"])
    val_data = json.loads(scan["val_json"])
    period_id = scan["period_id"]
    worker_code = scan["worker_code"]
    emp_name = scan["emp_name"]
    sheet_id = scan["sheet_id"]
    
    content_hash = compute_sheet_content_hash(val_data.get("validated_columns", {}))
    status = "VERIFIED" if val_data.get("is_all_valid") else "NEEDS_REVIEW"
    
    header = ocr_data.get("header") if isinstance(ocr_data.get("header"), dict) else {}
    date_str = str(header.get("date") or "")
    total_amount = str(header.get("total_amount") or "")

    cursor.execute("""
    INSERT INTO sheets (period_id, sheet_id, employee_name, worker_code, date_str, total_amount, image_path, content_hash, raw_json, status)
    VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?)
    """, (
        period_id,
        sheet_id,
        emp_name,
        worker_code,
        date_str,
        total_amount,
        content_hash,
        json.dumps(ocr_data, ensure_ascii=False),
        status
    ))
    sheet_db_id = cursor.lastrowid
    
    columns_map = [("top", "บน"), ("bottom", "ล่าง"), ("top_bottom", "บนล่าง")]
    val_cols = val_data.get("validated_columns", {})
    
    for col_key, col_label in columns_map:
        items = val_cols.get(col_key, [])
        for itm in items:
            cursor.execute("""
            INSERT INTO entries (period_id, sheet_db_id, sheet_id, employee_name, worker_code, date_str, category, set1, set2, set3, raw_text, is_valid, validation_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                period_id,
                sheet_db_id,
                sheet_id,
                emp_name,
                worker_code,
                date_str,
                col_label,
                itm.get("set1", ""),
                itm.get("set2", ""),
                itm.get("set3", ""),
                itm.get("raw_text", ""),
                1 if itm.get("is_valid") else 0,
                "; ".join(itm.get("errors", []))
            ))
            
    cursor.execute("UPDATE pending_scans SET status = 'CONFIRMED' WHERE id = ?", (scan_id,))
    conn.commit()
    conn.close()
    return {"sheet_id": sheet_id, "emp_name": emp_name, "period_id": period_id}

def cancel_pending_scan(scan_id: int) -> bool:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE pending_scans SET status = 'CANCELED' WHERE id = ?", (scan_id,))
    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return success

# ==================== AUDIT REPORT & SEQUENCE CHECK ====================
def run_audit_report(period_id: Optional[int] = None) -> Dict:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if not period_id:
        p = get_active_period() or get_latest_period()
        period_id = p["id"] if p else 1
        
    cursor.execute("SELECT sheet_id, worker_code, employee_name, status FROM sheets WHERE period_id = ? ORDER BY id ASC", (period_id,))
    saved_sheets = cursor.fetchall()
    
    cursor.execute("SELECT count(*) as total, sum(is_valid) as valids FROM entries WHERE period_id = ?", (period_id,))
    entry_stats = cursor.fetchone()
    
    # Check pending scans
    cursor.execute("SELECT count(*) as count FROM pending_scans WHERE period_id = ? AND status = 'PENDING'", (period_id,))
    pending_count = cursor.fetchone()["count"]
    
    conn.close()
    
    # Check sequence per worker code
    worker_sheets = {}
    for s in saved_sheets:
        code = s["worker_code"] or "A"
        sid = s["sheet_id"]
        # extract integer if sheet_id is e.g. "A-12" or "12"
        import re
        nums = re.findall(r"\d+", sid)
        if nums:
            num = int(nums[-1])
            worker_sheets.setdefault(code, []).append(num)
            
    sequence_report = {}
    for code, num_list in worker_sheets.items():
        if not num_list:
            continue
        min_n = min(num_list)
        max_n = max(num_list)
        full_set = set(range(1, max_n + 1))
        actual_set = set(num_list)
        missing = sorted(list(full_set - actual_set))
        sequence_report[code] = {
            "submitted_count": len(num_list),
            "max_sheet": max_n,
            "missing_sheets": missing,
            "is_complete": len(missing) == 0
        }
        
    return {
        "period_id": period_id,
        "total_sheets": len(saved_sheets),
        "total_entries": entry_stats["total"] or 0,
        "valid_entries": entry_stats["valids"] or 0,
        "pending_scans": pending_count,
        "worker_sequences": sequence_report
    }

# ==================== USER MANAGEMENT ====================
def get_user(user_id: str) -> Optional[Dict]:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def register_pending_user(user_id: str, display_name: str) -> bool:
    """Register a new user as APPROVED immediately — no approval queue needed."""
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        next_code = get_next_worker_code()
        cursor.execute("""
        INSERT INTO users (user_id, display_name, role, worker_code, status)
        VALUES (?, ?, 'worker', ?, 'APPROVED')
        """, (user_id, display_name, next_code))
        conn.commit()
        is_new = True
    except sqlite3.IntegrityError:
        is_new = False
    conn.close()
    return is_new


def get_next_worker_code() -> str:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT worker_code FROM users WHERE status = 'APPROVED' AND role != 'owner'")
    taken = {row["worker_code"] for row in cursor.fetchall() if row["worker_code"]}
    conn.close()
    for char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if char not in taken:
            return char
    return "A"

def get_pending_users() -> List[Dict]:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE status = 'PENDING' ORDER BY created_at DESC")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def set_owner(user_id: str, display_name: str = "เจ้าของระบบ") -> dict:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row:
        cursor.execute("UPDATE users SET role = 'owner', status = 'APPROVED', display_name = ? WHERE user_id = ?", (display_name, user_id))
    else:
        cursor.execute("INSERT INTO users (user_id, display_name, role, worker_code, status) VALUES (?, ?, 'owner', 'ADMIN', 'APPROVED')",
                       (user_id, display_name))
    conn.commit()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    res = dict(cursor.fetchone())
    conn.close()
    return res

def approve_user(identifier: str, worker_code: str) -> Optional[Dict]:
    """Approves user by matching user_id, display_name, or pending queue."""
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    clean_id = (identifier or "").strip()
    if clean_id.lower().startswith("id:"):
        clean_id = clean_id[3:].strip()
        
    row = None
    if clean_id:
        # 1. Match exact user_id or prefix
        cursor.execute("SELECT * FROM users WHERE user_id = ? OR user_id LIKE ?", (clean_id, f"{clean_id}%"))
        row = cursor.fetchone()
        
        # 2. Match display_name (case-insensitive / partial)
        if not row:
            cursor.execute("SELECT * FROM users WHERE LOWER(display_name) = LOWER(?) OR display_name LIKE ?", (clean_id, f"%{clean_id}%"))
            row = cursor.fetchone()
            
    # 3. If still not found or identifier is empty/latest, pick the latest PENDING user
    if not row:
        cursor.execute("SELECT * FROM users WHERE status = 'PENDING' ORDER BY created_at DESC LIMIT 1")
        row = cursor.fetchone()
        
    if not row:
        conn.close()
        return None
        
    u_id = row["user_id"]
    cursor.execute("""
    UPDATE users SET status = 'APPROVED', worker_code = ? WHERE user_id = ?
    """, (worker_code.upper(), u_id))
    conn.commit()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (u_id,))
    updated = dict(cursor.fetchone())
    conn.close()
    return updated

def block_user(identifier: str) -> Optional[Dict]:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    clean_id = (identifier or "").strip()
    if clean_id.lower().startswith("id:"):
        clean_id = clean_id[3:].strip()
        
    row = None
    if clean_id:
        cursor.execute("SELECT * FROM users WHERE user_id = ? OR user_id LIKE ?", (clean_id, f"{clean_id}%"))
        row = cursor.fetchone()
        if not row:
            cursor.execute("SELECT * FROM users WHERE LOWER(display_name) = LOWER(?) OR display_name LIKE ?", (clean_id, f"%{clean_id}%"))
            row = cursor.fetchone()
            
    if not row:
        cursor.execute("SELECT * FROM users WHERE status = 'PENDING' ORDER BY created_at DESC LIMIT 1")
        row = cursor.fetchone()
        
    if not row:
        conn.close()
        return None
        
    u_id = row["user_id"]
    cursor.execute("UPDATE users SET status = 'BLOCKED' WHERE user_id = ?", (u_id,))
    conn.commit()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (u_id,))
    updated = dict(cursor.fetchone())
    conn.close()
    return updated

# ==================== SEARCH & EXPORT ====================
def search_set1(numbers: List[str], period_id: Optional[int] = None) -> Dict[str, List[Dict]]:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    results = {}
    
    if not period_id:
        p = get_active_period() or get_latest_period()
        period_id = p["id"] if p else 1
        
    for num in numbers:
        clean_num = num.strip()
        if not clean_num:
            continue
        cursor.execute("""
        SELECT e.sheet_id, e.employee_name, e.worker_code, e.date_str, e.category, e.set1, e.set2, e.set3, e.raw_text, s.id as sheet_db_id
        FROM entries e
        JOIN sheets s ON e.sheet_db_id = s.id
        WHERE e.set1 = ? AND e.period_id = ?
        ORDER BY s.id DESC
        """, (clean_num, period_id))
        rows = [dict(r) for r in cursor.fetchall()]
        results[clean_num] = rows
        
    conn.close()
    return results

def get_daily_summary(period_id: Optional[int] = None) -> Dict:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if not period_id:
        p = get_active_period() or get_latest_period()
        period_id = p["id"] if p else 1
        
    cursor.execute("SELECT * FROM sheets WHERE period_id = ? ORDER BY id DESC", (period_id,))
    sheets = [dict(r) for r in cursor.fetchall()]
    
    cursor.execute("""
    SELECT count(*) as total_entries, sum(is_valid) as valid_entries
    FROM entries WHERE period_id = ?
    """, (period_id,))
    stats = cursor.fetchone()
    
    conn.close()
    return {
        "total_sheets": len(sheets),
        "sheets": sheets,
        "total_entries": stats["total_entries"] if stats else 0,
        "valid_entries": stats["valid_entries"] if stats else 0
    }

def export_csv(period_id: Optional[int] = None, output_path: Optional[str] = None) -> str:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if not period_id:
        p = get_active_period() or get_latest_period()
        period_id = p["id"] if p else 1
        
    cursor.execute("""
    SELECT category, set1, set3, set2
    FROM entries
    WHERE period_id = ?
    ORDER BY sheet_db_id ASC, id ASC
    """, (period_id,))
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
        filename = f"report_period_{period_id}.csv"
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
