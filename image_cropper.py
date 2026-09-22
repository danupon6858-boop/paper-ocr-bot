# -*- coding: utf-8 -*-
"""
image_cropper.py
โมดูลสำหรับตัดภาพชิ้นส่วนลายมือ (Image Snippets) จากพิกัด 2D Bounding Box [ymin, xmin, ymax, xmax] (0-1000)
- บน Render/Linux: ใช้ Pillow (PIL)
- บน macOS (หากไม่มี Pillow): ใช้ sips อัตโนมัติ
"""

import os
import io
import subprocess
from typing import List, Optional, Tuple

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def get_image_dimensions(image_bytes: bytes, image_path: str = "") -> Tuple[int, int]:
    """Returns (width, height) of an image."""
    if HAS_PIL:
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                return img.size
        except Exception:
            pass

    if image_path and os.path.exists(image_path):
        try:
            res = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", image_path],
                                 capture_output=True, text=True, timeout=5)
            w, h = 1000, 1000
            for line in res.stdout.splitlines():
                if "pixelWidth:" in line:
                    w = int(line.split(":")[-1].strip())
                elif "pixelHeight:" in line:
                    h = int(line.split(":")[-1].strip())
            return w, h
        except Exception:
            pass

    return 1000, 1000


def crop_snippet_from_bytes(image_bytes: bytes, box_2d: List[int], output_path: str, source_path: str = "") -> bool:
    """
    Crops a sub-region defined by box_2d: [ymin, xmin, ymax, xmax] (0-1000 scale).
    Adds 15% padding around the box so nearby context is visible.
    Saves to output_path.
    """
    if not box_2d or len(box_2d) < 4:
        return False

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    ymin, xmin, ymax, xmax = [max(0, min(1000, int(v))) for v in box_2d[:4]]

    if HAS_PIL:
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                w, h = img.size
                left = int(xmin * w / 1000.0)
                top = int(ymin * h / 1000.0)
                right = int(xmax * w / 1000.0)
                bottom = int(ymax * h / 1000.0)

                # Add padding
                pad_x = max(15, int((right - left) * 0.18))
                pad_y = max(15, int((bottom - top) * 0.18))

                left = max(0, left - pad_x)
                top = max(0, top - pad_y)
                right = min(w, right + pad_x)
                bottom = min(h, bottom + pad_y)

                if right <= left or bottom <= top:
                    return False

                cropped = img.crop((left, top, right, bottom))
                cropped.convert("RGB").save(output_path, "JPEG", quality=90)
                return True
        except Exception as e:
            print(f"[Image Cropper] PIL crop error: {e}")

    # Fallback to sips if on macOS
    temp_src = source_path if (source_path and os.path.exists(source_path)) else None
    temp_created = False
    if not temp_src and image_bytes:
        temp_src = output_path + ".tmp.jpg"
        try:
            with open(temp_src, "wb") as f:
                f.write(image_bytes)
            temp_created = True
        except Exception:
            temp_src = None

    if temp_src and os.path.exists(temp_src):
        try:
            w, h = get_image_dimensions(image_bytes, temp_src)
            left = int(xmin * w / 1000.0)
            top = int(ymin * h / 1000.0)
            right = int(xmax * w / 1000.0)
            bottom = int(ymax * h / 1000.0)

            crop_w = max(50, right - left + 30)
            crop_h = max(30, bottom - top + 30)
            offset_x = max(0, left - 15)
            offset_y = max(0, top - 15)

            # Copy source to output first
            with open(temp_src, "rb") as sf, open(output_path, "wb") as df:
                df.write(sf.read())

            subprocess.run([
                "sips",
                "--cropToHeightWidthPixels", str(crop_h), str(crop_w),
                "--cropOffsetOffsetYX", str(offset_y), str(offset_x),
                output_path
            ], capture_output=True, timeout=5)
            if temp_created and os.path.exists(temp_src):
                os.remove(temp_src)
            return True
        except Exception as e:
            print(f"[Image Cropper] sips crop error: {e}")
            if temp_created and os.path.exists(temp_src):
                os.remove(temp_src)

    return False
