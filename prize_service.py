import urllib.request
import urllib.parse
import json
import re
from typing import Dict, List, Optional
import database

DEFAULT_PAYOUT_RATES = {
    "3_TOP": 800,      # 3 ตัวบน (เช่น บาทละ 800)
    "3_TOD": 120,      # 3 ตัวโต๊ด (บาทละ 120)
    "2_TOP": 90,       # 2 ตัวบน (บาทละ 90)
    "2_BOT": 90,       # 2 ตัวล่าง (บาทละ 90)
    "6_RETURN": 800    # 6 กลับ (บาทละ 800)
}

def fetch_latest_thai_lottery() -> Dict:
    """
    Fetches the latest Thai lottery results from GLO Official (POST) or Rayriffy Open API.
    Returns structured prize data and detailed status with timestamp.
    """
    import datetime
    import ssl
    now_th = datetime.datetime.now().strftime("%d/%m/%Y เวลา %H:%M น.")

    try:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
    except Exception:
        ssl_ctx = None

    # 1. GLO Official API (Requires POST with application/json)
    try:
        glo_url = "https://www.glo.or.th/api/lottery/getLatestLottery"
        req = urllib.request.Request(
            glo_url,
            data=b"{}",
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Content-Type': 'application/json',
                'Accept': 'application/json, text/plain, */*'
            },
            method="POST"
        )
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=7) as resp:
            raw = json.loads(resp.read().decode('utf-8'))
            data = raw.get("response", {}).get("data", {})
            first_num = str(data.get("first", {}).get("number", "")).strip()
            last2_num = str(data.get("last2", {}).get("number", "")).strip()
            date_str = str(data.get("date", "") or data.get("displayDate", "")).strip()
            if len(first_num) == 6 and len(last2_num) == 2:
                return {
                    "status": "success",
                    "source": "สำนักงานสลากกินแบ่งรัฐบาล (GLO Official API)",
                    "date": date_str or "งวดล่าสุด",
                    "prize1": first_num,
                    "top3": first_num[-3:],
                    "top2": first_num[-2:],
                    "bottom2": last2_num,
                    "fetched_at": now_th
                }
    except Exception as e:
        print(f"⚠️ GLO API fetch error: {e}")

    # 2. Rayriffy Thai Lottery API (GET)
    try:
        ray_url = "https://lotto.api.rayriffy.com/latest"
        req = urllib.request.Request(
            ray_url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json'
            }
        )
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=7) as resp:
            raw = json.loads(resp.read().decode('utf-8'))
            response = raw.get("response", {})
            date_str = str(response.get("date", "")).strip()
            first_num = ""
            last2_num = ""
            
            prizes = response.get("prizes", [])
            for p in prizes:
                pid = p.get("id", "")
                pname = str(p.get("name", ""))
                if pid == "prizeFirst" or "รางวัลที่ 1" in pname:
                    nums = p.get("numbers") or p.get("number") or []
                    if isinstance(nums, list) and nums:
                        first_num = str(nums[0]).strip()
                    elif isinstance(nums, str):
                        first_num = nums.strip()
                elif pid == "runningNumberBackTwo" or "เลขท้าย 2 ตัว" in pname or "2 ตัว" in pname:
                    nums = p.get("numbers") or p.get("number") or []
                    if isinstance(nums, list) and nums:
                        last2_num = str(nums[0]).strip()
                    elif isinstance(nums, str):
                        last2_num = nums.strip()
                        
            running = response.get("runningNumbers", [])
            for r in running:
                rid = r.get("id", "")
                rname = str(r.get("name", ""))
                if rid == "runningNumberBackTwo" or "เลขท้าย 2 ตัว" in rname or "2 ตัว" in rname:
                    nums = r.get("numbers") or r.get("number") or []
                    if isinstance(nums, list) and nums:
                        last2_num = str(nums[0]).strip()
                    elif isinstance(nums, str):
                        last2_num = nums.strip()

            if len(first_num) == 6 and len(last2_num) >= 2:
                return {
                    "status": "success",
                    "source": "Rayriffy Lottery Open API",
                    "date": date_str or "งวดล่าสุด",
                    "prize1": first_num,
                    "top3": first_num[-3:],
                    "top2": first_num[-2:],
                    "bottom2": last2_num[:2],
                    "fetched_at": now_th
                }
    except Exception as e:
        print(f"⚠️ Rayriffy API fetch error: {e}")

    # Fallback status if offline or lottery not drawn yet
    return {
        "status": "offline",
        "source": "ไม่สามารถติดต่อเซิร์ฟเวอร์สลากได้",
        "date": "งวดปัจจุบัน",
        "prize1": "",
        "top3": "",
        "top2": "",
        "bottom2": "",
        "fetched_at": now_th
    }

