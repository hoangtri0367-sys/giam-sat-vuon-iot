"""
image_analysis.py
-----------------
Module phân tích ảnh cải ngọt cho hệ thống IoT.
Nhận ảnh top-down từ ESP32-CAM (chụp từ 42cm), chậu 80×40cm.
Layout: 18 cây, 3 hàng × 6 cột.

Tích hợp vào Flask: gọi analyze_image(image_bytes) -> dict
"""

import cv2
import numpy as np
from datetime import datetime
import math

# ────────────────────────────────────────────────
# CẤU HÌNH HỆ THỐNG (chỉnh ở đây nếu cần)
# ────────────────────────────────────────────────

# Kích thước thực tế (cm)
TRAY_WIDTH_CM  = 80.0
TRAY_HEIGHT_CM = 40.0
CAMERA_HEIGHT_CM = 42.0   # camera cách mặt đất

# Lưới cây
ROWS = 3
COLS = 6
TOTAL_PLANTS = ROWS * COLS  # 18

# Tỉ lệ pixel/cm sẽ tự tính sau khi detect chậu
# Giá trị mặc định dự phòng (dùng khi không detect được chậu)
DEFAULT_PX_PER_CM = None   # None = tự tính từ ảnh

# Màu chậu trắng → dùng để crop ROI
TRAY_COLOR_LOWER = np.array([0,   0, 180])   # HSV
TRAY_COLOR_UPPER = np.array([180, 40, 255])

# Ngưỡng màu lá (HSV)
HEALTHY_GREEN_LOWER = np.array([35,  40,  40])
HEALTHY_GREEN_UPPER = np.array([90, 255, 255])

YELLOW_LOWER = np.array([20, 50,  50])
YELLOW_UPPER = np.array([34, 255, 255])

BROWN_LOWER  = np.array([5,  50,  20])
BROWN_UPPER  = np.array([19, 200, 150])

# Ngưỡng phát hiện cây trong ô (% diện tích ô có màu xanh)
CELL_PLANT_THRESHOLD = 0.04   # 4% → coi như có cây
CELL_SICK_THRESHOLD  = 0.30   # >30% vàng/nâu trong pixel lá → cảnh báo


# ────────────────────────────────────────────────
# HÀM CHÍNH
# ────────────────────────────────────────────────

def analyze_image(image_bytes: bytes) -> dict:
    """
    Phân tích một ảnh từ ESP32-CAM.

    Parameters
    ----------
    image_bytes : bytes  – raw JPEG bytes từ ESP32-CAM

    Returns
    -------
    dict với các key:
        timestamp, plant_count, missing_positions,
        cell_results (list 18 phần tử),
        avg_canopy_cm2, health_summary, disease_warning,
        debug_image_bytes (JPEG bytes ảnh đã annotate)
    """
    # ── 1. Decode ảnh ──────────────────────────────
    nparr = np.frombuffer(image_bytes, np.uint8)
    img   = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return {"error": "Không decode được ảnh"}

    H, W = img.shape[:2]

    # ── 2. Crop ROI chậu ────────────────────────────
    tray_rect, px_per_cm = _detect_tray(img)
    if tray_rect is None:
        # Fallback: dùng toàn ảnh, tính tỉ lệ từ chiều rộng
        tray_rect = (0, 0, W, H)
        px_per_cm = W / TRAY_WIDTH_CM

    x0, y0, tw, th = tray_rect
    roi = img[y0:y0+th, x0:x0+tw].copy()

    # ── 3. Chia lưới 3×6 ────────────────────────────
    cell_w = tw / COLS
    cell_h = th / ROWS

    cell_results = []
    for r in range(ROWS):
        for c in range(COLS):
            cx0 = int(c * cell_w)
            cy0 = int(r * cell_h)
            cx1 = int((c + 1) * cell_w)
            cy1 = int((r + 1) * cell_h)
            cell_img = roi[cy0:cy1, cx0:cx1]

            result = _analyze_cell(cell_img, px_per_cm, row=r, col=c)
            result["grid_row"] = r + 1
            result["grid_col"] = c + 1
            result["position_label"] = f"R{r+1}C{c+1}"
            cell_results.append(result)

    # ── 4. Tổng hợp ─────────────────────────────────
    plant_count     = sum(1 for cr in cell_results if cr["has_plant"])
    missing         = [cr["position_label"] for cr in cell_results if not cr["has_plant"]]
    sick_cells      = [cr["position_label"] for cr in cell_results if cr.get("health") in ("yellow", "diseased")]
    canopy_vals     = [cr["canopy_cm2"] for cr in cell_results if cr["has_plant"]]
    avg_canopy      = round(float(np.mean(canopy_vals)), 2) if canopy_vals else 0.0

    disease_warning = len(sick_cells) > 0
    health_summary  = _summarize_health(cell_results)

    # ── 5. Vẽ annotation ────────────────────────────
    annotated = _draw_annotations(roi.copy(), cell_results, cell_w, cell_h)
    _, enc = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
    debug_bytes = enc.tobytes()

    return {
        "timestamp":          datetime.now().isoformat(),
        "plant_count":        plant_count,
        "missing_positions":  missing,
        "cell_results":       cell_results,
        "avg_canopy_cm2":     avg_canopy,
        "health_summary":     health_summary,
        "disease_warning":    disease_warning,
        "sick_positions":     sick_cells,
        "debug_image_bytes":  debug_bytes,
        "px_per_cm":          round(px_per_cm, 2),
    }


