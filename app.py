"""
app.py — Server chính hệ thống IoT Giám sát Cải Ngọt
"""

import io, json, logging, os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, request, send_file

from image_analysis import analyze_image
from weekly_report import generate_weekly_excel, _send_email
from backup_db import run_backup

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

# ── GIAI ĐOẠN SINH TRƯỞNG ────────────────────────────
# Khớp với mảng THRESHOLDS[] và STAGE_NAMES[] trong firmware ESP32_Main.ino
STAGE_NAMES = ["Nay_Mam", "Cay_Con", "Sinh_Truong", "Thu_Hoach"]
THRESHOLDS = [
    {"soil_min": 60, "soil_max": 80, "temp_max": 30},  # 0 Nảy mầm
    {"soil_min": 55, "soil_max": 75, "temp_max": 32},  # 1 Cây con
    {"soil_min": 45, "soil_max": 70, "temp_max": 35},  # 2 Sinh trưởng
    {"soil_min": 40, "soil_max": 65, "temp_max": 35},  # 3 Thu hoạch
]

# ── STATE ────────────────────────────────────────────
_device_state = {
    "pump": {"on": False, "mode": "auto"},
    "fan":  {"on": False, "mode": "auto"},
    "growth_stage": 2,   # mặc định Sinh trưởng — dashboard có thể đổi qua /api/growth-stage
}
_live_sensor = {}          # Dữ liệu live (RAM, cập nhật mỗi 1 phút)
_latest_annotated_bytes: bytes = None

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
            timestamp TEXT NOT NULL, temperature REAL,
            soil_moist REAL, light_lux REAL, growth_stage TEXT,
            pump_on INTEGER DEFAULT 0, fan_on INTEGER DEFAULT 0)""")
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
        cur.close()
    logger.info("DB PostgreSQL sẵn sàng")

# ── TELEGRAM ─────────────────────────────────────────
def _send_telegram(message: str, alert_key: str = None, cooldown_min: int = 30):
    """Gửi Telegram. cooldown_min: không gửi lại cùng loại cảnh báo trong N phút."""
    if alert_key:
        now = datetime.now()
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
                             (datetime.now().isoformat(), "telegram", message))
                cur.close()
            return True
    except Exception as e:
        logger.error(f"Telegram lỗi: {e}")
    return False

# ── RELAY ────────────────────────────────────────────
def _set_relay(device: str, on: bool, source: str = "auto"):
    _device_state[device]["on"] = on
    with _get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO relay_log(timestamp,device,action,source) VALUES(%s,%s,%s,%s)",
                     (datetime.now().isoformat(), device, "ON" if on else "OFF", source))
        cur.close()
    logger.info(f"Relay [{device}] → {'ON' if on else 'OFF'} ({source})")

def _current_thresholds():
    """Ngưỡng soil_min/soil_max/temp_max của giai đoạn sinh trưởng đang chọn."""
    return THRESHOLDS[_device_state["growth_stage"]]

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
    if light is not None and light < LIGHT_MIN:
        _send_telegram(f"☀️ <b>Ánh sáng yếu!</b> <b>{light} lux</b> &lt; {LIGHT_MIN} lux",
                       alert_key="light_low", cooldown_min=120)

# ── SENSOR HELPERS ───────────────────────────────────
def _fetch_sensor_history(hours=168):
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
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

    temp  = data.get("temperature")
    soil  = data.get("soil_moisture")
    light = data.get("light_lux")
    # Server là nguồn chân lý (authoritative) cho giai đoạn sinh trưởng — dùng tên
    # giai đoạn hiện tại của server để lưu/hiển thị, không dùng giá trị ESP32 gửi lên
    # (ESP32 có thể đang chạy giá trị cũ trong lúc chờ đồng bộ).
    stage = STAGE_NAMES[_device_state["growth_stage"]]

    _live_sensor = {
        "timestamp":    datetime.now().isoformat(),
        "temperature":  temp,
        "soil_moist":   soil,
        "light_lux":    light,
        "growth_stage": stage,
        "pump_on":      data.get("pump_on", False),
        "fan_on":       data.get("fan_on",  False),
    }

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

    temp  = data.get("temperature")
    soil  = data.get("soil_moisture")
    light = data.get("light_lux")
    # Server là nguồn chân lý cho giai đoạn sinh trưởng (xem giải thích ở /api/sensor/live)
    stage = STAGE_NAMES[_device_state["growth_stage"]]
    ts    = datetime.now().isoformat()

    # Cập nhật live luôn
    _live_sensor = {
        "timestamp": ts, "temperature": temp,
        "soil_moist": soil, "light_lux": light,
        "growth_stage": stage,
        "pump_on": _device_state["pump"]["on"],
        "fan_on":  _device_state["fan"]["on"],
    }

    # Lưu DB
    with _get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sensor_data(timestamp,temperature,soil_moist,light_lux,growth_stage,pump_on,fan_on) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (ts, temp, soil, light, stage,
             int(_device_state["pump"]["on"]),
             int(_device_state["fan"]["on"])))
        cur.close()

    _auto_control(temp, soil)
    _check_alerts(temp, soil, light)

    logger.info(f"[DB] T={temp}°C | Đất={soil}% | Sáng={light}lux")
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
    elif action in ("auto", "manual"):
        _device_state[device]["mode"] = action
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
        "timestamp": datetime.now().isoformat(),
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

@app.route("/api/upload-image", methods=["POST"])
def upload_image():
    global _latest_annotated_bytes
    ct = request.content_type or ""
    if not ("jpeg" in ct or "octet-stream" in ct):
        return jsonify({"error": "Chỉ nhận image/jpeg"}), 415
    image_bytes = request.get_data()
    if len(image_bytes) < 1000:
        return jsonify({"error": "Ảnh quá nhỏ"}), 400

    result = analyze_image(image_bytes)
    if "error" in result:
        return jsonify(result), 500

    _latest_annotated_bytes = result.get("debug_image_bytes")
    _insert_analysis(result)

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
    end   = datetime.now()
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
    since  = (datetime.now() - timedelta(days=7)).isoformat()
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
    week_end  = datetime.now()
    week_start = week_end - timedelta(days=days)
    records = _fetch_analysis_records(week_start, week_end)
    if not records:
        return jsonify({"error": f"Không có dữ liệu {days} ngày"}), 404
    xlsx = generate_weekly_excel(records, week_start)
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
    week_end   = datetime.now()
    week_start = week_end - timedelta(days=7)
    records    = _fetch_analysis_records(week_start, week_end)
    if not records: return
    xlsx  = generate_weekly_excel(records, week_start)
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
scheduler = init_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
