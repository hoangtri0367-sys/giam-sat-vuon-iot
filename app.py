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

    temp     = data.get("temperature")
    humidity = data.get("humidity")
    soil     = data.get("soil_moisture")
    light    = data.get("light_lux")
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

    # Đồng bộ trạng thái hiển thị theo đúng thực tế ESP32 báo về (ESP32 là nguồn chân lý
    # cho on/off vật lý — tránh dashboard hiển thị "BẬT/TẮT" ảo lệch với bơm/quạt thật).
    _device_state["pump"]["on"] = bool(_live_sensor["pump_on"])
    _device_state["fan"]["on"]  = bool(_live_sensor["fan_on"])

    _auto_control(temp, soil)
    _check_alerts(temp, soil, light)

    return jsonify({
        "status":       "ok",
        "pump_on":      _device_state["pump"]["on"],
        "pump_mode":    _device_state["pump"]["mode"],
        "fan_on":       _device_state["fan"]["on"],
        "fan_mode":     _device_state["fan"]["mode"],
        "growth_stage": _device_state["growth_stage"],   # int 0-3, ESP32 dùng để tự đồng bộ
    }), 200


@app.route("/api/sensor", methods=["POST"])
def receive_sensor_db():
    """
    ESP32 POST mỗi 30 phút — lưu vào DB để vẽ biểu đồ + xuất Excel.
    """
    global _live_sensor
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Cần JSON"}), 400

    temp     = data.get("temperature")
    humidity = data.get("humidity")
    soil     = data.get("soil_moisture")
    light    = data.get("light_lux")
    pump_on  = bool(data.get("pump_on", False))
    fan_on   = bool(data.get("fan_on",  False))
    # Server là nguồn chân lý cho giai đoạn sinh trưởng (xem giải thích ở /api/sensor/live)
    stage = STAGE_NAMES[_device_state["growth_stage"]]
    ts    = vn_now().isoformat()

    # Đồng bộ trạng thái hiển thị theo đúng thực tế ESP32 báo về
    _device_state["pump"]["on"] = pump_on
    _device_state["fan"]["on"]  = fan_on

    # Cập nhật live luôn
    _live_sensor = {
        "timestamp": ts, "temperature": temp, "humidity": humidity,
        "soil_moist": soil, "light_lux": light,
        "growth_stage": stage,
        "pump_on": pump_on,
        "fan_on":  fan_on,
    }

    # Lưu DB
    with _get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sensor_data(timestamp,temperature,humidity,soil_moist,light_lux,growth_stage,pump_on,fan_on) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (ts, temp, humidity, soil, light, stage,
             int(pump_on),
             int(fan_on)))
        cur.close()

    _auto_control(temp, soil)
    _check_alerts(temp, soil, light)

    logger.info(f"[DB] T={temp}°C | Am KK={humidity}% | Đất={soil}% | Sáng={light}lux")
    return jsonify({
        "status":       "ok",
        "pump_on":      _device_state["pump"]["on"],
        "pump_mode":    _device_state["pump"]["mode"],
        "fan_on":       _device_state["fan"]["on"],
        "fan_mode":     _device_state["fan"]["mode"],
        "growth_stage": _device_state["growth_stage"],
    }), 200


@app.route("/api/sensor/latest", methods=["GET"])
def sensor_latest():
    """Dashboard poll mỗi 10 giây — trả live data từ RAM."""
    if _live_sensor:
        data = dict(_live_sensor)
        data["devices"] = _device_state
        return jsonify(data)
    # Fallback về DB nếu chưa có live
    db = _fetch_sensor_latest_db()
    if db:
        db["devices"] = _device_state
        return jsonify(db)
    return jsonify({"status": "no_data"})


@app.route("/api/sensor/history", methods=["GET"])
def sensor_history():
    hours = int(request.args.get("hours", 168))
    return jsonify(_fetch_sensor_history(hours))


# ════════════════════════════════════════════════════
# ROUTES — RELAY CONTROL
# ════════════════════════════════════════════════════

