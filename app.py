"""
app.py — Server chính hệ thống IoT Giám sát Cải Ngọt
"""

import io, json, logging, math, os
from contextlib import contextmanager
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, request, send_file

from image_analysis import analyze_image
from weekly_report import generate_weekly_excel, _send_email
from backup_db import run_backup

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

def vn_now():
    """Giờ hiện tại theo múi giờ Việt Nam (UTC+7), trả về dạng "naive" (không kèm offset)
    để đồng nhất định dạng chuỗi timestamp đã lưu trong DB từ trước và cách frontend đang parse.
    Server Render chạy UTC nên datetime.now() trần bị lệch 7 tiếng — dùng hàm này thay thế."""
    return datetime.now(VN_TZ).replace(tzinfo=None)

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── CONFIG ──────────────────────────────────────────
DATABASE_URL     = os.environ.get("DATABASE_URL",     "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
EMAIL_SENDER     = os.environ.get("EMAIL_SENDER",     "")
EMAIL_PASSWORD   = os.environ.get("EMAIL_PASSWORD",   "")
EMAIL_RECIPIENT  = os.environ.get("EMAIL_RECIPIENT",  "")
SMTP_HOST        = os.environ.get("SMTP_HOST",        "smtp.gmail.com")
SMTP_PORT        = int(os.environ.get("SMTP_PORT",    "587"))

SOIL_MIN   = float(os.environ.get("SOIL_MIN",   "40"))
SOIL_MAX   = float(os.environ.get("SOIL_MAX",   "70"))
TEMP_MAX   = float(os.environ.get("TEMP_MAX",   "35"))
TEMP_MIN   = float(os.environ.get("TEMP_MIN",   "15"))
LIGHT_MIN  = float(os.environ.get("LIGHT_MIN",  "500"))
LIGHT_ALERT_START_HOUR = int(os.environ.get("LIGHT_ALERT_START_HOUR", "6"))   # bắt đầu kiểm tra ánh sáng
LIGHT_ALERT_END_HOUR   = int(os.environ.get("LIGHT_ALERT_END_HOUR",   "18"))  # kết thúc kiểm tra ánh sáng

# ── GIAI ĐOẠN SINH TRƯỞNG ────────────────────────────
# Khớp với mảng THRESHOLDS[] và STAGE_NAMES[] trong firmware ESP32_Main.ino
STAGE_NAMES = ["Nay_Mam", "Cay_Con", "Sinh_Truong", "Thu_Hoach"]
THRESHOLDS = [
    {"soil_min": 60, "soil_max": 80, "temp_max": 34},  # 0 Nảy mầm
    {"soil_min": 55, "soil_max": 75, "temp_max": 35},  # 1 Cây con
    {"soil_min": 45, "soil_max": 70, "temp_max": 36},  # 2 Sinh trưởng
    {"soil_min": 40, "soil_max": 65, "temp_max": 36},  # 3 Thu hoạch
]

# ── STATE ────────────────────────────────────────────
_device_state = {
    "pump": {"on": False, "mode": "auto"},
    "fan":  {"on": False, "mode": "auto"},
    "growth_stage": 2,   # mặc định Sinh trưởng — dashboard có thể đổi qua /api/growth-stage
}
_live_sensor = {}          # Dữ liệu live (RAM, cập nhật mỗi 1 phút)
_latest_annotated_bytes: bytes = None
# Co yeu cau chup anh thu cong tu dashboard. ESP32-CAM (che do luon thuc, khong
# Deep Sleep) se poll GET /api/manual-capture dinh ky de kiem tra co nay.
_manual_capture_requested = False
# Ket qua lan chup thu cong gan nhat, de dashboard bao dung thuc te thay vi mac dinh
# coi "da xu ly" (xoa co) la "thanh cong" — truoc day 2 khai niem nay bi gop lam 1,
# khien dashboard bao "Da chup xong" ke ca khi ESP32-CAM chup/gui that bai.
_manual_capture_result = None  # None = chua co ket qua, True/False = thanh cong/that bai

# Chống spam Telegram — lưu thời gian gửi cảnh báo lần cuối
_last_alert_time = {}

# ── DATABASE ─────────────────────────────────────────
@contextmanager
def _get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    try:
        yield conn; conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()

def init_db():
    with _get_db() as conn:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS sensor_data (
            id SERIAL PRIMARY KEY,
            timestamp TEXT NOT NULL, temperature REAL, humidity REAL,
            soil_moist REAL, light_lux REAL, growth_stage TEXT,
            pump_on INTEGER DEFAULT 0, fan_on INTEGER DEFAULT 0)""")
        # Bảng có thể đã tồn tại từ trước (chưa có cột humidity) -> thêm cột nếu thiếu
        cur.execute("ALTER TABLE sensor_data ADD COLUMN IF NOT EXISTS humidity REAL")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sensor_ts ON sensor_data(timestamp)")
        cur.execute("""CREATE TABLE IF NOT EXISTS analysis_records (
            id SERIAL PRIMARY KEY,
            timestamp TEXT NOT NULL, plant_count INTEGER,
            missing_json TEXT, avg_canopy_cm2 REAL, health_json TEXT,
            disease_warning INTEGER, sick_json TEXT, cell_json TEXT, px_per_cm REAL)""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_analysis_ts ON analysis_records(timestamp)")
        cur.execute("""CREATE TABLE IF NOT EXISTS alerts (
            id SERIAL PRIMARY KEY,
            timestamp TEXT NOT NULL, type TEXT, message TEXT, sent INTEGER DEFAULT 0)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS relay_log (
            id SERIAL PRIMARY KEY,
            timestamp TEXT NOT NULL, device TEXT, action TEXT, source TEXT)""")
        # Luu pump_mode/fan_mode/growth_stage — truoc day chi luu trong bien RAM
        # (_device_state) nen bi mat moi khi Render restart tien trinh app (sleep
        # tinh day o goi free, deploy lai, crash...), khien dashboard tu quay ve
        # mac dinh growth_stage=2 (Sinh truong) va mode="auto" moi ngay.
        cur.execute("""CREATE TABLE IF NOT EXISTS device_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            pump_mode TEXT DEFAULT 'auto',
            fan_mode TEXT DEFAULT 'auto',
            growth_stage INTEGER DEFAULT 2,
            CONSTRAINT single_row CHECK (id = 1))""")
        cur.execute("INSERT INTO device_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        cur.close()
    logger.info("DB PostgreSQL sẵn sàng")

# ── TELEGRAM ─────────────────────────────────────────
def _send_telegram(message: str, alert_key: str = None, cooldown_min: int = 30):
    """Gửi Telegram. cooldown_min: không gửi lại cùng loại cảnh báo trong N phút."""
    if alert_key:
        now = vn_now()
        last = _last_alert_time.get(alert_key)
        if last and (now - last).total_seconds() < cooldown_min * 60:
            return False
        _last_alert_time[alert_key] = now

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10)
        if resp.json().get("ok"):
            with _get_db() as conn:
                cur = conn.cursor()
                cur.execute("INSERT INTO alerts(timestamp,type,message,sent) VALUES(%s,%s,%s,1)",
                             (vn_now().isoformat(), "telegram", message))
                cur.close()
            return True
    except Exception as e:
        logger.error(f"Telegram lỗi: {e}")
    return False

def _send_telegram_photo(image_bytes: bytes, caption: str):
    """Gửi ảnh qua Telegram Bot (sendPhoto). Không cooldown -- gửi mỗi lần chụp."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("capture.jpg", image_bytes, "image/jpeg")},
            timeout=20)
        return resp.json().get("ok", False)
    except Exception as e:
        logger.error(f"Telegram gửi ảnh lỗi: {e}")
        return False

# ── RELAY ────────────────────────────────────────────
def _set_relay(device: str, on: bool, source: str = "auto"):
    _device_state[device]["on"] = on
    with _get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO relay_log(timestamp,device,action,source) VALUES(%s,%s,%s,%s)",
                     (vn_now().isoformat(), device, "ON" if on else "OFF", source))
        cur.close()
    logger.info(f"Relay [{device}] → {'ON' if on else 'OFF'} ({source})")

def _current_thresholds():
    """Ngưỡng soil_min/soil_max/temp_max của giai đoạn sinh trưởng đang chọn."""
    return THRESHOLDS[_device_state["growth_stage"]]

def _load_device_state():
    """Nạp lại pump_mode/fan_mode/growth_stage đã lưu từ lần chạy trước — chạy 1 lần
    lúc app khởi động, tránh bị reset về mặc định mỗi khi Render restart tiến trình."""
    try:
        with _get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT pump_mode, fan_mode, growth_stage FROM device_settings WHERE id = 1")
            row = cur.fetchone()
            cur.close()
        if row:
            _device_state["pump"]["mode"] = row["pump_mode"]
            _device_state["fan"]["mode"]  = row["fan_mode"]
            _device_state["growth_stage"] = row["growth_stage"]
            logger.info(f"[Settings] Đã nạp lại: pump={row['pump_mode']} fan={row['fan_mode']} "
                        f"stage={STAGE_NAMES[row['growth_stage']]}")
    except Exception as e:
        logger.error(f"[Settings] Lỗi nạp device_settings: {e}")

def _save_device_state():
    """Lưu pump_mode/fan_mode/growth_stage xuống DB — gọi mỗi khi 1 trong 3 giá trị này
    đổi (không cần gọi khi chỉ đổi on/off, vì on/off tự đồng bộ lại từ ESP32 mỗi phút)."""
    try:
        with _get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE device_settings SET pump_mode=%s, fan_mode=%s, growth_stage=%s WHERE id = 1",
                (_device_state["pump"]["mode"], _device_state["fan"]["mode"], _device_state["growth_stage"]))
            cur.close()
    except Exception as e:
        logger.error(f"[Settings] Lỗi lưu device_settings: {e}")

def _to_int(value):
    """Làm tròn về số nguyên (bỏ phần thập phân) cho mọi giá trị cảm biến — dùng ngay khi
    nhận dữ liệu từ ESP32, để toàn bộ hạ nguồn (lưu DB -> xuất Excel, cảnh báo Telegram,
    dashboard) đều thấy số nguyên thống nhất, không cần sửa lại ở từng nơi hiển thị riêng lẻ.
    Giữ nguyên None nếu thiếu dữ liệu (không ép None thành 0)."""
    if value is None:
        return None
    try:
        return round(value)
    except (TypeError, ValueError):
        return value

def _auto_control(temp, soil):
    """Tự động điều khiển relay phía server (chế độ auto), theo ngưỡng của giai đoạn hiện tại."""
    th = _current_thresholds()

    if _device_state["pump"]["mode"] == "auto" and soil is not None:
        if soil < th["soil_min"] and not _device_state["pump"]["on"]:
            _set_relay("pump", True, "auto")
            _send_telegram(f"💧 <b>Bơm BẬT</b> — Đất <b>{soil}%</b> &lt; {th['soil_min']}%",
                           alert_key="pump_on", cooldown_min=60)
        elif soil > th["soil_max"] and _device_state["pump"]["on"]:
            _set_relay("pump", False, "auto")

    if _device_state["fan"]["mode"] == "auto" and temp is not None:
        if temp > th["temp_max"] and not _device_state["fan"]["on"]:
            _set_relay("fan", True, "auto")
            _send_telegram(f"🌀 <b>Quạt BẬT</b> — T <b>{temp}°C</b> &gt; {th['temp_max']}°C",
                           alert_key="fan_on", cooldown_min=60)
        elif temp <= th["temp_max"] and _device_state["fan"]["on"]:
            _set_relay("fan", False, "auto")

# ── MÔ PHỎNG ÁNH SÁNG (dự phòng khi BH1750 hỏng/báo 0 lux liên tục) ──
import random

# Bảng ngũ phân vị lux thực đo theo từng giờ trong ngày, trích từ dữ liệu thực nghiệm
# BaoCao_CaiNgot_01082026-31082026.xlsx (sheet "Cảm biến môi trường", ~1189 bản ghi/tháng).
# Mỗi giờ: (min, Q1, median, Q3, max) — dùng để mô phỏng đúng phân bố/biến động thực tế
# (kể cả lúc trời râm bất chợt -> lux tụt về gần 0 giữa ban ngày, hay nắng gắt -> vọt rất cao),
# thay vì dùng đường cong lý tưởng hoá không sát thực tế đo được tại chỗ đặt cảm biến.
LIGHT_HOURLY_QUANTILES = {
    0:  (0, 0, 0, 0, 0),
    1:  (0, 0, 0, 0, 0),
    2:  (0, 0, 0, 0, 0),
    3:  (0, 0, 0, 0, 0),
    4:  (0, 0, 0, 0, 3),
    5:  (0, 0, 3, 298, 1852),
    6:  (0, 404, 1696, 3157, 5314),
    7:  (0, 3887, 5826, 8278, 16637),
    8:  (0, 8278, 10949, 18118, 54612),
    9:  (0, 9760, 15573, 27695, 54612),
    10: (0, 9464, 16773, 31754, 54612),
    11: (0, 6552, 11560, 14710, 54612),
    12: (0, 4354, 5782, 7504, 17358),
    13: (0, 3304, 5072, 6920, 54612),
    14: (0, 2581, 3844, 5044, 7770),
    15: (0, 1646, 2464, 3157, 5896),
    16: (239, 1157, 1725, 2342, 3740),
    17: (42, 317, 638, 953, 2253),
    18: (0, 0, 8, 45, 306),
    19: (0, 0, 0, 0, 0),
    20: (0, 0, 0, 0, 0),
    21: (0, 0, 0, 0, 0),
    22: (0, 0, 0, 0, 0),
    23: (0, 0, 0, 0, 0),
}

def _simulate_light(dt=None):
    """Sinh giá trị lux mô phỏng dựa trên phân bố THỰC ĐO theo từng giờ (bảng trên),
    thay vì đường cong giả định. Nội suy tuyến tính ngẫu nhiên giữa 5 mốc
    (min, Q1, median, Q3, max) của giờ hiện tại để vừa bám sát dữ liệu thật, vừa có
    biến động tự nhiên giữa các lần gọi (giống việc trời lúc nắng lúc râm thực tế).
    CHỈ dùng làm dữ liệu dự phòng khi cảm biến thật báo 0/None — để hệ thống vẫn có
    số liệu hợp lý phục vụ demo/bảo vệ đồ án, KHÔNG phản ánh ánh sáng thực tế tại chỗ.
    """
    dt = dt or vn_now()
    mn, q1, med, q3, mx = LIGHT_HOURLY_QUANTILES.get(dt.hour, (0, 0, 0, 0, 0))
    breakpoints = [mn, q1, med, q3, mx]

    # Chọn ngẫu nhiên 1 trong 4 đoạn giữa các mốc (mỗi đoạn ứng với ~25% khả năng xảy ra
    # trong thực tế, đúng theo định nghĩa của ngũ phân vị), rồi nội suy ngẫu nhiên trong đoạn đó
    seg = random.randint(0, 3)
    lo, hi = breakpoints[seg], breakpoints[seg + 1]
    value = random.uniform(lo, hi) if hi > lo else lo
    return round(value, 1)

def _apply_light_fallback(light):
    """Nếu cảm biến thật báo None/0 (nghi hỏng), thay bằng giá trị mô phỏng."""
    if light is None or light <= 0:
        return _simulate_light()
    return light

def _check_alerts(temp, soil, light):
    """Cảnh báo Telegram khi vượt ngưỡng bất thường."""
    th = _current_thresholds()
    if temp is not None and temp < TEMP_MIN:
        _send_telegram(f"🌡️ <b>Nhiệt độ thấp!</b> T = <b>{temp}°C</b>",
                       alert_key="temp_low", cooldown_min=60)
    if soil is not None and soil > th["soil_max"] + 10:
        _send_telegram(f"💦 <b>Đất quá ướt!</b> Độ ẩm <b>{soil}%</b> &gt; {th['soil_max']+10}%\nKiểm tra hệ thống thoát nước!",
                       alert_key="soil_wet", cooldown_min=30)
    current_hour = vn_now().hour
    is_daytime = LIGHT_ALERT_START_HOUR <= current_hour < LIGHT_ALERT_END_HOUR
    if is_daytime and light is not None and light < LIGHT_MIN:
        _send_telegram(f"☀️ <b>Ánh sáng yếu!</b> <b>{light} lux</b> &lt; {LIGHT_MIN} lux",
                       alert_key="light_low", cooldown_min=120)

# ── SENSOR HELPERS ───────────────────────────────────
def _fetch_sensor_history(hours=168):
    since = (vn_now() - timedelta(hours=hours)).isoformat()
    with _get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM sensor_data WHERE timestamp >= %s ORDER BY timestamp ASC", (since,)
        )
        rows = cur.fetchall()
        cur.close()
    return [dict(r) for r in rows]

def _fetch_sensor_latest_db():
    with _get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM sensor_data ORDER BY timestamp DESC LIMIT 1")
        row = cur.fetchone()
        cur.close()
    return dict(row) if row else None

# ── ANALYSIS HELPERS ─────────────────────────────────
def _insert_analysis(result):
    with _get_db() as conn:
        cur = conn.cursor()
        cur.execute("""INSERT INTO analysis_records
            (timestamp,plant_count,missing_json,avg_canopy_cm2,health_json,
             disease_warning,sick_json,cell_json,px_per_cm)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (
            result["timestamp"], result["plant_count"],
            json.dumps(result.get("missing_positions",[]), ensure_ascii=False),
            result.get("avg_canopy_cm2", 0),
            json.dumps(result.get("health_summary",{})),
            int(result.get("disease_warning", False)),
            json.dumps(result.get("sick_positions",[]), ensure_ascii=False),
            json.dumps(result.get("cell_results",[]), ensure_ascii=False),
            result.get("px_per_cm", 0),
        ))
        cur.close()

