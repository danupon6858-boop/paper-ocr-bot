from typing import List, Dict
import database

def format_search_results(numbers: List[str]) -> str:
    """Format the query results for Set 1 search in human-friendly Thai"""
    results = database.search_set1(numbers)
    if not results:
        return "⚠️ กรุณาระบุตัวเลขที่ต้องการค้นหา เช่น 'เช็ค 310' หรือ 'เช็ค 401 370'"
        
    lines = []
    lines.append(f"🔍 ผลการค้นหาชุดที่ 1 ({', '.join(numbers)})")
    lines.append("─────────────────────────")
    
    found_any = False
    for num, matches in results.items():
        if matches:
            found_any = True
            lines.append(f"📌 เลข {num} (พบใน {len(matches)} จุด):")
            for m in matches:
                sheet = m['sheet_id'] or 'ไม่ระบุใบที่'
                emp = m['employee_name'] or 'ไม่ระบุชื่อ'
                date = m['date_str'] or 'ไม่ระบุวันที่'
                cat = m['category']
                set3 = f" {m['set3']}" if m['set3'] else ""
                val = f"{m['set1']} = {set3}{m['set2']}"
                lines.append(f"  • ใบที่ {sheet} | หมวด '{cat}' | รายการ: {m['raw_text']}")
                lines.append(f"    (ผู้บันทึก: {emp}, วันที่: {date})")
        else:
            lines.append(f"❌ เลข {num}: ไม่พบในระบบ")
        lines.append("")
        
    return "\n".join(lines).strip()

def format_daily_status(date_str: str = None) -> str:
    summary = database.get_daily_summary(date_str)
    lines = [
        "📊 รายงานสถานะการบันทึกข้อมูล",
        "─────────────────────────",
        f"📄 เอกสารทั้งหมดในระบบ: {summary['total_sheets']} แผ่น",
        f"📝 รายการข้อมูลทั้งหมด: {summary['total_entries']} รายการ",
        f"✅ รายการที่ถูกต้อง: {summary['valid_entries']} รายการ"
    ]
    invalid_entries = summary['total_entries'] - summary['valid_entries']
    if invalid_entries > 0:
        lines.append(f"⚠️ รายการที่ต้องตรวจสอบ: {invalid_entries} รายการ")
        
    lines.append("─────────────────────────")
    if summary['sheets']:
        lines.append("📋 รายชื่อใบที่เข้าระบบแล้ว:")
        for s in summary['sheets']:
            status_icon = "✅" if s['status'] == "VERIFIED" else "⚠️"
            sheet_id = s['sheet_id'] or 'ไม่ระบุ'
            emp = s['employee_name'] or '-'
            lines.append(f"  {status_icon} ใบที่: {sheet_id} (ผู้ส่ง: {emp})")
            
    return "\n".join(lines)