@app.route("/api/control", methods=["POST"])
def control_relay():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Cần JSON"}), 400

    device = data.get("device")
    action = data.get("action")
    if device not in ("pump", "fan"):
        return jsonify({"error": "device: pump hoặc fan"}), 400

    if action in ("on", "off"):
        _device_state[device]["mode"] = "manual"
        _set_relay(device, action == "on", "manual")
        _save_device_state()
    elif action in ("auto", "manual"):
        _device_state[device]["mode"] = action
        _save_device_state()
    else:
        return jsonify({"error": "action không hợp lệ"}), 400

    return jsonify({
        "status": "ok", "device": device,
        "on":   _device_state[device]["on"],
        "mode": _device_state[device]["mode"],
    })


@app.route("/api/device-state", methods=["GET"])
def device_state():
    return jsonify({
        "pump_on":          _device_state["pump"]["on"],
        "pump_mode":        _device_state["pump"]["mode"],
        "fan_on":           _device_state["fan"]["on"],
        "fan_mode":         _device_state["fan"]["mode"],
        "growth_stage":     _device_state["growth_stage"],
        "growth_stage_name": STAGE_NAMES[_device_state["growth_stage"]],
        "timestamp": vn_now().isoformat(),
    })


@app.route("/api/growth-stage", methods=["GET"])
def get_growth_stage():
    """Dashboard đọc giai đoạn hiện tại (để tô sáng lựa chọn đang chọn)."""
    stage = _device_state["growth_stage"]
    return jsonify({
        "growth_stage": stage,
        "growth_stage_name": STAGE_NAMES[stage],
        "thresholds": THRESHOLDS[stage],
        "options": [{"value": i, "name": n} for i, n in enumerate(STAGE_NAMES)],
    })


@app.route("/api/growth-stage", methods=["POST"])
def set_growth_stage():
    """Dashboard chọn giai đoạn sinh trưởng mới (0-3). ESP32 sẽ tự đồng bộ trong
    tối đa 30 giây qua pollDeviceState(), hoặc tối đa 1 phút qua postSensor()."""
    data = request.get_json(silent=True)
    if not data or "growth_stage" not in data:
        return jsonify({"error": "Cần { \"growth_stage\": 0-3 }"}), 400
    try:
        stage = int(data["growth_stage"])
    except (TypeError, ValueError):
        return jsonify({"error": "growth_stage phải là số nguyên 0-3"}), 400
    if stage < 0 or stage >= len(STAGE_NAMES):
        return jsonify({"error": f"growth_stage phải trong khoảng 0-{len(STAGE_NAMES)-1}"}), 400

    _device_state["growth_stage"] = stage
    _save_device_state()
    logger.info(f"[Stage] Đổi giai đoạn -> {STAGE_NAMES[stage]}")
    return jsonify({
        "status": "ok",
        "growth_stage": stage,
        "growth_stage_name": STAGE_NAMES[stage],
        "thresholds": THRESHOLDS[stage],
    })


# ════════════════════════════════════════════════════
# ROUTES — ẢNH
# ════════════════════════════════════════════════════

# ════════════════════════════════════════════════════
# ROUTES — CHỤP ẢNH THỦ CÔNG (dashboard bấm nút -> ESP32-CAM poll thấy -> chụp)
# ════════════════════════════════════════════════════

@app.route("/api/manual-capture", methods=["POST"])
def request_manual_capture():
    """Dashboard gọi khi bấm nút 'Chụp ảnh ngay' -> đặt cờ để ESP32-CAM (poll định kỳ,
    tuỳ chế độ có thể mất tới vài phút mới thấy) phát hiện và chụp ở lần poll gần nhất."""
    global _manual_capture_requested, _manual_capture_result
    _manual_capture_requested = True
    _manual_capture_result = None  # Reset kết quả cũ, chờ ESP32-CAM báo lại kết quả mới
    logger.info("[Manual] Dashboard yêu cầu chụp ảnh thủ công")
    return jsonify({"status": "ok", "manual_capture": True})

@app.route("/api/manual-capture", methods=["GET"])
def check_manual_capture():
    """ESP32-CAM gọi định kỳ để kiểm tra có yêu cầu chụp thủ công không.
    Dashboard cũng gọi route này để poll kết quả (manual_capture=false + result=true/false/null)."""
    return jsonify({"manual_capture": _manual_capture_requested, "result": _manual_capture_result})

