import re
import json
from typing import List, Dict, Tuple

class OCRValidator:
    """
    Zero-Error Validation Engine
    Enforces strict structural and business rules:
      - Set 1: Digits only (or '?' for uncertain), 2-4 characters
      - Set 3: Whitelisted to '', 'ก3', or 'ก6'
      - Set 2: If Set 3 present -> single number only. If absent -> single number or NxN.
    
    Entries with '?' are treated as "uncertain" (is_valid=True, is_uncertain=True).
    This allows them to be confirmed/inserted while flagging for review.
    """
    @staticmethod
    def validate_entry(entry: Dict[str, str], col_name: str) -> Tuple[bool, List[str]]:
        errors = []
        set1 = entry.get("set1", "").strip()
        set2 = entry.get("set2", "").strip().lower()
        set3 = entry.get("set3", "").strip()
        raw = entry.get("raw_text", "")

        # Detect uncertain entries (containing '?')
        is_uncertain = "?" in set1 or "?" in set2 or entry.get("confidence") == "low"

        # 1. Check Set 1 (Must be 2-4 digits, or digits+? for uncertain reading)
        if not re.match(r"^[\d?]{2,4}$", set1):
            errors.append(f"[{col_name}] ชุด 1 '{set1}' ไม่ถูกต้อง (ต้องเป็นตัวเลข 2-4 หลักเท่านั้น)")

        # 2. Check Set 3 (Must be either empty, 'ก3', or 'ก6')
        if set3 and set3 not in ["ก3", "ก6"]:
            errors.append(f"[{col_name}] ชุด 3 '{set3}' ไม่ถูกต้อง (ต้องเป็น 'ก3' หรือ 'ก6' เท่านั้น)")

        # 3. Check Set 2 (allow '?' in uncertain entries)
        if set3 in ["ก3", "ก6"]:
            # Rule: If Set 3 is present, Set 2 MUST be single digits only, NO 'x'
            if not re.match(r"^[\d?]+$", set2):
                errors.append(f"[{col_name}] รายการ '{raw}' มี '{set3}' แต่ชุด 2 '{set2}' มี 'x' หรือไม่ใช่ตัวเลขเดี่ยว (ผิดกฎ)")
        else:
            # Rule: If no Set 3, Set 2 can be single number or number x number (allows '?' in uncertain)
            is_single_number = bool(re.match(r"^[\d?]+$", set2))
            is_nxn = bool(re.match(r"^[\d?]+x[\d?]+$", set2))
            if not (is_single_number or is_nxn):
                errors.append(f"[{col_name}] ชุด 2 '{set2}' ในรายการ '{raw}' ไม่ตรงรูปแบบ (ต้องเป็นตัวเลขเดี่ยว หรือ NxN)")

        is_valid = len(errors) == 0
        return is_valid, errors

    @classmethod
    def validate_document(cls, ocr_result: Dict) -> Dict:
        columns = ocr_result.get("columns", {})
        all_errors = []
        validated_columns = {}
        valid_count = 0
        total_count = 0
        uncertain_count = 0

        for col_key, col_label in [("top", "บน"), ("bottom", "ล่าง"), ("top_bottom", "บนล่าง")]:
            items = columns.get(col_key, [])
            validated_items = []
            for item in items:
                total_count += 1
                is_valid, errors = cls.validate_entry(item, col_label)
                item_copy = dict(item)
                item_copy["is_valid"] = is_valid
                item_copy["errors"] = errors
                # Tag uncertain items (has '?' or confidence=low)
                is_uncertain = (
                    "?" in str(item.get("set1", "")) or
                    "?" in str(item.get("set2", "")) or
                    item.get("confidence") == "low"
                )
                item_copy["is_uncertain"] = is_uncertain
                validated_items.append(item_copy)
                if is_valid:
                    valid_count += 1
                else:
                    all_errors.extend(errors)
                if is_uncertain:
                    uncertain_count += 1
            validated_columns[col_key] = validated_items

        return {
            "is_all_valid": len(all_errors) == 0,
            "total_items": total_count,
            "valid_items": valid_count,
            "uncertain_items": uncertain_count,
            "errors": all_errors,
            "validated_columns": validated_columns
        }