# ────────────────────────────────────────────────
# DETECT CHẬU TRẮNG
# ────────────────────────────────────────────────

def _detect_tray(img):
    """
    Tìm bounding rect của chậu màu trắng.
    Trả về (x, y, w, h), px_per_cm  hoặc  None, fallback_scale
    """
    hsv  = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, TRAY_COLOR_LOWER, TRAY_COLOR_UPPER)

    # Morphology để lấy vùng lớn nhất
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    mask   = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask   = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  kernel)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None, img.shape[1] / TRAY_WIDTH_CM

    largest = max(contours, key=cv2.contourArea)
    if cv2.contourArea(largest) < 0.1 * img.shape[0] * img.shape[1]:
        return None, img.shape[1] / TRAY_WIDTH_CM

    rect = cv2.boundingRect(largest)   # (x, y, w, h)
    px_per_cm = rect[2] / TRAY_WIDTH_CM
    return rect, px_per_cm


# ────────────────────────────────────────────────
# PHÂN TÍCH TỪNG Ô
# ────────────────────────────────────────────────

def _analyze_cell(cell_img, px_per_cm: float, row: int, col: int) -> dict:
    """Phân tích một ô trong lưới."""
    if cell_img.size == 0:
        return {"has_plant": False, "health": "unknown", "canopy_cm2": 0.0,
                "green_ratio": 0.0, "yellow_ratio": 0.0, "brown_ratio": 0.0}

    hsv = cv2.cvtColor(cell_img, cv2.COLOR_BGR2HSV)
    total_px = cell_img.shape[0] * cell_img.shape[1]

    # Mask từng màu
    m_green  = cv2.inRange(hsv, HEALTHY_GREEN_LOWER, HEALTHY_GREEN_UPPER)
    m_yellow = cv2.inRange(hsv, YELLOW_LOWER, YELLOW_UPPER)
    m_brown  = cv2.inRange(hsv, BROWN_LOWER,  BROWN_UPPER)

    # Morphology nhỏ để lọc noise
    k3 = np.ones((3, 3), np.uint8)
    m_green  = cv2.morphologyEx(m_green,  cv2.MORPH_OPEN, k3)
    m_yellow = cv2.morphologyEx(m_yellow, cv2.MORPH_OPEN, k3)
    m_brown  = cv2.morphologyEx(m_brown,  cv2.MORPH_OPEN, k3)

    green_px  = int(cv2.countNonZero(m_green))
    yellow_px = int(cv2.countNonZero(m_yellow))
    brown_px  = int(cv2.countNonZero(m_brown))

    green_ratio  = green_px  / total_px
    yellow_ratio = yellow_px / total_px
    brown_ratio  = brown_px  / total_px
    sick_ratio   = (yellow_px + brown_px) / max(green_px + yellow_px + brown_px, 1)

    has_plant = green_ratio >= CELL_PLANT_THRESHOLD

    # Diện tích tán lá (cm²) từ pixel xanh
    canopy_px2 = green_px
    canopy_cm2 = round(canopy_px2 / (px_per_cm ** 2), 2) if px_per_cm > 0 else 0.0

    # Đánh giá sức khỏe
    if not has_plant:
        health = "missing"
    elif sick_ratio > CELL_SICK_THRESHOLD and brown_ratio > 0.05:
        health = "diseased"
    elif sick_ratio > CELL_SICK_THRESHOLD:
        health = "yellow"
    else:
        health = "healthy"

    return {
        "has_plant":    has_plant,
        "health":       health,
        "canopy_cm2":   canopy_cm2,
        "green_ratio":  round(green_ratio, 4),
        "yellow_ratio": round(yellow_ratio, 4),
        "brown_ratio":  round(brown_ratio, 4),
        "sick_ratio":   round(sick_ratio, 4),
    }


