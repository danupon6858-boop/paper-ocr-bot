from typing import List, Dict
import database

def format_search_results(numbers: List[str], period_id: int = None) -> str:
    active = database.get_active_period() or database.get_latest_period()
    p_name = active["name"] if active else "งวดปัจจุบัน"
    results = database.search_set1(numbers, period_id)
    if not results:
        return "⚠️ กรุณาระบุตัวเลขที่ต้องการค้นหา เช่น 'เช็ค 310' หรือ 'เช็ค 401 370'"
        
    lines = []
    lines.append(f"🔍 ผลการค้นหาชุดที่ 1 ({', '.join(numbers)})")
    lines.append(f"📌 [{p_name}]")
    lines.append("─────────────────────────")
    
    found_any = False
    for num, matches in results.items():
        if matches:
            found_any = True
            lines.append(f"📌 เลข {num} (พบใน {len(matches)} จุด):")
            for m in matches:
                sheet = m['sheet_id'] or 'ไม่ระบุใบที่'
                emp = m['employee_name'] or 'ไม่ระบุชื่อ'
                cat = m['category']
                lines.append(f"  • ใบที่ {sheet} | หมวด '{cat}' | รายการ: {m['raw_text']}")
                lines.append(f"    (ผู้บันทึก: {emp})")
        else:
            lines.append(f"❌ เลข {num}: ไม่พบในระบบ")
        lines.append("")
        
    return "\n".join(lines).strip()

def format_daily_status(period_id: int = None) -> str:
    active = database.get_active_period()
    latest = database.get_latest_period()
    p = active or latest
    p_name = p["name"] if p else "งวดปัจจุบัน"
    p_status = "🟢 เปิดรับข้อมูล" if (p and p.get("status") == "OPEN") else "🔴 ปิดงวดอยู่"
    
    summary = database.get_daily_summary(p["id"] if p else 1)
    lines = [
        "📊 รายงานสถานะประจำงวด",
        f"📌 {p_name} ({p_status})",
        "─────────────────────────",
        f"📄 เอกสารทั้งหมด: {summary['total_sheets']} แผ่น",
        f"📝 รายการข้อมูลทั้งหมด: {summary['total_entries']} รายการ",
        f"✅ รายการที่ถูกต้อง: {summary['valid_entries']} รายการ"
    ]
    invalid_entries = summary['total_entries'] - summary['valid_entries']
    if invalid_entries > 0:
        lines.append(f"⚠️ รายการที่ต้องตรวจสอบ: {invalid_entries} รายการ")
        
    lines.append("─────────────────────────")
    if summary['sheets']:
        lines.append("📋 ตัวอย่างใบที่เข้าระบบแล้ว:")
        for s in summary['sheets'][:10]:
            status_icon = "✅" if s['status'] == "VERIFIED" else "⚠️"
            sheet_id = s['sheet_id'] or 'ไม่ระบุ'
            emp = s['employee_name'] or '-'
            lines.append(f"  {status_icon} ใบที่: {sheet_id} (ผู้ส่ง: {emp})")
        if len(summary['sheets']) > 10:
            lines.append(f"  (และอีก {len(summary['sheets']) - 10} แผ่น)")
            
    return "\n".join(lines)

def format_audit_report(period_id: int = None) -> str:
    audit = database.run_audit_report(period_id)
    active = database.get_active_period() or database.get_latest_period()
    p_name = active["name"] if active else "งวดปัจจุบัน"
    
    lines = [
        "🔍 รายงานรีเช็คความถูกต้องของงาน (Audit)",
        f"📌 [{p_name}]",
        "─────────────────────────",
        f"📊 ยอดรวม: {audit['total_sheets']} แผ่น ({audit['total_entries']} รายการ)"
    ]
    
    if audit['pending_scans'] > 0:
        lines.append(f"⏳ มี {audit['pending_scans']} ใบ ที่ยังค้างรอกดยืนยันบันทึก!")
        
    lines.append("")
    lines.append("1️⃣ ตรวจสอบลำดับกระดาษรายคน:")
    seqs = audit.get("worker_sequences", {})
    if not seqs:
        lines.append("  (ยังไม่มีข้อมูลแผ่นที่บันทึก)")
    else:
        for code, info in seqs.items():
            if info["is_complete"]:
                lines.append(f"  • รหัส {code}: ส่งแล้ว 1–{info['max_sheet']} ➔ ✅ ครบทุกแผ่น")
            else:
                missing_str = ", ".join([f"{code}-{m}" for m in info["missing_sheets"][:8]])
                lines.append(f"  • รหัส {code}: ❌ ขาดแผ่นที่ [{missing_str}]!")
                
    lines.append("")
    lines.append("─────────────────────────")
    # Action recommendations
    lines.append("🎯 คำแนะนำ:")
    has_issues = False
    for code, info in seqs.items():
        if not info["is_complete"]:
            has_issues = True
            missing_str = ", ".join([f"{code}-{m}" for m in info["missing_sheets"][:5]])
            lines.append(f"• พนักงาน {code}: ตามหาแผ่นที่ [{missing_str}] มาส่งด่วนครับ")
            
    if audit['pending_scans'] > 0:
        has_issues = True
        lines.append(f"• มีภาพที่ยังไม่กดยืนยัน {audit['pending_scans']} แผ่น รบกวนตรวจและกดยืนยันครับ")
        
    if not has_issues:
        lines.append("✨ ข้อมูลทั้งหมดถูกต้อง ครบถ้วน 100% พร้อมปิดงวดครับ!")
        
    return "\n".join(lines)
