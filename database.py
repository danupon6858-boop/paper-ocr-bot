import sqlite3
import json
import csv
import io
import os
import hashlib
import time
import re
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from config import DB_PATH, OWNER_USER_ID, PRE_APPROVED_USERS

UPLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")


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

    # 6. Corrections (ประวัติการแก้ไขตัวเลข / AI Feedback Loop)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS corrections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id INTEGER,
        sheet_id TEXT,
        period_id INTEGER,
        worker_code TEXT,
        action_type TEXT, -- 'SINGLE_EDIT', 'DELETE', 'BULK_EDIT'
        old_set1 TEXT,
        new_set1 TEXT,
        old_set2 TEXT,
        new_set2 TEXT,
        category TEXT,
        note TEXT,
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
        ("content_hash", "TEXT"),
        ("image_path", "TEXT DEFAULT ''")
    ])
    ensure_columns("entries", [
        ("period_id", "INTEGER DEFAULT 1"),
        ("worker_code", "TEXT DEFAULT 'A'"),
        ("box_2d", "TEXT DEFAULT ''"),
        ("snippet_path", "TEXT DEFAULT ''"),
        ("uncertain_note", "TEXT DEFAULT ''")
    ])
    ensure_columns("pending_scans", [
        ("image_path", "TEXT DEFAULT ''"),
        ("first_pass_clean", "INTEGER DEFAULT 1"),
        ("edit_count", "INTEGER DEFAULT 0"),
        ("ocr_latency_ms", "INTEGER DEFAULT 0")
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

def save_uploaded_image(image_bytes: bytes, period_id: int, sheet_id: str) -> str:
    """Saves raw/processed image bytes to uploads/{period_id}/{sheet_id}_{timestamp}.jpg"""
    if not image_bytes:
        return ""
    try:
        p_dir = os.path.join(UPLOADS_DIR, str(period_id))
        os.makedirs(p_dir, exist_ok=True)
        safe_sheet_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', str(sheet_id))
        filename = f"{safe_sheet_id}_{int(time.time())}.jpg"
        filepath = os.path.join(p_dir, filename)
        with open(filepath, "wb") as f:
            f.write(image_bytes)
        return f"uploads/{period_id}/{filename}"
    except Exception as e:
        print(f"Error saving uploaded image: {e}")
        return ""

def create_pending_scan(user_id: str, worker_code: str, emp_name: str, period_id: int, sheet_id: str,
                        ocr_result: dict, val_result: dict, image_path: str = "", ocr_latency_ms: int = 0) -> int:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO pending_scans (user_id, worker_code, emp_name, period_id, sheet_id, ocr_json, val_json, status, image_path, first_pass_clean, edit_count, ocr_latency_ms)
    VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, 1, 0, ?)
    """, (
        user_id,
        worker_code,
        emp_name,
        period_id,
        sheet_id,
        json.dumps(ocr_result, ensure_ascii=False),
        json.dumps(val_result, ensure_ascii=False),
        image_path,
        ocr_latency_ms
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

def _strip_validation_keys(columns: dict) -> dict:
    """Strip validator-added keys (is_valid, errors) from column items before writing to ocr_json."""
    clean = {}
    for col_key, items in columns.items():
        clean[col_key] = [
            {k: v for k, v in itm.items() if k not in ("is_valid", "errors")}
            for itm in (items if isinstance(items, list) else [])
        ]
    return clean

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
    # Strip validator keys (is_valid, errors) before storing — keeps ocr_json clean
    ocr_data["columns"] = _strip_validation_keys(new_columns)
    
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
    image_path = scan.get("image_path") or ""
    
    content_hash = compute_sheet_content_hash(val_data.get("validated_columns", {}))
    status = "VERIFIED" if val_data.get("is_all_valid") else "NEEDS_REVIEW"
    
    header = ocr_data.get("header") if isinstance(ocr_data.get("header"), dict) else {}
    date_str = str(header.get("date") or "")
    total_amount = str(header.get("total_amount") or "")

    cursor.execute("""
    INSERT INTO sheets (period_id, sheet_id, employee_name, worker_code, date_str, total_amount, image_path, content_hash, raw_json, status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        period_id,
        sheet_id,
        emp_name,
        worker_code,
        date_str,
        total_amount,
        image_path,
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
            box_str = json.dumps(itm.get("box_2d", [])) if itm.get("box_2d") else ""
            snip_path = itm.get("snippet_path", "")
            unc_note = itm.get("uncertain_note", "")
            cursor.execute("""
            INSERT INTO entries (period_id, sheet_db_id, sheet_id, employee_name, worker_code, date_str, category, set1, set2, set3, raw_text, is_valid, validation_error, box_2d, snippet_path, uncertain_note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                "; ".join(itm.get("errors", [])),
                box_str,
                snip_path,
                unc_note
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

def is_sheet_id_taken(period_id: int, sheet_id: str) -> bool:
    """Checks whether a sheet_id is already used in confirmed sheets or active pending_scans for a period."""
    if not sheet_id:
        return False
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM sheets WHERE period_id = ? AND sheet_id = ?", (period_id, sheet_id))
    if cursor.fetchone():
        conn.close()
        return True
    cursor.execute("SELECT 1 FROM pending_scans WHERE period_id = ? AND sheet_id = ? AND status = 'PENDING'", (period_id, sheet_id))
    if cursor.fetchone():
        conn.close()
        return True
    conn.close()
    return False

def get_next_available_sheet_id(period_id: int, worker_code: str) -> str:
    """
    Finds the next sequential sheet ID for a given worker in a period.
    Scans both confirmed sheets and active pending_scans to avoid collisions.
    """
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Look at confirmed sheets
    cursor.execute("SELECT sheet_id FROM sheets WHERE period_id = ?", (period_id,))
    existing_sheets = [r["sheet_id"] for r in cursor.fetchall()]
    
    # 2. Look at pending scans
    cursor.execute("SELECT sheet_id FROM pending_scans WHERE period_id = ? AND status = 'PENDING'", (period_id,))
    pending_sheets = [r["sheet_id"] for r in cursor.fetchall()]
    
    conn.close()
    
    all_sids = existing_sheets + pending_sheets
    max_num = 0
    pattern = re.compile(rf"^{re.escape(worker_code)}-(\d+)$", re.IGNORECASE)
    
    for sid in all_sids:
        sid_str = str(sid or "").strip()
        m = pattern.match(sid_str)
        if m:
            try:
                num = int(m.group(1))
                if num > max_num:
                    max_num = num
            except ValueError:
                pass
        elif sid_str.isdigit() and worker_code == "A":
            try:
                num = int(sid_str)
                if num > max_num:
                    max_num = num
            except ValueError:
                pass
                
    return f"{worker_code}-{max_num + 1}"

def get_all_pending_scans(user_id: str) -> List[Dict]:
    """Returns all currently pending scans for a user ordered from oldest to newest."""
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT * FROM pending_scans
    WHERE user_id = ? AND status = 'PENDING'
    ORDER BY id ASC
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def confirm_all_pending_scans(user_id: str) -> List[Dict]:
    """Confirms all currently pending scans for a user. Returns list of confirmed sheet summaries."""
    scans = get_all_pending_scans(user_id)
    confirmed_list = []
    for s in scans:
        res = confirm_pending_scan(s["id"])
        if res:
            confirmed_list.append(res)
    return confirmed_list

def cancel_all_pending_scans(user_id: str) -> int:
    """Cancels all currently pending scans for a user. Returns number of canceled scans."""
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE pending_scans
    SET status = 'CANCELED'
    WHERE user_id = ? AND status = 'PENDING'
    """, (user_id,))
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count

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

def get_period_financials(period_id: Optional[int] = None) -> Dict:
    """
    Computes financial inflow statistics for a period:
    total inflow (sum of bet amounts in set2), category breakdown, and average per sheet.
    """
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if not period_id:
        p = get_active_period() or get_latest_period()
        period_id = p["id"] if p else 1
        
    cursor.execute("SELECT id, sheet_id, employee_name, total_amount FROM sheets WHERE period_id = ?", (period_id,))
    sheets = cursor.fetchall()
    total_sheets = len(sheets)
    
    cursor.execute("""
    SELECT category, set1, set2, is_valid
    FROM entries
    WHERE period_id = ? AND is_valid = 1
    """, (period_id,))
    entries = cursor.fetchall()
    conn.close()
    
    total_entries = len(entries)
    total_inflow = 0.0
    inflow_by_category = {"บน": 0.0, "ล่าง": 0.0, "บนล่าง": 0.0}
    count_by_category = {"บน": 0, "ล่าง": 0, "บนล่าง": 0}
    digits_2_vol = 0.0
    digits_2_cnt = 0
    digits_3_vol = 0.0
    digits_3_cnt = 0
    digits_other_vol = 0.0
    digits_other_cnt = 0
    num_map = {}
    
    for e in entries:
        cat = e["category"] or ""
        s1 = str(e["set1"] or "").strip()
        s1_clean = re.sub(r'\D', '', s1)
        s2 = str(e["set2"] or "").strip().lower()
        vol = 0.0
        try:
            if "x" in s2:
                parts = s2.split("x")
                vol = sum(float(re.sub(r'[^\d.]', '', p)) for p in parts if re.sub(r'[^\d.]', '', p))
            else:
                vol = float(re.sub(r'[^\d.]', '', s2)) if re.sub(r'[^\d.]', '', s2) else 0.0
        except Exception:
            vol = 0.0
            
        total_inflow += vol
        if cat in inflow_by_category:
            inflow_by_category[cat] += vol
            count_by_category[cat] = count_by_category.get(cat, 0) + 1
        else:
            inflow_by_category[cat] = inflow_by_category.get(cat, 0.0) + vol
            count_by_category[cat] = count_by_category.get(cat, 0) + 1

        if len(s1_clean) == 2:
            digits_2_vol += vol
            digits_2_cnt += 1
        elif len(s1_clean) == 3:
            digits_3_vol += vol
            digits_3_cnt += 1
        else:
            digits_other_vol += vol
            digits_other_cnt += 1

        if s1_clean:
            if s1_clean not in num_map:
                num_map[s1_clean] = {"number": s1_clean, "volume": 0.0, "count": 0, "cat": cat}
            num_map[s1_clean]["volume"] += vol
            num_map[s1_clean]["count"] += 1
            
    avg_per_sheet = (total_inflow / total_sheets) if total_sheets > 0 else 0.0
    unique_employees = len(set(s["employee_name"] for s in sheets if s["employee_name"]))

    top_numbers = sorted(num_map.values(), key=lambda x: (x["volume"], x["count"]), reverse=True)[:8]
    
    return {
        "period_id": period_id,
        "total_sheets": total_sheets,
        "total_employees": unique_employees,
        "total_entries": total_entries,
        "total_inflow": total_inflow,
        "inflow_by_category": inflow_by_category,
        "count_by_category": count_by_category,
        "digits_2_volume": digits_2_vol,
        "digits_2_count": digits_2_cnt,
        "digits_3_volume": digits_3_vol,
        "digits_3_count": digits_3_cnt,
        "digits_other_volume": digits_other_vol,
        "digits_other_count": digits_other_cnt,
        "top_numbers": top_numbers,
        "avg_per_sheet": avg_per_sheet
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

# ==================== CORRECTIONS & AI FEEDBACK ====================
def log_correction(scan_id: int, sheet_id: str, period_id: int, worker_code: str,
                   action_type: str, old_set1: str = "", new_set1: str = "",
                   old_set2: str = "", new_set2: str = "", category: str = "", note: str = ""):
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO corrections (scan_id, sheet_id, period_id, worker_code, action_type, old_set1, new_set1, old_set2, new_set2, category, note)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (scan_id, sheet_id, period_id, worker_code, action_type, old_set1, new_set1, old_set2, new_set2, category, note))
    
    # Mark scan as non-clean
    cursor.execute("""
    UPDATE pending_scans
    SET first_pass_clean = 0, edit_count = edit_count + 1
    WHERE id = ?
    """, (scan_id,))
    
    conn.commit()
    conn.close()

def classify_number_pattern(num_str: str) -> str:
    """
    Classifies a set1 digit string into patterns:
      - 'เลขเบิ้ล' (Twins/Doubles): e.g. 11, 22, 55, 99
      - 'เลขหาม' (Sandwich/Palindrome): e.g. 121, 505, 787
      - 'เลขตอง' (Triples): e.g. 111, 777, 999
      - 'เลขเรียง' (Sequential): e.g. 123, 456, 789
      - 'เลขทั่วไป' (Standard)
    """
    s = re.sub(r'\D', '', str(num_str))
    if not s:
        return "เลขทั่วไป"
        
    length = len(s)
    
    # All same digits (ตอง for 3, or all-same for 4)
    if len(set(s)) == 1 and length >= 3:
        return "เลขตอง"
        
    # Double (2 digits same, e.g. 11, 22, 99)
    if length == 2 and s[0] == s[1]:
        return "เลขเบิ้ล"
        
    # Sandwich / Palindrome for 3 digits (e.g. 121, 505)
    if length == 3 and s[0] == s[2] and s[0] != s[1]:
        return "เลขหาม"
        
    # Sequential (e.g. 123, 234, 345, 987, 876)
    if length in (3, 4):
        digits = [int(d) for d in s]
        diffs = [digits[i+1] - digits[i] for i in range(len(digits)-1)]
        if all(d == 1 for d in diffs) or all(d == -1 for d in diffs):
            return "เลขเรียง"
            
    # Check if contains double in 3-4 digits (e.g. 112, 255)
    if length == 3 and len(set(s)) == 2:
        return "เลขเบิ้ล"
        
    return "เลขทั่วไป"

def get_set1_analytics(period_id: Optional[int] = None) -> dict:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    query = """
    SELECT set1, set2, category
    FROM entries
    WHERE is_valid = 1
    """
    params = []
    if period_id is not None:
        query += " AND period_id = ?"
        params.append(period_id)
        
    cursor.execute(query, tuple(params))
    rows = cursor.fetchall()
    conn.close()
    
    total_entries = len(rows)
    length_counts = {"2": 0, "3": 0, "4": 0, "other": 0}
    pattern_counts = {"เลขเบิ้ล": 0, "เลขหาม": 0, "เลขตอง": 0, "เลขเรียง": 0, "เลขทั่วไป": 0}
    freq_map = {}
    
    for r in rows:
        s1 = r["set1"].strip()
        s2 = r["set2"].strip()
        cat = r["category"].strip()
        
        clean_s1 = re.sub(r'\D', '', s1)
        if not clean_s1:
            continue
            
        l_str = str(len(clean_s1))
        if l_str in length_counts:
            length_counts[l_str] += 1
        else:
            length_counts["other"] += 1
            
        pat = classify_number_pattern(clean_s1)
        pattern_counts[pat] = pattern_counts.get(pat, 0) + 1
        
        if clean_s1 not in freq_map:
            freq_map[clean_s1] = {"set1": clean_s1, "count": 0, "top": 0, "bottom": 0, "top_bottom": 0, "volume": 0.0}
            
        freq_map[clean_s1]["count"] += 1
        if cat == "บน":
            freq_map[clean_s1]["top"] += 1
        elif cat == "ล่าง":
            freq_map[clean_s1]["bottom"] += 1
        elif cat == "บนล่าง":
            freq_map[clean_s1]["top_bottom"] += 1
            
        try:
            if "x" in s2.lower():
                parts = s2.lower().split("x")
                vol = sum(float(re.sub(r'[^\d.]', '', p)) for p in parts if re.sub(r'[^\d.]', '', p))
            else:
                vol = float(re.sub(r'[^\d.]', '', s2)) if re.sub(r'[^\d.]', '', s2) else 0.0
            freq_map[clean_s1]["volume"] += vol
        except Exception:
            pass
            
    top_by_freq = sorted(freq_map.values(), key=lambda x: x["count"], reverse=True)[:20]
    top_by_volume = sorted(freq_map.values(), key=lambda x: x["volume"], reverse=True)[:10]
    
    return {
        "total_entries": total_entries,
        "length_counts": length_counts,
        "pattern_counts": pattern_counts,
        "top_by_freq": top_by_freq,
        "top_by_volume": top_by_volume
    }

def get_ai_feedback_metrics(period_id: Optional[int] = None) -> dict:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    p_filter = "WHERE period_id = ?" if period_id else ""
    p_params = (period_id,) if period_id else ()
    
    cursor.execute(f"""
    SELECT count(*) as total, sum(first_pass_clean) as clean, avg(ocr_latency_ms) as avg_latency
    FROM pending_scans
    {p_filter}
    """, p_params)
    scan_row = cursor.fetchone()
    total_scans = scan_row["total"] or 0
    clean_scans = scan_row["clean"] or 0
    avg_latency = scan_row["avg_latency"] or 0
    clean_rate = round((clean_scans / total_scans * 100), 1) if total_scans > 0 else 100.0
    
    c_query = "SELECT * FROM corrections"
    if period_id:
        c_query += " WHERE period_id = ?"
    c_query += " ORDER BY id DESC LIMIT 50"
    cursor.execute(c_query, p_params)
    corrections = [dict(r) for r in cursor.fetchall()]
    
    misread_counts = {}
    for c in corrections:
        o = c.get("old_set1", "").strip()
        n = c.get("new_set1", "").strip()
        if o and n and o != n:
            pair = f"{o} ➔ {n}"
            misread_counts[pair] = misread_counts.get(pair, 0) + 1
            
    top_misread = sorted(misread_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    
    conn.close()
    return {
        "total_scans": total_scans,
        "clean_scans": clean_scans,
        "clean_rate": clean_rate,
        "avg_latency_s": round(avg_latency / 1000.0, 1) if avg_latency else 0.0,
        "recent_corrections": corrections,
        "top_misread": top_misread
    }

def get_staff_and_peak_metrics(period_id: Optional[int] = None) -> dict:
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    filter_clause = "WHERE period_id = ?" if period_id else ""
    params = (period_id,) if period_id else ()
    
    cursor.execute(f"""
    SELECT strftime('%H', created_at) as hour, count(*) as count
    FROM sheets
    {filter_clause}
    GROUP BY hour
    ORDER BY hour ASC
    """, params)
    hourly = {row["hour"]: row["count"] for row in cursor.fetchall()}
    
    cursor.execute(f"""
    SELECT worker_code, employee_name, count(*) as total_sheets
    FROM sheets
    {filter_clause}
    GROUP BY worker_code
    ORDER BY total_sheets DESC
    """, params)
    workers = [dict(r) for r in cursor.fetchall()]
    
    conn.close()
    return {
        "hourly": hourly,
        "workers": workers
    }

# ==================== DEEP SEARCH ====================
def deep_search_set1(num_query: str, period_id: Optional[int] = None) -> dict:
    """
    Searches for exact matches of set1 across entries joined with sheets.
    Returns:
      - query: cleaned search string
      - total_count: number of matches
      - total_volume: sum of set2 amounts
      - cat_breakdown: {'บน': count, 'ล่าง': count, 'บนล่าง': count}
      - matches: list of entry details with image_path
    """
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    clean_q = re.sub(r'[^\d?]', '', str(num_query)).strip()
    if not clean_q:
        conn.close()
        return {"query": num_query, "total_count": 0, "total_volume": 0.0, "cat_breakdown": {}, "matches": []}
        
    query_sql = """
    SELECT e.id, e.period_id, e.sheet_id, e.employee_name, e.worker_code,
           e.category, e.set1, e.set2, e.set3, e.raw_text, s.created_at,
           s.image_path, p.name as period_name
    FROM entries e
    LEFT JOIN sheets s ON e.sheet_db_id = s.id
    LEFT JOIN periods p ON e.period_id = p.id
    WHERE e.set1 = ?
    """
    params = [clean_q]
    if period_id is not None:
        query_sql += " AND e.period_id = ?"
        params.append(period_id)
        
    query_sql += " ORDER BY e.id DESC LIMIT 100"
    
    cursor.execute(query_sql, tuple(params))
    rows = cursor.fetchall()
    conn.close()
    
    cat_counts = {"บน": 0, "ล่าง": 0, "บนล่าง": 0}
    total_volume = 0.0
    matches = []
    
    for r in rows:
        cat = r["category"]
        if cat in cat_counts:
            cat_counts[cat] += 1
            
        s2 = r["set2"] or ""
        try:
            if "x" in s2.lower():
                parts = s2.lower().split("x")
                vol = sum(float(re.sub(r'[^\d.]', '', p)) for p in parts if re.sub(r'[^\d.]', '', p))
            else:
                vol = float(re.sub(r'[^\d.]', '', s2)) if re.sub(r'[^\d.]', '', s2) else 0.0
            total_volume += vol
        except Exception:
            pass
            
        matches.append({
            "id": r["id"],
            "period_name": r["period_name"] or f"งวด {r['period_id']}",
            "sheet_id": r["sheet_id"],
            "employee_name": r["employee_name"],
            "worker_code": r["worker_code"] or "A",
            "category": cat,
            "set1": r["set1"],
            "set2": r["set2"],
            "set3": r["set3"],
            "raw_text": r["raw_text"],
            "image_path": r["image_path"] or "",
            "created_at": r["created_at"]
        })
        
    return {
        "query": clean_q,
        "total_count": len(matches),
        "total_volume": total_volume,
        "cat_breakdown": cat_counts,
        "matches": matches
    }