# ────────────────────────────────────────────────
# VẼ ANNOTATION
# ────────────────────────────────────────────────

def _draw_annotations(roi, cell_results, cell_w, cell_h):
    COLOR_MAP = {
        "healthy":  (0, 200, 0),
        "yellow":   (0, 200, 255),
        "diseased": (0, 0, 255),
        "missing":  (180, 180, 180),
        "unknown":  (100, 100, 100),
    }

    for i, cr in enumerate(cell_results):
        r = cr["grid_row"] - 1
        c = cr["grid_col"] - 1
        x0 = int(c * cell_w);  y0 = int(r * cell_h)
        x1 = int((c+1)*cell_w); y1 = int((r+1)*cell_h)

        color = COLOR_MAP.get(cr["health"], (100, 100, 100))
        cv2.rectangle(roi, (x0, y0), (x1, y1), color, 2)

        label = cr["position_label"]
        canopy = f"{cr['canopy_cm2']}cm2"
        cv2.putText(roi, label,  (x0+4, y0+16), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
        cv2.putText(roi, canopy, (x0+4, y0+30), cv2.FONT_HERSHEY_SIMPLEX, 0.35, color, 1)

        # Icon trạng thái
        icon = "✓" if cr["health"] == "healthy" else ("?" if cr["health"] == "missing" else "!")
        cv2.putText(roi, icon, (x1-18, y0+16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

    # Chú thích legend
    legend = [("Healthy","(0,200,0)"),("Yellow","(0,200,255)"),("Diseased","(0,0,255)"),("Missing","(180,180,180)")]
    for idx, (txt, _) in enumerate(legend):
        c = list(COLOR_MAP.values())[idx]
        cv2.putText(roi, f"■ {txt}", (4, roi.shape[0]-10-idx*16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, c, 1)

    return roi


# ────────────────────────────────────────────────
# TỔNG HỢP SỨC KHỎE
# ────────────────────────────────────────────────

def _summarize_health(cell_results: list) -> dict:
    counts = {"healthy": 0, "yellow": 0, "diseased": 0, "missing": 0}
    for cr in cell_results:
        h = cr.get("health", "missing")
        if h in counts:
            counts[h] += 1
    total_present = TOTAL_PLANTS - counts["missing"]
    counts["total_present"] = total_present
    counts["health_score"] = round(
        counts["healthy"] / TOTAL_PLANTS * 100, 1
    ) if TOTAL_PLANTS > 0 else 0.0
    return counts