def get_permutations_3(s: str) -> List[str]:
    """Generates all unique permutations of 3 digits."""
    if len(s) != 3:
        return []
    import itertools
    perms = set("".join(p) for p in itertools.permutations(s))
    return list(perms)

def check_period_winners(period_id: int, top3: str, bottom2: str, payout_rates: Optional[Dict] = None) -> Dict:
    """
    Scans all confirmed entries for the given period against the winning numbers.
    """
    rates = dict(DEFAULT_PAYOUT_RATES)
    if payout_rates:
        rates.update(payout_rates)
        
    clean_top3 = re.sub(r'\D', '', str(top3)).strip()
    clean_bot2 = re.sub(r'\D', '', str(bottom2)).strip()
    clean_top2 = clean_top3[-2:] if len(clean_top3) >= 2 else ""
    
    top3_perms = set(get_permutations_3(clean_top3)) if len(clean_top3) == 3 else set()
    top3_tod_set = top3_perms - {clean_top3}
    
    database.init_db()
    conn = database.get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT e.id, e.period_id, e.sheet_id, e.employee_name, e.worker_code,
           e.category, e.set1, e.set2, e.set3, e.raw_text, s.image_path
    FROM entries e
    LEFT JOIN sheets s ON e.sheet_db_id = s.id
    WHERE e.period_id = ? AND e.is_valid = 1
    ORDER BY e.sheet_db_id ASC, e.id ASC
    """, (period_id,))
    entries = cursor.fetchall()
    conn.close()
    
    winners = []
    total_payout = 0.0
    
    for r in entries:
        s1 = re.sub(r'\D', '', str(r["set1"])).strip()
        s2 = str(r["set2"] or "").strip()
        s3 = str(r["set3"] or "").strip().lower()
        cat = str(r["category"] or "").strip()
        
        # Parse bet amount from set2
        bet_amount = 0.0
        try:
            if "x" in s2.lower():
                parts = s2.lower().split("x")
                bet_amount = float(re.sub(r'[^\d.]', '', parts[0])) if parts[0] else 0.0
            else:
                bet_amount = float(re.sub(r'[^\d.]', '', s2)) if s2 else 0.0
        except Exception:
            bet_amount = 0.0
            
        is_won = False
        prize_type = ""
        rate = 0
        
        # 1. Check 3 Digits
        if len(s1) == 3 and len(clean_top3) == 3:
            if cat in ("บน", "บนล่าง"):
                if s1 == clean_top3:
                    is_won = True
                    prize_type = "3 ตัวบน (ตรง)"
                    rate = rates["3_TOP"]
                elif s1 in top3_tod_set and (s3 == "ก3" or "โต๊ด" in s3 or cat == "บน"):
                    is_won = True
                    prize_type = "3 ตัวโต๊ด"
                    rate = rates["3_TOD"]
                elif s3 == "ก6" and s1 in top3_perms:
                    is_won = True
                    prize_type = "3 ตัวบน (6 กลับ)"
                    rate = rates["6_RETURN"]

        # 2. Check 2 Digits
        elif len(s1) == 2:
            # 2 ตัวบน
            if cat in ("บน", "บนล่าง") and clean_top2 and s1 == clean_top2:
                is_won = True
                prize_type = "2 ตัวบน"
                rate = rates["2_TOP"]
                
            # 2 ตัวล่าง
            if cat in ("ล่าง", "บนล่าง") and clean_bot2 and s1 == clean_bot2:
                if is_won: # Won both on top-bottom
                    payout_sub = bet_amount * rates["2_BOT"]
                    total_payout += payout_sub
                    winners.append({
                        "entry_id": r["id"],
                        "sheet_id": r["sheet_id"],
                        "worker_code": r["worker_code"] or "A",
                        "employee_name": r["employee_name"],
                        "category": cat,
                        "set1": s1,
                        "set2": s2,
                        "set3": s3,
                        "prize_type": "2 ตัวล่าง",
                        "bet_amount": bet_amount,
                        "rate": rates["2_BOT"],
                        "payout": payout_sub,
                        "image_path": r["image_path"] or ""
                    })
                else:
                    is_won = True
                    prize_type = "2 ตัวล่าง"
                    rate = rates["2_BOT"]
                    
        if is_won:
            payout = bet_amount * rate
            total_payout += payout
            winners.append({
                "entry_id": r["id"],
                "sheet_id": r["sheet_id"],
                "worker_code": r["worker_code"] or "A",
                "employee_name": r["employee_name"],
                "category": cat,
                "set1": s1,
                "set2": s2,
                "set3": s3,
                "prize_type": prize_type,
                "bet_amount": bet_amount,
                "rate": rate,
                "payout": payout,
                "image_path": r["image_path"] or ""
            })
            
    return {
        "period_id": period_id,
        "top3": clean_top3,
        "top2": clean_top2,
        "bottom2": clean_bot2,
        "total_winners": len(winners),
        "total_payout": total_payout,
        "winners": winners
    }

def render_prizes_page(period_id: Optional[int] = None, custom_prizes: Optional[Dict] = None) -> str:
    """Renders the HTML page for lottery checking and winning settlement."""
    all_periods = database.get_all_periods()
    active_period = database.get_active_period()
    latest_period = database.get_latest_period()
    selected_p = (next((p for p in all_periods if p["id"] == period_id), None) if period_id else None) or active_period or latest_period
    p_id = selected_p["id"] if selected_p else 1
    p_name = selected_p["name"] if selected_p else "งวดปัจจุบัน"
    
    import datetime
    now_th = datetime.datetime.now().strftime("%d/%m/%Y เวลา %H:%M น.")

    prize_info = custom_prizes or {}
    is_custom = bool(prize_info.get("top3") or prize_info.get("bottom2"))
    
    if not is_custom:
        fetched = fetch_latest_thai_lottery()
        top3 = fetched.get("top3", "")
        bottom2 = fetched.get("bottom2", "")
        fetch_status = fetched.get("status", "offline")
        source_note = fetched.get("source", "เว็บสลากกินแบ่งฯ")
        draw_date = fetched.get("date", p_name)
        updated_at = fetched.get("fetched_at", now_th)
    else:
        top3 = prize_info.get("top3", "").strip()
        bottom2 = prize_info.get("bottom2", "").strip()
        fetch_status = "manual"
        source_note = "กำหนดตัวเลขเองโดยผู้ใช้งาน (Manual Override)"
        draw_date = p_name
        updated_at = now_th

    if fetch_status == "success":
        banner_bg = "#f0fdf4"
        banner_border = "#86efac"
        banner_title = "🟢 ดึงผลรางวัลสลากกินแบ่งรัฐบาลล่าสุดสำเร็จแล้ว!"
        banner_desc = f"📡 แหล่งข้อมูล: <b>{source_note}</b> &nbsp;|&nbsp; 📅 งวดประจำวันที่: <b>{draw_date}</b>"
        badge_color = "#16a34a"
    elif is_custom:
        banner_bg = "#eff6ff"
        banner_border = "#93c5fd"
        banner_title = "✏️ กำลังตรวจในโหมดกำหนดตัวเลขเอง (Manual Input)"
        banner_desc = f"ตรวจรางวัลด้วยเลขที่ระบุเอง: <b>3 ตัวบน = {top3 or '-'}</b> &nbsp;|&nbsp; <b>2 ตัวล่าง = {bottom2 or '-'}</b>"
        badge_color = "#2563eb"
    else:
        banner_bg = "#fffbeb"
        banner_border = "#fcd34d"
        banner_title = "🟡 ยังไม่สามารถดึงผลอัตโนมัติจากกองสลากได้ในขณะนี้"
        banner_desc = "💡 อาจยังไม่ออกผลประจำงวด หรือเซิร์ฟเวอร์สลากกำลังปรับปรุง — คุณสามารถ <b>พิมพ์เลข 3 ตัวบน และ 2 ตัวล่าง เองในช่องด้านล่าง</b> แล้วกด [🔍 ตรวจรางวัล] ได้ทันทีครับ"
        badge_color = "#d97706"

    status_banner_html = f"""
    <div style="background:{banner_bg}; border:2px solid {banner_border}; padding:16px 20px; border-radius:14px; margin-bottom:20px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px; box-shadow:0 1px 3px rgba(0,0,0,0.05);">
        <div>
            <div style="font-size:16px; font-weight:800; color:{badge_color}; display:flex; align-items:center; gap:6px;">{banner_title}</div>
            <div style="font-size:13px; color:#334155; margin-top:5px;">{banner_desc}</div>
            <div style="font-size:12px; color:#64748b; margin-top:4px;">⏱️ อัปเดตล่าสุด: <b>{updated_at}</b></div>
        </div>
        <div>
            <a href="/prizes?period_id={p_id}" style="background:{badge_color}; color:white; padding:10px 16px; border-radius:10px; text-decoration:none; font-weight:700; font-size:13px; display:inline-flex; align-items:center; gap:6px; box-shadow:0 2px 4px rgba(0,0,0,0.1);">
                🔄 ดึงผลสดใหม่จากกองสลาก
            </a>
        </div>
    </div>
    """

    results = check_period_winners(p_id, top3, bottom2)
    financials = database.get_period_financials(p_id)
    total_inflow = financials.get("total_inflow", 0.0)
    total_payout = results.get("total_payout", 0.0)
    net_balance = total_inflow - total_payout
    margin_pct = (net_balance / total_inflow * 100) if total_inflow > 0 else 0.0
    is_profit = net_balance >= 0

    balance_color = "#16a34a" if is_profit else "#dc2626"
    balance_sign = "+" if net_balance > 0 else ""
    balance_tag = f"🟢 กำไร {margin_pct:.1f}%" if is_profit else f"🔴 ขาดทุน {abs(margin_pct):.1f}%"
    if total_inflow == 0 and total_payout == 0:
        balance_tag = "⚪️ ยอดเสมอตัว (0%)"

    financial_card_html = f"""
    <div style="background:white; border-radius:14px; border:1px solid #e2e8f0; padding:18px 20px; margin-bottom:20px; box-shadow:0 1px 3px rgba(0,0,0,0.05);">
        <div style="font-size:16px; font-weight:800; color:#0f172a; margin-bottom:14px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
            <span>💰 สรุปบัญชีกำไร-ขาดทุนประจำงวด (P&amp;L Financial Summary)</span>
            <span style="font-size:13px; font-weight:700; color:#475569; background:#f1f5f9; padding:4px 10px; border-radius:8px;">{p_name}</span>
        </div>
        <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(210px, 1fr)); gap:14px;">
            <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:12px; padding:16px; text-align:center;">
                <div style="font-size:12px; font-weight:700; color:#64748b; text-transform:uppercase;">📥 ยอดรับรวมทั้งหมด (Inflow)</div>
                <div style="font-size:26px; font-weight:800; color:#2563eb; margin-top:4px;">{total_inflow:,.0f} <span style="font-size:14px; font-weight:600;">บาท</span></div>
                <div style="font-size:11px; color:#94a3b8; margin-top:4px;">บน {financials['inflow_by_category'].get('บน', 0):,.0f} | ล่าง {financials['inflow_by_category'].get('ล่าง', 0):,.0f} | บนล่าง {financials['inflow_by_category'].get('บนล่าง', 0):,.0f}</div>
            </div>
            <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:12px; padding:16px; text-align:center;">
                <div style="font-size:12px; font-weight:700; color:#64748b; text-transform:uppercase;">📤 ยอดจ่ายรางวัลรวม (Payout)</div>
                <div style="font-size:26px; font-weight:800; color:#dc2626; margin-top:4px;">{total_payout:,.0f} <span style="font-size:14px; font-weight:600;">บาท</span></div>
                <div style="font-size:11px; color:#94a3b8; margin-top:4px;">ผู้ถูกรางวัล {results['total_winners']} รายการ</div>
            </div>
            <div style="background:#f8fafc; border:2px solid {balance_color}; border-radius:12px; padding:16px; text-align:center;">
                <div style="font-size:12px; font-weight:700; color:#64748b; text-transform:uppercase;">💵 กำไรสุทธิ (Net Margin)</div>
                <div style="font-size:26px; font-weight:800; color:{balance_color}; margin-top:4px;">{balance_sign}{net_balance:,.0f} <span style="font-size:14px; font-weight:600;">บาท</span></div>
                <div style="font-size:12px; font-weight:700; color:{balance_color}; margin-top:4px;">{balance_tag}</div>
            </div>
        </div>
    </div>
    """
    
    winners_rows = ""
    for idx, w in enumerate(results["winners"], 1):
        img_btn = f'<a href="/{w["image_path"]}" target="_blank" class="btn-pic">📷 ดูรูป</a>' if w["image_path"] else "-"
        winners_rows += f"""
        <tr>
            <td style="text-align:center; font-weight:bold; color:#64748b;">#{idx}</td>
            <td><strong>{w['sheet_id']}</strong></td>
            <td style="text-align:center;"><span class="badge-worker">{w['worker_code']}</span> {w['employee_name']}</td>
            <td style="text-align:center;">{w['category']}</td>
            <td><strong style="font-size:17px; font-family:monospace; color:#0f172a;">{w['set1']}</strong></td>
            <td><span class="badge-prize">{w['prize_type']}</span></td>
            <td style="text-align:right;">{w['bet_amount']:,.0f}</td>
            <td style="text-align:right; font-weight:800; color:#16a34a; font-size:16px;">{w['payout']:,.0f} บาท</td>
            <td style="text-align:center;">{img_btn}</td>
        </tr>
        """
        
    if not winners_rows:
        winners_rows = f"<tr><td colspan='9' style='text-align:center; padding:35px; color:#94a3b8;'>ไม่พบรายการที่ถูกรางวัลใน {p_name}</td></tr>"

    period_options = ""
    for p in all_periods:
        sel = "selected" if p["id"] == p_id else ""
        period_options += f"<option value='{p['id']}' {sel}>{p['name']}</option>"

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🎰 ระบบตรวจผลรางวัล - {p_name}</title>
    <style>
        * {{ box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, 'Prompt', 'Segoe UI', Roboto, sans-serif; }}
        body {{ background: #f8fafc; color: #1e293b; margin: 0; padding: 16px; }}
        .container {{ max-width: 1100px; margin: 0 auto; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }}
        .title {{ font-size: 24px; font-weight: 800; color: #0f172a; margin: 0; }}
        
        .prize-cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin-bottom: 20px; }}
        .prize-card {{ background: white; border-radius: 14px; border: 1px solid #e2e8f0; padding: 18px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
        .prize-label {{ font-size: 13px; font-weight: 700; color: #64748b; text-transform: uppercase; }}
        .prize-num {{ font-size: 32px; font-weight: 800; color: #dc2626; font-family: monospace; letter-spacing: 2px; margin-top: 6px; }}
        
        .controls-card {{ background: white; border-radius: 14px; border: 1px solid #e2e8f0; padding: 16px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
        .form-row {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-end; }}
        .form-group {{ flex: 1; min-width: 140px; }}
        label {{ font-size: 13px; font-weight: 700; color: #475569; display: block; margin-bottom: 6px; }}
        input[type="text"], select {{ width: 100%; padding: 10px 14px; border: 1px solid #cbd5e1; border-radius: 10px; font-size: 15px; font-weight: 700; outline: none; }}
        
        .btn {{ padding: 10px 20px; border-radius: 10px; font-size: 14px; font-weight: 700; border: none; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; gap: 6px; }}
        .btn-fetch {{ background: #2563eb; color: white; }}
        .btn-check {{ background: #16a34a; color: white; }}
        .btn-back {{ background: #64748b; color: white; }}
        .btn-pic {{ background: #eff6ff; color: #2563eb; border: 1px solid #bfdbfe; padding: 4px 8px; border-radius: 6px; font-size: 12px; text-decoration: none; font-weight: 700; }}

        .summary-banner {{ background: linear-gradient(135deg, #1e3a8a 0%, #3b82f6 100%); color: white; padding: 20px; border-radius: 14px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 14px; }}
        .summary-val {{ font-size: 32px; font-weight: 800; }}

        .table-container {{ background: white; border: 1px solid #e2e8f0; border-radius: 14px; overflow-x: auto; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
        table {{ width: 100%; border-collapse: collapse; text-align: left; font-size: 14px; }}
        th {{ background: #f1f5f9; padding: 14px 16px; font-weight: 800; color: #1e293b; border-bottom: 2px solid #cbd5e1; }}
        td {{ padding: 12px 16px; border-bottom: 1px solid #f1f5f9; }}
        tr:hover {{ background: #f8fafc; }}

        .badge-prize {{ background: #fef3c7; color: #b45309; font-weight: 700; font-size: 12px; padding: 4px 8px; border-radius: 6px; }}
        .badge-worker {{ background: #e2e8f0; color: #334155; font-weight: 700; font-size: 11px; padding: 3px 6px; border-radius: 4px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1 class="title">🎰 ระบบตรวจผลรางวัลอัตโนมัติ</h1>
                <div style="font-size:13px; color:#64748b; margin-top:4px;">ดึงผลจากเว็บ และคำนวณยอดจ่ายรางวัลประจำงวด</div>
            </div>
            <div style="display:flex; gap:8px;">
                <a href="/?period_id={p_id}" class="btn btn-back">⬅️ กลับหน้ากระดาน</a>
            </div>
        </div>

        {status_banner_html}

        {financial_card_html}

        <div class="controls-card">
            <form method="GET" action="/prizes">
                <div class="form-row">
                    <div class="form-group" style="flex:2;">
                        <label>📁 เลือกงวดที่ต้องการตรวจ:</label>
                        <select name="period_id" onchange="this.form.submit()">
                            {period_options}
                        </select>
                    </div>
                    <div class="form-group">
                        <label>3 ตัวบน:</label>
                        <input type="text" name="top3" value="{top3}" placeholder="เช่น 401">
                    </div>
                    <div class="form-group">
                        <label>2 ตัวล่าง:</label>
                        <input type="text" name="bottom2" value="{bottom2}" placeholder="เช่น 54">
                    </div>
                    <div>
                        <button type="submit" class="btn btn-check">🔍 ตรวจรางวัล</button>
                    </div>
                </div>
            </form>
        </div>

        <div class="prize-cards">
            <div class="prize-card">
                <div class="prize-label">3 ตัวบน</div>
                <div class="prize-num">{top3 or '-'}</div>
            </div>
            <div class="prize-card">
                <div class="prize-label">2 ตัวบน</div>
                <div class="prize-num" style="color:#2563eb;">{results['top2'] or '-'}</div>
            </div>
            <div class="prize-card">
                <div class="prize-label">2 ตัวล่าง</div>
                <div class="prize-num" style="color:#16a34a;">{bottom2 or '-'}</div>
            </div>
            <div class="prize-card">
                <div class="prize-label">แหล่งข้อมูลผลสลาก</div>
                <div style="font-size:16px; font-weight:700; color:#475569; margin-top:10px;">{source_note}</div>
            </div>
        </div>

        <div class="summary-banner">
            <div>
                <div style="font-size:14px; opacity:0.9;">ผลการตรวจรางวัล: {p_name}</div>
                <div style="font-size:22px; font-weight:800; margin-top:4px;">พบผู้ถูกรางวัลทั้งหมด {results['total_winners']} รายการ</div>
            </div>
            <div style="text-align:right;">
                <div style="font-size:13px; opacity:0.9;">ยอดเงินรางวัลรวมที่ต้องจ่าย</div>
                <div class="summary-val">{results['total_payout']:,.0f} <span style="font-size:18px; font-weight:normal;">บาท</span></div>
            </div>
        </div>

        <h3 style="margin:16px 0 10px 0; font-size:17px; color:#0f172a;">📋 รายการใบที่ถูกรางวัลในงวดนี้</h3>
        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        <th style="width:60px; text-align:center;">ลำดับ</th>
                        <th>ใบที่</th>
                        <th style="text-align:center;">ผู้ส่ง</th>
                        <th style="text-align:center;">หมวด</th>
                        <th>เลขที่ถูก</th>
                        <th>รางวัลที่ได้</th>
                        <th style="text-align:right;">ยอดเดิมพัน</th>
                        <th style="text-align:right;">ยอดเงินรางวัล</th>
                        <th style="text-align:center;">หลักฐาน</th>
                    </tr>
                </thead>
                <tbody>
                    {winners_rows}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>
"""
    return html