@app.route("/api/manual-capture/clear", methods=["POST"])
def clear_manual_capture():
    """ESP32-CAM gọi sau khi đã XỬ LÝ XONG (dù thành công hay thất bại) yêu cầu chụp thủ
    công, để xóa cờ tránh chụp lặp lại vô hạn. Body JSON {"success": true/false} — bắt
    buộc phải gửi true/false thực tế, không phải "gọi được route là coi như thành công"."""
    global _manual_capture_requested, _manual_capture_result
    _manual_capture_requested = False
    try:
        data = request.get_json(force=True, silent=True) or {}
        _manual_capture_result = bool(data.get("success", False))
    except Exception:
        _manual_capture_result = False
    logger.info(f"[Manual] Kết quả chụp thủ công: {'thành công' if _manual_capture_result else 'THẤT BẠI'}")
    return jsonify({"status": "ok"})


@app.route("/api/upload-image", methods=["POST"])
def upload_image():
    global _latest_annotated_bytes
    ct = request.content_type or ""
    if not ("jpeg" in ct or "octet-stream" in ct):
        return jsonify({"error": "Chỉ nhận image/jpeg"}), 415
    image_bytes = request.get_data()
    if len(image_bytes) < 1000:
        return jsonify({"error": "Ảnh quá nhỏ"}), 400

    # STAGE_NAMES[idx].lower() -> "nay_mam"/"cay_con"/"sinh_truong"/"thu_hoach",
    # đúng key mà CELL_PLANT_THRESHOLD_BY_STAGE trong image_analysis.py dùng.
    # Thiếu bước này thì mọi ảnh đều bị áp ngưỡng mặc định (4%), khiến cây con
    # mới nảy mầm (tán lá còn nhỏ) bị báo nhầm "Missing".
    current_stage_key = STAGE_NAMES[_device_state["growth_stage"]].lower()
    result = analyze_image(image_bytes, growth_stage=current_stage_key)
    if "error" in result:
        return jsonify(result), 500

    # Ghi đè lại timestamp bằng giờ Việt Nam đúng — analyze_image() (image_analysis.py)
    # có thể đang dùng datetime.now() trần (giờ UTC của server Render), không phải vn_now().
    result["timestamp"] = vn_now().isoformat()

    _latest_annotated_bytes = result.get("debug_image_bytes")
    _insert_analysis(result)

    # Gửi ảnh (đã đánh dấu sức khỏe từng ô) qua Telegram mỗi lần chụp, kèm ngày giờ + tóm tắt
    if _latest_annotated_bytes:
        ts_str = vn_now().strftime("%H:%M %d/%m/%Y")
        hs = result["health_summary"].get("health_score")
        caption = (f"📸 <b>Ảnh chụp lúc {ts_str}</b>\n"
                   f"🌱 Số cây: {result['plant_count']}/16\n"
                   f"🍃 Tán lá TB: {result['avg_canopy_cm2']} cm²\n"
                   f"💯 Điểm sức khỏe: {hs}%")
        _send_telegram_photo(_latest_annotated_bytes, caption)

    if result.get("disease_warning"):
        sick = ", ".join(result.get("sick_positions", []))
        _send_telegram(f"🍂 <b>Phát hiện lá bệnh/vàng!</b>\nVị trí: <b>{sick}</b>",
                       alert_key="disease", cooldown_min=60)
    missing = result.get("missing_positions", [])
    if len(missing) >= 3:
        _send_telegram(f"🌱 <b>Thiếu {len(missing)} cây!</b>\n{', '.join(missing)}",
                       alert_key="missing", cooldown_min=120)

    return jsonify({
        "status": "ok",
        "plant_count":  result["plant_count"],
        "missing":      result["missing_positions"],
        "health_score": result["health_summary"].get("health_score"),
        "disease_warn": result["disease_warning"],
        "avg_canopy":   result["avg_canopy_cm2"],
        "timestamp":    result["timestamp"],
    }), 200


