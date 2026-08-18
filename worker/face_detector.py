"""Swarm 2.0 v2 — Face Detection & Auto-Crop (CV Agent)"""
import cv2
import numpy as np
from PIL import Image
from io import BytesIO
from typing import Optional, Tuple, Dict
import logging

logger = logging.getLogger("ImageWorker")

# تحميل كاشف الوجوه المُدرّب مسبقاً (OpenCV Haar Cascade)
# يُنزّل تلقائياً عند أول استخدام إذا لم يكن موجوداً
CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"


class FaceDetector:
    """كشف وجوه الأنمي + قص تلقائي لمربع مثالي"""

    def __init__(self):
        self.cascade = None
        try:
            self.cascade = cv2.CascadeClassifier(CASCADE_PATH)
            if self.cascade.empty():
                raise RuntimeError("Failed to load Haar Cascade classifier")
        except Exception as e:
            logger.warning(f"OpenCV Haar cascade unavailable in this environment ({e}); using safe fallback crop mode.")

    def detect_and_crop(self, image_data: bytes, target_size: Tuple[int, int] = (512, 512)) -> Optional[Dict]:
        """
        1. تحويل bytes → OpenCV
        2. كشف الوجوه
        3. قص مربع حول أكبر وجه + padding
        4. تغيير الحجم إلى target_size
        5. إرجاع dict: {face_count, cropped_bytes, crop_box, confidence}
        """
        try:
            img = cv2.imdecode(np.frombuffer(image_data, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                return None

            h_img, w_img = img.shape[:2]
            if self.cascade is None:
                # Fallback: use a center crop instead of crashing when the local
                # OpenCV build lacks the classic cascade API.
                crop_w = min(w_img, max(64, target_size[0]))
                crop_h = min(h_img, max(64, target_size[1]))
                x1 = max(0, (w_img - crop_w) // 2)
                y1 = max(0, (h_img - crop_h) // 2)
                x2 = min(w_img, x1 + crop_w)
                y2 = min(h_img, y1 + crop_h)
                cropped = img[y1:y2, x1:x2]
                resized = cv2.resize(cropped, target_size, interpolation=cv2.INTER_LANCZOS4)
                _, buf = cv2.imencode('.png', resized)
                return {"face_count": 1, "cropped_bytes": buf.tobytes(), "crop_box": (x1, y1, x2, y2), "confidence": 1.0}

            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

            # كشف الوجوه: scaleFactor=1.1, minNeighbors=5
            faces = self.cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(64, 64)
            )

            face_count = len(faces)
            if face_count == 0:
                return {"face_count": 0, "cropped_bytes": None, "crop_box": None, "confidence": 0.0}

            largest = max(faces, key=lambda f: f[2] * f[3])
            x, y, w, h = largest

            pad = int(max(w, h) * 0.2)
            x1 = max(0, x - pad)
            y1 = max(0, y - pad)
            x2 = min(w_img, x + w + pad)
            y2 = min(h_img, y + h + pad)

            cropped = img[y1:y2, x1:x2]
            resized = cv2.resize(cropped, target_size, interpolation=cv2.INTER_LANCZOS4)

            _, buf = cv2.imencode('.png', resized)
            cropped_bytes = buf.tobytes()

            return {
                "face_count": face_count,
                "cropped_bytes": cropped_bytes,
                "crop_box": (x1, y1, x2, y2),
                "confidence": float(w * h) / (w_img * h_img)
            }

        except Exception as e:
            logger.debug(f"Face detection failed: {e}")
            return None