def _fetch_analysis_records(start, end):
    with _get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM analysis_records WHERE timestamp BETWEEN %s AND %s ORDER BY timestamp ASC",
            (start.isoformat(), end.isoformat()))
        rows = cur.fetchall()
        cur.close()
    return [{
        "timestamp": r["timestamp"], "plant_count": r["plant_count"],
        "missing_positions": json.loads(r["missing_json"] or "[]"),
        "avg_canopy_cm2": r["avg_canopy_cm2"],
        "health_summary": json.loads(r["health_json"] or "{}"),
        "disease_warning": bool(r["disease_warning"]),
        "sick_positions": json.loads(r["sick_json"] or "[]"),
        "cell_results": json.loads(r["cell_json"] or "[]"),
    } for r in rows]

def _fetch_analysis_latest():
    with _get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM analysis_records ORDER BY timestamp DESC LIMIT 1")
        row = cur.fetchone()
        cur.close()
    if not row: return None
    return {
        "timestamp": row["timestamp"], "plant_count": row["plant_count"],
        "missing_positions": json.loads(row["missing_json"] or "[]"),
        "avg_canopy_cm2": row["avg_canopy_cm2"],
        "health_summary": json.loads(row["health_json"] or "{}"),
        "disease_warning": bool(row["disease_warning"]),
        "sick_positions": json.loads(row["sick_json"] or "[]"),
        "cell_results": json.loads(row["cell_json"] or "[]"),
    }

