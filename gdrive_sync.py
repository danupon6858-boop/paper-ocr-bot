# -*- coding: utf-8 -*-
"""
gdrive_sync.py
โมดูลสำรองและกู้คืนข้อมูลอัตโนมัติด้วย Google Drive 5TB ผ่าน Google Apps Script Webhook
- กู้คืน records.db อัตโนมัติเมื่อ Render บูตเครื่องใหม่ (Boot Restore)
- สำรอง records.db และรูปภาพใน uploads/ ไปยัง Google Drive อัตโนมัติใน Background Thread
- หากยังไม่ได้ใส่ GDRIVE_SYNC_URL ระบบจะทำงานโหมด local ปกติ 100% ไม่กระทบการใช้งาน
"""

import os
import sys
import base64
import json
import threading
import urllib.request
import urllib.error
import config

GDRIVE_SYNC_URL = os.environ.get("GDRIVE_SYNC_URL", "").strip()

def is_configured() -> bool:
    return bool(GDRIVE_SYNC_URL and GDRIVE_SYNC_URL.startswith("https://script.google.com/"))

def restore_db_from_gdrive() -> bool:
    """
    ดึง records.db ล่าสุดจาก Google Drive เมื่อระบบเปิดเครื่องขึ้นมาใหม่
    """
    if not is_configured():
        print("ℹ️ [GDrive Sync] ยังไม่ได้ตั้งค่า GDRIVE_SYNC_URL (ใช้ฐานข้อมูลในเครื่อง)")
        return False
        
    print("🔄 [GDrive Sync] กำลังตรวจสอบและดึง records.db จาก Google Drive...")
    try:
        url = f"{GDRIVE_SYNC_URL}?action=restore_db"
        req = urllib.request.Request(url, headers={"User-Agent": "PaperOCR-Bot"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "success" and data.get("db_base64"):
                db_bytes = base64.b64decode(data["db_base64"])
                with open(config.DB_PATH, "wb") as f:
                    f.write(db_bytes)
                print(f"✅ [GDrive Sync] กู้คืน records.db สำเร็จ ({len(db_bytes):,} bytes)")
                return True
            else:
                print(f"ℹ️ [GDrive Sync] ยังไม่มีไฟล์สำรองใน Google Drive หรือ: {data.get('message')}")
                return False
    except Exception as e:
        print(f"⚠️ [GDrive Sync] ไม่สามารถดึงฐานข้อมูลจาก Google Drive ได้: {e}")
        return False

def _do_backup_db():
    if not is_configured():
        return
    if not os.path.exists(config.DB_PATH):
        return
        
    try:
        with open(config.DB_PATH, "rb") as f:
            db_bytes = f.read()
            
        payload = {
            "action": "backup_db",
            "db_base64": base64.b64encode(db_bytes).decode("utf-8")
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            GDRIVE_SYNC_URL,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "PaperOCR-Bot"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            if res_data.get("status") == "success":
                print(f"☁️ [GDrive Sync] สำรอง records.db ขึ้น Google Drive เรียบร้อย ({len(db_bytes):,} bytes)")
            else:
                print(f"⚠️ [GDrive Sync] ตอบกลับ: {res_data}")
    except Exception as e:
        print(f"⚠️ [GDrive Sync] การสำรอง records.db ขัดข้อง: {e}")

def trigger_db_backup():
    """เรียกสำรองฐานข้อมูลแบบ Async (ไม่บล็อกการทำงานหลัก)"""
    if is_configured():
        t = threading.Thread(target=_do_backup_db, daemon=True)
        t.start()

def _do_upload_image(rel_path: str):
    if not is_configured():
        return
    local_path = os.path.join(os.path.dirname(__file__), rel_path.lstrip("/"))
    if not os.path.exists(local_path):
        return
        
    try:
        filename = os.path.basename(local_path)
        with open(local_path, "rb") as f:
            img_bytes = f.read()
            
        payload = {
            "action": "upload_image",
            "filename": filename,
            "img_base64": base64.b64encode(img_bytes).decode("utf-8")
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            GDRIVE_SYNC_URL,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "PaperOCR-Bot"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            if res_data.get("status") == "success":
                print(f"☁️ [GDrive Sync] สำรองรูป {filename} ขึ้น Google Drive เรียบร้อย")
    except Exception as e:
        print(f"⚠️ [GDrive Sync] สำรองรูป {rel_path} ขัดข้อง: {e}")

def trigger_image_backup(rel_path: str):
    """เรียกสำรองรูปภาพแบบ Async"""
    if is_configured():
        t = threading.Thread(target=_do_upload_image, args=(rel_path,), daemon=True)
        t.start()

GOOGLE_APPS_SCRIPT_CODE = """
function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);
    var rootFolder = getOrCreateFolder("PaperOCR_Backup");
    
    if (body.action === "backup_db") {
      var dbBlob = Utilities.newBlob(Utilities.base64Decode(body.db_base64), "application/octet-stream", "records.db");
      var files = rootFolder.getFilesByName("records.db");
      while (files.hasNext()) {
        files.next().setTrashed(true);
      }
      rootFolder.createFile(dbBlob);
      return ContentService.createTextOutput(JSON.stringify({status: "success", message: "DB backed up"})).setMimeType(ContentService.MimeType.JSON);
    }
    
    if (body.action === "upload_image") {
      var imgFolder = getOrCreateFolder("PaperOCR_Images", rootFolder);
      var imgBlob = Utilities.newBlob(Utilities.base64Decode(body.img_base64), "image/jpeg", body.filename);
      var files = imgFolder.getFilesByName(body.filename);
      while (files.hasNext()) {
        files.next().setTrashed(true);
      }
      imgFolder.createFile(imgBlob);
      return ContentService.createTextOutput(JSON.stringify({status: "success", message: "Image saved"})).setMimeType(ContentService.MimeType.JSON);
    }
    
    return ContentService.createTextOutput(JSON.stringify({status: "error", message: "Unknown action"})).setMimeType(ContentService.MimeType.JSON);
  } catch(err) {
    return ContentService.createTextOutput(JSON.stringify({status: "error", message: err.toString()})).setMimeType(ContentService.MimeType.JSON);
  }
}

function doGet(e) {
  try {
    var action = e.parameter.action;
    var rootFolder = getOrCreateFolder("PaperOCR_Backup");
    
    if (action === "restore_db") {
      var files = rootFolder.getFilesByName("records.db");
      if (files.hasNext()) {
        var file = files.next();
        var bytes = file.getBlob().getBytes();
        var b64 = Utilities.base64Encode(bytes);
        return ContentService.createTextOutput(JSON.stringify({status: "success", db_base64: b64})).setMimeType(ContentService.MimeType.JSON);
      } else {
        return ContentService.createTextOutput(JSON.stringify({status: "not_found", message: "No backup file found"})).setMimeType(ContentService.MimeType.JSON);
      }
    }
    return ContentService.createTextOutput(JSON.stringify({status: "ok", message: "PaperOCR GDrive Bridge is Active"})).setMimeType(ContentService.MimeType.JSON);
  } catch(err) {
    return ContentService.createTextOutput(JSON.stringify({status: "error", message: err.toString()})).setMimeType(ContentService.MimeType.JSON);
  }
}

function getOrCreateFolder(folderName, parentFolder) {
  var parent = parentFolder || DriveApp;
  var folders = parent.getFoldersByName(folderName);
  if (folders.hasNext()) {
    return folders.next();
  }
  return parent.createFolder(folderName);
}
"""
