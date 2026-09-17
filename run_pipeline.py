import os
import json
import ocr_engine
from validator import OCRValidator
import database
import query_service

def main():
    print("==================================================")
    print("🚀 เริ่มต้นทดสอบระบบนำเข้าข้อมูลและค้นหา (Phase 1)")
    print("==================================================")
    
    # 1. Test image 1: Clean sample format
    img_sample = "/Users/danupon/.gemini/antigravity/brain/f14e79bc-74ae-4d65-8a53-ce096474a0bd/.user_uploaded/media_1789621590769.jpg"
    print(f"\n📸 [1/4] อ่านภาพตัวอย่าง: {os.path.basename(img_sample)}")
    ocr_result = ocr_engine.extract_from_file(img_sample)
    
    # Fill in sample sheet ID for demonstration if header was empty in handwriting
    if not ocr_result["header"].get("sheet_id"):
        ocr_result["header"]["sheet_id"] = "TEST-01"
        ocr_result["header"]["customer_name"] = "พนักงาน ก."
        ocr_result["header"]["date"] = "17/09/69"
        
    print(f"   -> สกัดข้อมูลได้: {len(ocr_result['columns'].get('top', []))} รายการใน 'บน', {len(ocr_result['columns'].get('bottom', []))} รายการใน 'ล่าง'")
    
    # 2. Validation
    print("\n🔍 [2/4] ตรวจสอบความถูกต้องด้วย Zero-Error Validation Engine...")
    val_result = OCRValidator.validate_document(ocr_result)
    print(f"   -> สถานะ: {'✅ ผ่านทั้งหมด 100%' if val_result['is_all_valid'] else '⚠️ พบข้อผิดพลาด'}")
    print(f"   -> รายการที่ผ่าน: {val_result['valid_items']} / {val_result['total_items']}")
    
    # 3. Save to Database
    print("\n💾 [3/4] บันทึกลงฐานข้อมูล SQLite...")
    sheet_db_id = database.save_document(ocr_result, val_result, img_sample)
    print(f"   -> บันทึกเรียบร้อย (Sheet ID: {sheet_db_id})")
    
    # Export CSV
    csv_path = database.export_csv()
    print(f"   -> ส่งออกไฟล์รายงาน CSV เรียบร้อย: {csv_path}")
    
    # 4. Test Queries (Simulating Owner Line Commands)
    print("\n==================================================")
    print("📱 จำลองการใช้งานคำสั่งของเจ้าของผ่าน LINE")
    print("==================================================")
    
    # Query 1: Search single number
    print("\n[คำสั่งจากเจ้าของ]: เช็ค 401")
    print(query_service.format_search_results(["401"]))
    
    # Query 2: Search multiple numbers
    print("\n[คำสั่งจากเจ้าของ]: เช็ค 377 12 999")
    print(query_service.format_search_results(["377", "12", "999"]))
    
    # Query 3: Status
    print("\n[คำสั่งจากเจ้าของ]: สถานะ")
    print(query_service.format_daily_status())
    
    print("\n==================================================")
    print("✅ ทดสอบเสร็จสิ้นสมบูรณ์!")
    print("==================================================")

if __name__ == "__main__":
    main()
