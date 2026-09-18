# -*- coding: utf-8 -*-
"""
setup_rich_menu.py
สร้างและลงทะเบียน LINE Rich Menu (3 ปุ่มมาตรฐาน) ผ่าน LINE Messaging API:
1. 🌐 ดูตารางสด -> เปิดหน้าเว็บ Dashboard (https://paper-ocr-bot.onrender.com)
2. 🎰 ตรวจรางวัล -> เปิดหน้าระบบตรวจผลรางวัล (/prizes)
3. ❓ วิธีใช้งาน -> ส่งข้อความ 'วิธีใช้งาน' รับคู่มือคำสั่งใน LINE
"""

import os
import sys
import json
import urllib.request
import urllib.error
import config

LINE_API_ENDPOINT = "https://api.line.me/v2/bot"
LINE_DATA_ENDPOINT = "https://api-data.line.me/v2/bot"

def get_headers(content_type="application/json"):
    token = config.LINE_CHANNEL_ACCESS_TOKEN
    if not token:
        print("❌ ไม่พบ LINE_CHANNEL_ACCESS_TOKEN ใน environment หรือ config.py")
        sys.exit(1)
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type
    }

def create_rich_menu_structure(base_url: str) -> dict:
    dashboard_url = base_url.rstrip("/")
    prizes_url = f"{dashboard_url}/prizes"
    
    return {
        "size": {"width": 2500, "height": 843},
        "selected": True,
        "name": "OCR Bot Rich Menu (3 Tabs)",
        "chatBarText": "📌 เมนูหลัก (ตารางสด / ตรวจรางวัล)",
        "areas": [
            {
                "bounds": {"x": 0, "y": 0, "width": 833, "height": 843},
                "action": {
                    "type": "uri",
                    "label": "ดูตารางสด",
                    "uri": dashboard_url
                }
            },
            {
                "bounds": {"x": 833, "y": 0, "width": 834, "height": 843},
                "action": {
                    "type": "uri",
                    "label": "ตรวจรางวัล",
                    "uri": prizes_url
                }
            },
            {
                "bounds": {"x": 1667, "y": 0, "width": 833, "height": 843},
                "action": {
                    "type": "message",
                    "label": "วิธีใช้งาน",
                    "text": "วิธีใช้งาน"
                }
            }
        ]
    }

def delete_all_rich_menus():
    """ลบ Rich Menu เก่าทั้งหมดเพื่อไม่ให้ตกค้าง"""
    url = f"{LINE_API_ENDPOINT}/richmenu/list"
    req = urllib.request.Request(url, headers=get_headers())
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            menus = data.get("richmenus", [])
            print(f"ℹ️ พบ Rich Menu เดิม {len(menus)} รายการ")
            for m in menus:
                rm_id = m["richMenuId"]
                del_url = f"{LINE_API_ENDPOINT}/richmenu/{rm_id}"
                del_req = urllib.request.Request(del_url, headers=get_headers(), method="DELETE")
                try:
                    urllib.request.urlopen(del_req)
                    print(f"  🗑️ ลบ Rich Menu เก่า: {rm_id}")
                except Exception as e:
                    print(f"  ⚠️ ไม่สามารถลบ {rm_id}: {e}")
    except Exception as e:
        print(f"⚠️ เกิดข้อผิดพลาดในการดึงรายการ Rich Menu: {e}")

def setup_rich_menu():
    print("🚀 เริ่มต้นการติดตั้ง LINE Rich Menu...")
    token = config.LINE_CHANNEL_ACCESS_TOKEN
    if not token:
        print("❌ ERROR: กรุณาระบุ LINE_CHANNEL_ACCESS_TOKEN ใน config.py หรือ Render Environment Variables")
        return False
        
    base_url = os.environ.get("BASE_URL", config.BASE_URL)
    print(f"🔗 Dashboard Base URL: {base_url}")
    
    # 1. Clean old menus
    delete_all_rich_menus()
    
    # 2. Create Rich Menu definition
    menu_def = create_rich_menu_structure(base_url)
    create_url = f"{LINE_API_ENDPOINT}/richmenu"
    body_bytes = json.dumps(menu_def).encode("utf-8")
    
    print("📤 กำลังสร้าง Rich Menu Object...")
    req = urllib.request.Request(create_url, data=body_bytes, headers=get_headers(), method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            rich_menu_id = res_data.get("richMenuId")
            print(f"✅ สร้าง Rich Menu สำเร็จ ID: {rich_menu_id}")
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode('utf-8')
        print(f"❌ เกิดข้อผิดพลาดในการสร้าง Rich Menu ({e.code}): {err_msg}")
        return False

    # 3. Upload Rich Menu Image
    image_path = os.path.join(os.path.dirname(__file__), "rich_menu.jpg")
    content_type = "image/jpeg"
    if not os.path.exists(image_path):
        image_path = os.path.join(os.path.dirname(__file__), "rich_menu.png")
        content_type = "image/png"
        
    if not os.path.exists(image_path):
        print(f"❌ ไม่พบไฟล์ภาพ Rich Menu ที่: {image_path}")
        return False
        
    print(f"🖼️ กำลังอัปโหลดภาพ Rich Menu ({image_path})...")
    with open(image_path, "rb") as f:
        img_bytes = f.read()
        
    upload_url = f"{LINE_DATA_ENDPOINT}/richmenu/{rich_menu_id}/content"
    upload_req = urllib.request.Request(
        upload_url,
        data=img_bytes,
        headers=get_headers(content_type=content_type),
        method="POST"
    )
    try:
        with urllib.request.urlopen(upload_req) as resp:
            print("✅ อัปโหลดภาพ Rich Menu สำเร็จ")
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode('utf-8')
        print(f"❌ ไม่สามารถอัปโหลดภาพ Rich Menu ได้ ({e.code}): {err_msg}")
        return False

    # 4. Set as Default Rich Menu for all users
    set_default_url = f"{LINE_API_ENDPOINT}/user/all/richmenu/{rich_menu_id}"
    set_req = urllib.request.Request(set_default_url, headers=get_headers(), method="POST")
    try:
        with urllib.request.urlopen(set_req) as resp:
            print(f"🎉 ตั้งค่า Rich Menu ID: {rich_menu_id} เป็นค่าเริ่มต้นสำหรับผู้ใช้ทุกคนเรียบร้อย!")
            return True
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode('utf-8')
        print(f"❌ ไม่สามารถตั้งเป็น default ได้ ({e.code}): {err_msg}")
        return False

if __name__ == "__main__":
    setup_rich_menu()