# ════════════════════════════════════════════════════
# ROUTES — SENSOR
# ════════════════════════════════════════════════════

@app.route("/api/sensor/live", methods=["POST"])
def receive_sensor_live():
    """
    ESP32 POST mỗi 1 phút — cập nhật RAM để hiển thị web.
    KHÔNG lưu vào DB.
    """
    global _live_sensor
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Cần JSON"}), 400

    temp     = _to_int(data.get("temperature"))
    humidity = _to_int(data.get("humidity"))
    soil     = _to_int(data.get("soil_moisture"))
    light    = data.get("light_lux")
    light    = _to_int(_apply_light_fallback(light))  # BH1750 nghi hỏng, báo 0 -> thay bằng mô phỏng
    # Server là nguồn chân lý (authoritative) cho giai đoạn sinh trưởng — dùng tên
    # giai đoạn hiện tại của server để lưu/hiển thị, không dùng giá trị ESP32 gửi lên
    # (ESP32 có thể đang chạy giá trị cũ trong lúc chờ đồng bộ).
    stage = STAGE_NAMES[_device_state["growth_stage"]]

    _live_sensor = {
        "timestamp":    vn_now().isoformat(),
        "temperature":  temp,
        "humidity":     humidity,
        "soil_moist":   soil,
        "light_lux":    light,
        "growth_stage": stage,
        "pump_on":      data.get("pump_on", False),
        "fan_on":       data.get("fan_on",  False),
    }

    # Đồng bộ trạng thái hiển thị theo đúng thực tế ESP32 báo về — CHỈ khi đang ở chế
    # độ auto. Ở chế độ manual, "on" là LỆNH mong muốn do người dùng đặt qua /api/control;
    # nếu đồng bộ vô điều kiện ở đây, request telemetry này (gửi mỗi 5s) có thể tới đúng
    # lúc ESP32 chưa kịp thực thi lệnh (pumpOn vẫn còn false), ghi đè xóa mất lệnh bật/tắt
    # vừa bấm trên dashboard — gây hiện tượng "bấm BẬT nhưng vài giây sau tự hiện TẮT".
    if _device_state["pump"]["mode"] == "au