@app.route("/api/latest-image", methods=["GET"])
def latest_image():
    if _latest_annotated_bytes is None:
        return jsonify({"error": "Chưa có ảnh"}), 404
    return send_file(io.BytesIO(_latest_annotated_bytes),
                     mimetype="image/jpeg", download_name="latest.jpg")

@app.route("/api/plant-status", methods=["GET"])
def plant_status():
    return jsonify(_fetch_analysis_latest() or {"status": "no_data"})

@app.route("/api/image-history", methods=["GET"])
def image_history():
    days  = int(request.args.get("days", 7))
    end   = vn_now()
    start = end - timedelta(days=days)
    return jsonify([{
        "timestamp": r["timestamp"], "plant_count": r["plant_count"],
        "avg_canopy": r["avg_canopy_cm2"],
        "health_score": r["health_summary"].get("health_score", 0),
    } for r in _fetch_analysis_records(start, end)])


# ════════════════════════════════════════════════════
# ROUTES — DASHBOARD & EXCEL
# ════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def dashboard():
    return render_template("index.html")

@app.route("/api/dashboard-summary", methods=["GET"])
def dashboard_summary():
    # Ưu tiên live data, fallback DB
    sensor = _live_sensor if _live_sensor else _fetch_sensor_latest_db()
    plant  = _fetch_analysis_latest()
    since  = (vn_now() - timedelta(days=7)).isoformat()
    with _get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM alerts WHERE timestamp >= %s ORDER BY timestamp DESC LIMIT 20",
            (since,))
        alerts = cur.fetchall()
        cur.close()
    return jsonify({
        "sensor":  sensor,
        "plant":   plant,
        "alerts":  [dict(a) for a in alerts],
        "devices": _device_state,
    })

@app.route("/api/export-weekly", methods=["GET"])
def export_weekly():
    days      = int(request.args.get("days", 7))
    send_mail = request.args.get("send", "0") == "1"
    week_end  = vn_now()
    week_start = week_end - timedelta(days=days)
    records = _fetch_analysis_records(week_start, week_end)
    if not records:
        return jsonify({"error": f"Không có dữ liệu {days} ngày"}), 404
    sensor_records = _fetch_sensor_history(hours=days * 24)
    xlsx = generate_weekly_excel(records, week_start, sensor_records)
    fname = f"BaoCao_CaiNgot_{week_start.strftime('%d%m%Y')}-{week_end.strftime('%d%m%Y')}.xlsx"
    if send_mail:
        try: _send_email(xlsx, fname, week_start, week_end, len(records))
        except Exception as e: logger.error(f"Email lỗi: {e}")
    return send_file(io.BytesIO(xlsx),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=fname)


# ════════════════════════════════════════════════════
# SCHEDULER & MAIN
# ════════════════════════════════════════════════════

def _scheduled_report():
    week_end   = vn_now()
    week_start = week_end - timedelta(days=7)
    records    = _fetch_analysis_records(week_start, week_end)
    if not records: return
    sensor_records = _fetch_sensor_history(hours=7 * 24)
    xlsx  = generate_weekly_excel(records, week_start, sensor_records)
    fname = f"BaoCao_CaiNgot_{week_start.strftime('%d%m%Y')}-{week_end.strftime('%d%m%Y')}.xlsx"
    try:
        _send_email(xlsx, fname, week_start, week_end, len(records))
        logger.info(f"Đã gửi báo cáo: {fname}")
    except Exception as e:
        logger.error(f"Scheduler email lỗi: {e}")

def _scheduled_backup():
    try:
        run_backup()
    except Exception as e:
        logger.error(f"Backup database lỗi: {e}")

def init_scheduler():
    scheduler = BackgroundScheduler(timezone="Asia/Ho_Chi_Minh")
    scheduler.add_job(_scheduled_report, "cron",
                      day_of_week="sun", hour=20, minute=0,
                      id="weekly_report", replace_existing=True)
    scheduler.add_job(_scheduled_backup, "cron",
                      day_of_week="mon,thu", hour=6, minute=0,
                      id="db_backup", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler: Báo cáo CN 20:00 | Backup DB Thứ 2 & Thứ 5 06:00 ICT")
    return scheduler

init_db()
_load_device_state()
scheduler = init_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
