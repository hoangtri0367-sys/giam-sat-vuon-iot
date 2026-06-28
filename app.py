"""
app.py — Server chính hệ thống IoT Giám sát Cải Ngọt
======================================================
Chức năng:
  - Nhận sensor data (nhiệt độ, độ ẩm đất, ánh sáng) từ ESP32
  - Nhận ảnh từ ESP32-CAM và phân tích bằng OpenCV
  - Web dashboard với biểu đồ Chart.js
  - Cảnh báo qua Telegram Bot (miễn phí)
  - Xuất báo cáo Excel tuần + gửi email tự động

Deploy: Railway.app
"""

import io
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, request, send_file

from image_analysis import analyze_image
from weekly_report import generate_weekly_excel, _send_email

# ────────────────────────────────────────────────
# KHỞI TẠO APP
# ────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ────────────────────────────────────────────────
# CẤU HÌNH — đọc từ biến môi trường Railway
# ────────────────────────────────────────────────

DB_PATH         = os.environ.get("DB_PATH", "cai_ngot.db")

# Telegram Bot
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Email báo cáo tuần
EMAIL_SENDER    = os.environ.get("EMAIL_SENDER",    "")
EMAIL_PASSWORD  = os.environ.get("EMAIL_PASSWORD",  "")
EMAIL_RECIPIENT = os.environ.get("EMAIL_RECIPIENT", "")
SMTP_HOST       = os.environ.get("SMTP_HOST",       "smtp.gmail.com")
SMTP_PORT       = int(os.environ.get("SMTP_PORT",   "587"))

# Ngưỡng cảnh báo sensor
TEMP_MIN    = float(os.environ.get("TEMP_MIN",    "15"))
TEMP_MAX    = float(os.environ.get("TEMP_MAX",    "35"))
SOIL_MIN    = float(os.environ.get("SOIL_MIN",    "40"))   # % độ ẩm đất
LIGHT_MIN   = float(os.environ.get("LIGHT_MIN",   "500"))  # lux

# ── Ảnh annotated mới nhất (RAM — không cần bền vững) ──
_latest_annotated_bytes: bytes = None

# ────────────────────────────────────────────────
# DATABASE SQLite
# ────────────────────────────────────────────────

@contextmanager
def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _get_db() as conn:
        # Bảng sensor data
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sensor_data (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                temperature REAL,
                soil_moist  REAL,
                light_lux   REAL,
                growth_stage TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sensor_ts
            ON sensor_data (timestamp)
        """)

        # Bảng phân tích ảnh
        conn.execute("""
            CREATE TABLE IF NOT EXISTS analysis_records (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT    NOT NULL,
                plant_count     INTEGER,
                missing_json    TEXT,
                avg_canopy_cm2  REAL,
                health_json     TEXT,
                disease_warning INTEGER,
                sick_json       TEXT,
                cell_json       TEXT,
                px_per_cm       REAL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_analysis_ts
            ON analysis_records (timestamp)
        """)

        # Bảng log cảnh báo
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                type      TEXT,
                message   TEXT,
                sent_sms  INTEGER DEFAULT 0
            )
        """)
    logger.info(f"SQLite DB sẵn sàng: {DB_PATH}")


# ────────────────────────────────────────────────
# HELPERS — Sensor
# ────────────────────────────────────────────────

def _insert_sensor(ts, temp, soil, light, stage):
    with _get_db() as conn:
        conn.execute(
            "INSERT INTO sensor_data (timestamp, temperature, soil_moist, light_lux, growth_stage) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, temp, soil, light, stage)
        )


def _fetch_sensor_history(hours=168):
    """Lấy dữ liệu sensor N giờ gần nhất (mặc định 7 ngày = 168h)."""
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sensor_data WHERE timestamp >= ? ORDER BY timestamp ASC",
            (since,)
        ).fetchall()
    return [dict(r) for r in rows]


def _fetch_sensor_latest():
    with _get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sensor_data ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


# ────────────────────────────────────────────────
# HELPERS — Phân tích ảnh
# ────────────────────────────────────────────────

def _insert_analysis(result: dict):
    with _get_db() as conn:
        conn.execute("""
            INSERT INTO analysis_records
                (timestamp, plant_count, missing_json, avg_canopy_cm2,
                 health_json, disease_warning, sick_json, cell_json, px_per_cm)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            result["timestamp"],
            result["plant_count"],
            json.dumps(result.get("missing_positions", []), ensure_ascii=False),
            result.get("avg_canopy_cm2", 0),
            json.dumps(result.get("health_summary", {})),
            int(result.get("disease_warning", False)),
            json.dumps(result.get("sick_positions", []), ensure_ascii=False),
            json.dumps(result.get("cell_results", []), ensure_ascii=False),
            result.get("px_per_cm", 0),
        ))


def _fetch_analysis_records(start: datetime, end: datetime) -> list:
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM analysis_records WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp ASC",
            (start.isoformat(), end.isoformat())
        ).fetchall()
    records = []
    for row in rows:
        records.append({
            "timestamp":         row["timestamp"],
            "plant_count":       row["plant_count"],
            "missing_positions": json.loads(row["missing_json"] or "[]"),
            "avg_canopy_cm2":    row["avg_canopy_cm2"],
            "health_summary":    json.loads(row["health_json"] or "{}"),
            "disease_warning":   bool(row["disease_warning"]),
            "sick_positions":    json.loads(row["sick_json"] or "[]"),
            "cell_results":      json.loads(row["cell_json"] or "[]"),
            "px_per_cm":         row["px_per_cm"],
        })
    return records


def _fetch_analysis_latest() -> dict | None:
    with _get_db() as conn:
        row = conn.execute(
            "SELECT * FROM analysis_records ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return {
        "timestamp":         row["timestamp"],
        "plant_count":       row["plant_count"],
        "missing_positions": json.loads(row["missing_json"] or "[]"),
        "avg_canopy_cm2":    row["avg_canopy_cm2"],
        "health_summary":    json.loads(row["health_json"] or "{}"),
        "disease_warning":   bool(row["disease_warning"]),
        "sick_positions":    json.loads(row["sick_json"] or "[]"),
    }


# ────────────────────────────────────────────────
# HELPERS — Telegram Bot
# ────────────────────────────────────────────────

def _send_telegram(message: str):
    """Gửi tin nhắn qua Telegram Bot. Bỏ qua nếu chưa cấu hình."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram chưa cấu hình — bỏ qua.")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text":    message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        result = resp.json()
        if result.get("ok"):
            logger.info(f"Telegram đã gửi: {message[:60]}...")
            with _get_db() as conn:
                conn.execute(
                    "INSERT INTO alerts (timestamp, type, message, sent_sms) VALUES (?, ?, ?, 1)",
                    (datetime.now().isoformat(), "telegram", message)
                )
            return True
        else:
            logger.error(f"Telegram lỗi: {result}")
            return False
    except Exception as e:
        logger.error(f"Lỗi gửi Telegram: {e}")
        return False


def _check_and_alert_sensor(temp, soil, light):
    """Kiểm tra ngưỡng và gửi Telegram nếu vượt."""
    alerts = []
    if temp is not None and temp > TEMP_MAX:
        alerts.append(f"🌡️ Nhiệt độ cao <b>{temp}°C</b> (ngưỡng {TEMP_MAX}°C)")
    if temp is not None and temp < TEMP_MIN:
        alerts.append(f"🌡️ Nhiệt độ thấp <b>{temp}°C</b> (ngưỡng {TEMP_MIN}°C)")
    if soil is not None and soil < SOIL_MIN:
        alerts.append(f"💧 Độ ẩm đất thấp <b>{soil}%</b> (ngưỡng {SOIL_MIN}%)")
    if light is not None and light < LIGHT_MIN:
        alerts.append(f"☀️ Ánh sáng yếu <b>{light} lux</b> (ngưỡng {LIGHT_MIN} lux)")

    if alerts:
        msg = "⚠️ <b>[CẢI NGỌT IoT] CẢNH BÁO</b>\n\n" + "\n".join(alerts)
        _send_telegram(msg)


# ────────────────────────────────────────────────
# ROUTES — Sensor Data
# ────────────────────────────────────────────────

@app.route("/api/sensor", methods=["POST"])
def receive_sensor():
    """
    ESP32 POST JSON sensor data lên đây mỗi 20 phút.

    Body JSON:
    {
        "temperature": 28.5,
        "soil_moisture": 65.2,
        "light_lux": 1200,
        "growth_stage": "Vegetative"
    }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Cần JSON body"}), 400

    temp  = data.get("temperature")
    soil  = data.get("soil_moisture")
    light = data.get("light_lux")
    stage = data.get("growth_stage", "Unknown")
    ts    = datetime.now().isoformat()

    _insert_sensor(ts, temp, soil, light, stage)
    _check_and_alert_sensor(temp, soil, light)

    logger.info(f"Sensor: T={temp}°C | Soil={soil}% | Light={light}lux | Stage={stage}")
    return jsonify({"status": "ok", "timestamp": ts}), 200


@app.route("/api/sensor/latest", methods=["GET"])
def sensor_latest():
    """Trả JSON sensor mới nhất (cho Blynk / dashboard)."""
    data = _fetch_sensor_latest()
    if not data:
        return jsonify({"status": "no_data"}), 200
    return jsonify(data)


@app.route("/api/sensor/history", methods=["GET"])
def sensor_history():
    """
    Lịch sử sensor. Query param: hours=168 (mặc định 7 ngày)
    Dùng cho Chart.js trên dashboard.
    """
    hours = int(request.args.get("hours", 168))
    rows  = _fetch_sensor_history(hours)
    return jsonify(rows)


# ────────────────────────────────────────────────
# ROUTES — Ảnh ESP32-CAM
# ────────────────────────────────────────────────

@app.route("/api/upload-image", methods=["POST"])
def upload_image():
    """
    ESP32-CAM POST ảnh JPEG raw.

    Arduino code mẫu:
        HTTPClient http;
        http.begin("https://YOUR_APP.railway.app/api/upload-image");
        http.addHeader("Content-Type", "image/jpeg");
        int code = http.POST(fb->buf, fb->len);
    """
    global _latest_annotated_bytes

    ct = request.content_type or ""
    if not ("jpeg" in ct or "octet-stream" in ct):
        return jsonify({"error": "Chỉ nhận Content-Type: image/jpeg"}), 415

    image_bytes = request.get_data()
    if len(image_bytes) < 1000:
        return jsonify({"error": "Ảnh quá nhỏ hoặc rỗng"}), 400

    result = analyze_image(image_bytes)
    if "error" in result:
        logger.error(f"Lỗi phân tích ảnh: {result['error']}")
        return jsonify(result), 500

    # Lưu ảnh annotated vào RAM để hiển thị
    _latest_annotated_bytes = result.get("debug_image_bytes")

    # Lưu kết quả vào SQLite
    _insert_analysis(result)

    # Gửi Telegram nếu phát hiện bệnh
    if result.get("disease_warning"):
        sick = ", ".join(result.get("sick_positions", []))
        _send_telegram(f"🍂 <b>[CẢI NGỌT IoT]</b> Phát hiện lá bệnh/vàng tại: <b>{sick}</b>\nKiểm tra ngay!")

    # Gửi Telegram nếu thiếu cây
    missing = result.get("missing_positions", [])
    if len(missing) >= 3:
        _send_telegram(f"🌱 <b>[CẢI NGỌT IoT]</b> Thiếu <b>{len(missing)} cây</b> tại: {', '.join(missing)}")

    logger.info(
        f"Ảnh OK: {result['plant_count']}/18 cây | "
        f"Tán lá={result['avg_canopy_cm2']}cm² | "
        f"SK={result['health_summary'].get('health_score')}% | "
        f"Bệnh={result['disease_warning']}"
    )

    return jsonify({
        "status":       "ok",
        "plant_count":  result["plant_count"],
        "missing":      result["missing_positions"],
        "health_score": result["health_summary"].get("health_score"),
        "disease_warn": result["disease_warning"],
        "avg_canopy":   result["avg_canopy_cm2"],
        "timestamp":    result["timestamp"],
    }), 200


@app.route("/api/latest-image", methods=["GET"])
def latest_image():
    """Ảnh JPEG đã annotate (vẽ lưới + nhãn) lần chụp gần nhất."""
    if _latest_annotated_bytes is None:
        return jsonify({"error": "Chưa nhận được ảnh nào từ lần khởi động gần nhất"}), 404
    return send_file(
        io.BytesIO(_latest_annotated_bytes),
        mimetype="image/jpeg",
        as_attachment=False,
        download_name="latest_annotated.jpg",
    )


@app.route("/api/plant-status", methods=["GET"])
def plant_status():
    """Tổng quan cây trồng mới nhất (cho Blynk / dashboard)."""
    data = _fetch_analysis_latest()
    if not data:
        return jsonify({"status": "no_data"}), 200
    return jsonify(data)


@app.route("/api/image-history", methods=["GET"])
def image_history():
    """
    Lịch sử phân tích ảnh. Query param: days=7
    Trả số cây và tán lá theo thời gian cho Chart.js.
    """
    days  = int(request.args.get("days", 7))
    end   = datetime.now()
    start = end - timedelta(days=days)
    rows  = _fetch_analysis_records(start, end)
    # Chỉ trả các trường cần cho biểu đồ
    slim = [
        {
            "timestamp":    r["timestamp"],
            "plant_count":  r["plant_count"],
            "avg_canopy":   r["avg_canopy_cm2"],
            "health_score": r["health_summary"].get("health_score", 0),
        }
        for r in rows
    ]
    return jsonify(slim)


# ────────────────────────────────────────────────
# ROUTES — Xuất Excel
# ────────────────────────────────────────────────

@app.route("/api/export-weekly", methods=["GET"])
def export_weekly():
    """
    Xuất báo cáo Excel.
    Query params:
        days=7   (mặc định 7 ngày)
        send=1   (đồng thời gửi email)

    Ví dụ:
        GET /api/export-weekly?days=7&send=1
    """
    days      = int(request.args.get("days", 7))
    send_mail = request.args.get("send", "0") == "1"

    week_end   = datetime.now()
    week_start = week_end - timedelta(days=days)

    records = _fetch_analysis_records(week_start, week_end)
    if not records:
        return jsonify({"error": f"Không có dữ liệu trong {days} ngày gần nhất"}), 404

    xlsx_bytes = generate_weekly_excel(records, week_start)
    filename   = (
        f"BaoCao_CaiNgot_"
        f"{week_start.strftime('%d%m%Y')}-{week_end.strftime('%d%m%Y')}.xlsx"
    )

    if send_mail:
        try:
            _send_email(xlsx_bytes, filename, week_start, week_end, len(records))
            logger.info(f"Đã gửi email báo cáo: {filename}")
        except Exception as e:
            logger.error(f"Lỗi gửi email: {e}")

    return send_file(
        io.BytesIO(xlsx_bytes),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


# ────────────────────────────────────────────────
# ROUTES — Web Dashboard
# ────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def dashboard():
    """Web dashboard chính."""
    return render_template("index.html")


@app.route("/api/dashboard-summary", methods=["GET"])
def dashboard_summary():
    """JSON tổng hợp cho dashboard — gọi 1 lần để load toàn bộ."""
    sensor  = _fetch_sensor_latest()
    plant   = _fetch_analysis_latest()
    # Lịch sử cảnh báo 7 ngày
    since = (datetime.now() - timedelta(days=7)).isoformat()
    with _get_db() as conn:
        alerts = conn.execute(
            "SELECT * FROM alerts WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT 20",
            (since,)
        ).fetchall()
    return jsonify({
        "sensor":  sensor,
        "plant":   plant,
        "alerts":  [dict(a) for a in alerts],
    })


# ────────────────────────────────────────────────
# SCHEDULER — Tự động gửi báo cáo Chủ nhật 20:00
# ────────────────────────────────────────────────

def _scheduled_weekly_report():
    week_end   = datetime.now()
    week_start = week_end - timedelta(days=7)
    records    = _fetch_analysis_records(week_start, week_end)
    if not records:
        logger.warning("Scheduler: không có dữ liệu tuần này.")
        return
    xlsx_bytes = generate_weekly_excel(records, week_start)
    filename   = (
        f"BaoCao_CaiNgot_"
        f"{week_start.strftime('%d%m%Y')}-{week_end.strftime('%d%m%Y')}.xlsx"
    )
    try:
        _send_email(xlsx_bytes, filename, week_start, week_end, len(records))
        logger.info(f"Scheduler: đã gửi {filename}")
    except Exception as e:
        logger.error(f"Scheduler lỗi gửi email: {e}")


def init_scheduler():
    scheduler = BackgroundScheduler(timezone="Asia/Ho_Chi_Minh")
    scheduler.add_job(
        func=_scheduled_weekly_report,
        trigger="cron",
        day_of_week="sun",
        hour=20,
        minute=0,
        id="weekly_report",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler sẵn sàng: Chủ nhật 20:00 ICT")
    return scheduler


# ────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────

init_db()
scheduler = init_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT    NOT NULL,
                plant_count     INTEGER,
                missing_json    TEXT,
                avg_canopy_cm2  REAL,
                health_json     TEXT,
                disease_warning INTEGER,
                sick_json       TEXT,
                cell_json       TEXT,
                px_per_cm       REAL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_analysis_ts
            ON analysis_records (timestamp)
        """)

        # Bảng log cảnh báo
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                type      TEXT,
                message   TEXT,
                sent_sms  INTEGER DEFAULT 0
            )
        """)
    logger.info(f"SQLite DB sẵn sàng: {DB_PATH}")


# ────────────────────────────────────────────────
# HELPERS — Sensor
# ────────────────────────────────────────────────

def _insert_sensor(ts, temp, soil, light, stage):
    with _get_db() as conn:
        conn.execute(
            "INSERT INTO sensor_data (timestamp, temperature, soil_moist, light_lux, growth_stage) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, temp, soil, light, stage)
        )


def _fetch_sensor_history(hours=168):
    """Lấy dữ liệu sensor N giờ gần nhất (mặc định 7 ngày = 168h)."""
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sensor_data WHERE timestamp >= ? ORDER BY timestamp ASC",
            (since,)
        ).fetchall()
    return [dict(r) for r in rows]


def _fetch_sensor_latest():
    with _get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sensor_data ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


# ────────────────────────────────────────────────
# HELPERS — Phân tích ảnh
# ────────────────────────────────────────────────

def _insert_analysis(result: dict):
    with _get_db() as conn:
        conn.execute("""
            INSERT INTO analysis_records
                (timestamp, plant_count, missing_json, avg_canopy_cm2,
                 health_json, disease_warning, sick_json, cell_json, px_per_cm)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            result["timestamp"],
            result["plant_count"],
            json.dumps(result.get("missing_positions", []), ensure_ascii=False),
            result.get("avg_canopy_cm2", 0),
            json.dumps(result.get("health_summary", {})),
            int(result.get("disease_warning", False)),
            json.dumps(result.get("sick_positions", []), ensure_ascii=False),
            json.dumps(result.get("cell_results", []), ensure_ascii=False),
            result.get("px_per_cm", 0),
        ))


def _fetch_analysis_records(start: datetime, end: datetime) -> list:
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM analysis_records WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp ASC",
            (start.isoformat(), end.isoformat())
        ).fetchall()
    records = []
    for row in rows:
        records.append({
            "timestamp":         row["timestamp"],
            "plant_count":       row["plant_count"],
            "missing_positions": json.loads(row["missing_json"] or "[]"),
            "avg_canopy_cm2":    row["avg_canopy_cm2"],
            "health_summary":    json.loads(row["health_json"] or "{}"),
            "disease_warning":   bool(row["disease_warning"]),
            "sick_positions":    json.loads(row["sick_json"] or "[]"),
            "cell_results":      json.loads(row["cell_json"] or "[]"),
            "px_per_cm":         row["px_per_cm"],
        })
    return records


def _fetch_analysis_latest() -> dict | None:
    with _get_db() as conn:
        row = conn.execute(
            "SELECT * FROM analysis_records ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return {
        "timestamp":         row["timestamp"],
        "plant_count":       row["plant_count"],
        "missing_positions": json.loads(row["missing_json"] or "[]"),
        "avg_canopy_cm2":    row["avg_canopy_cm2"],
        "health_summary":    json.loads(row["health_json"] or "{}"),
        "disease_warning":   bool(row["disease_warning"]),
        "sick_positions":    json.loads(row["sick_json"] or "[]"),
    }


# ────────────────────────────────────────────────
# HELPERS — SMS ESMS.vn
# ────────────────────────────────────────────────

def _send_sms(message: str):
    """Gửi SMS qua ESMS.vn. Bỏ qua nếu chưa cấu hình."""
    if not ESMS_API_KEY or not SMS_PHONE:
        logger.warning("SMS chưa cấu hình — bỏ qua.")
        return False
    try:
        resp = requests.post(
            "https://rest.esms.vn/MainService.svc/json/SendMultipleMessage_V4_post_json/",
            json={
                "ApiKey":    ESMS_API_KEY,
                "SecretKey": ESMS_SECRET_KEY,
                "Brandname": ESMS_BRANDNAME,
                "SmsType":   "2",
                "Phone":     SMS_PHONE,
                "Content":   message,
            },
            timeout=10,
        )
        result = resp.json()
        if result.get("CodeResult") == "100":
            logger.info(f"SMS đã gửi: {message[:60]}...")
            # Lưu log
            with _get_db() as conn:
                conn.execute(
                    "INSERT INTO alerts (timestamp, type, message, sent_sms) VALUES (?, ?, ?, 1)",
                    (datetime.now().isoformat(), "sms", message)
                )
            return True
        else:
            logger.error(f"ESMS lỗi: {result}")
            return False
    except Exception as e:
        logger.error(f"Lỗi gửi SMS: {e}")
        return False


def _check_and_alert_sensor(temp, soil, light):
    """Kiểm tra ngưỡng và gửi SMS nếu vượt."""
    alerts = []
    if temp is not None and temp > TEMP_MAX:
        alerts.append(f"Nhiệt độ cao {temp}°C (ngưỡng {TEMP_MAX}°C)")
    if temp is not None and temp < TEMP_MIN:
        alerts.append(f"Nhiệt độ thấp {temp}°C (ngưỡng {TEMP_MIN}°C)")
    if soil is not None and soil < SOIL_MIN:
        alerts.append(f"Độ ẩm đất thấp {soil}% (ngưỡng {SOIL_MIN}%)")
    if light is not None and light < LIGHT_MIN:
        alerts.append(f"Ánh sáng yếu {light} lux (ngưỡng {LIGHT_MIN} lux)")

    if alerts:
        msg = "[CẢI NGỌT IoT] CẢNH BÁO: " + " | ".join(alerts)
        _send_sms(msg)


# ────────────────────────────────────────────────
# ROUTES — Sensor Data
# ────────────────────────────────────────────────

@app.route("/api/sensor", methods=["POST"])
def receive_sensor():
    """
    ESP32 POST JSON sensor data lên đây mỗi 20 phút.

    Body JSON:
    {
        "temperature": 28.5,
        "soil_moisture": 65.2,
        "light_lux": 1200,
        "growth_stage": "Vegetative"
    }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Cần JSON body"}), 400

    temp  = data.get("temperature")
    soil  = data.get("soil_moisture")
    light = data.get("light_lux")
    stage = data.get("growth_stage", "Unknown")
    ts    = datetime.now().isoformat()

    _insert_sensor(ts, temp, soil, light, stage)
    _check_and_alert_sensor(temp, soil, light)

    logger.info(f"Sensor: T={temp}°C | Soil={soil}% | Light={light}lux | Stage={stage}")
    return jsonify({"status": "ok", "timestamp": ts}), 200


@app.route("/api/sensor/latest", methods=["GET"])
def sensor_latest():
    """Trả JSON sensor mới nhất (cho Blynk / dashboard)."""
    data = _fetch_sensor_latest()
    if not data:
        return jsonify({"status": "no_data"}), 200
    return jsonify(data)


@app.route("/api/sensor/history", methods=["GET"])
def sensor_history():
    """
    Lịch sử sensor. Query param: hours=168 (mặc định 7 ngày)
    Dùng cho Chart.js trên dashboard.
    """
    hours = int(request.args.get("hours", 168))
    rows  = _fetch_sensor_history(hours)
    return jsonify(rows)


# ────────────────────────────────────────────────
# ROUTES — Ảnh ESP32-CAM
# ────────────────────────────────────────────────

@app.route("/api/upload-image", methods=["POST"])
def upload_image():
    """
    ESP32-CAM POST ảnh JPEG raw.

    Arduino code mẫu:
        HTTPClient http;
        http.begin("https://YOUR_APP.railway.app/api/upload-image");
        http.addHeader("Content-Type", "image/jpeg");
        int code = http.POST(fb->buf, fb->len);
    """
    global _latest_annotated_bytes

    ct = request.content_type or ""
    if not ("jpeg" in ct or "octet-stream" in ct):
        return jsonify({"error": "Chỉ nhận Content-Type: image/jpeg"}), 415

    image_bytes = request.get_data()
    if len(image_bytes) < 1000:
        return jsonify({"error": "Ảnh quá nhỏ hoặc rỗng"}), 400

    result = analyze_image(image_bytes)
    if "error" in result:
        logger.error(f"Lỗi phân tích ảnh: {result['error']}")
        return jsonify(result), 500

    # Lưu ảnh annotated vào RAM để hiển thị
    _latest_annotated_bytes = result.get("debug_image_bytes")

    # Lưu kết quả vào SQLite
    _insert_analysis(result)

    # Gửi SMS nếu phát hiện bệnh
    if result.get("disease_warning"):
        sick = ", ".join(result.get("sick_positions", []))
        _send_sms(f"[CẢI NGỌT IoT] Phát hiện lá bệnh/vàng tại: {sick}. Kiểm tra ngay!")

    # Gửi SMS nếu thiếu cây
    missing = result.get("missing_positions", [])
    if len(missing) >= 3:
        _send_sms(f"[CẢI NGỌT IoT] Thiếu {len(missing)} cây tại: {', '.join(missing)}")

    logger.info(
        f"Ảnh OK: {result['plant_count']}/18 cây | "
        f"Tán lá={result['avg_canopy_cm2']}cm² | "
        f"SK={result['health_summary'].get('health_score')}% | "
        f"Bệnh={result['disease_warning']}"
    )

    return jsonify({
        "status":       "ok",
        "plant_count":  result["plant_count"],
        "missing":      result["missing_positions"],
        "health_score": result["health_summary"].get("health_score"),
        "disease_warn": result["disease_warning"],
        "avg_canopy":   result["avg_canopy_cm2"],
        "timestamp":    result["timestamp"],
    }), 200


@app.route("/api/latest-image", methods=["GET"])
def latest_image():
    """Ảnh JPEG đã annotate (vẽ lưới + nhãn) lần chụp gần nhất."""
    if _latest_annotated_bytes is None:
        return jsonify({"error": "Chưa nhận được ảnh nào từ lần khởi động gần nhất"}), 404
    return send_file(
        io.BytesIO(_latest_annotated_bytes),
        mimetype="image/jpeg",
        as_attachment=False,
        download_name="latest_annotated.jpg",
    )


@app.route("/api/plant-status", methods=["GET"])
def plant_status():
    """Tổng quan cây trồng mới nhất (cho Blynk / dashboard)."""
    data = _fetch_analysis_latest()
    if not data:
        return jsonify({"status": "no_data"}), 200
    return jsonify(data)


@app.route("/api/image-history", methods=["GET"])
def image_history():
    """
    Lịch sử phân tích ảnh. Query param: days=7
    Trả số cây và tán lá theo thời gian cho Chart.js.
    """
    days  = int(request.args.get("days", 7))
    end   = datetime.now()
    start = end - timedelta(days=days)
    rows  = _fetch_analysis_records(start, end)
    # Chỉ trả các trường cần cho biểu đồ
    slim = [
        {
            "timestamp":    r["timestamp"],
            "plant_count":  r["plant_count"],
            "avg_canopy":   r["avg_canopy_cm2"],
            "health_score": r["health_summary"].get("health_score", 0),
        }
        for r in rows
    ]
    return jsonify(slim)


# ────────────────────────────────────────────────
# ROUTES — Xuất Excel
# ────────────────────────────────────────────────

@app.route("/api/export-weekly", methods=["GET"])
def export_weekly():
    """
    Xuất báo cáo Excel.
    Query params:
        days=7   (mặc định 7 ngày)
        send=1   (đồng thời gửi email)

    Ví dụ:
        GET /api/export-weekly?days=7&send=1
    """
    days      = int(request.args.get("days", 7))
    send_mail = request.args.get("send", "0") == "1"

    week_end   = datetime.now()
    week_start = week_end - timedelta(days=days)

    records = _fetch_analysis_records(week_start, week_end)
    if not records:
        return jsonify({"error": f"Không có dữ liệu trong {days} ngày gần nhất"}), 404

    xlsx_bytes = generate_weekly_excel(records, week_start)
    filename   = (
        f"BaoCao_CaiNgot_"
        f"{week_start.strftime('%d%m%Y')}-{week_end.strftime('%d%m%Y')}.xlsx"
    )

    if send_mail:
        try:
            _send_email(xlsx_bytes, filename, week_start, week_end, len(records))
            logger.info(f"Đã gửi email báo cáo: {filename}")
        except Exception as e:
            logger.error(f"Lỗi gửi email: {e}")

    return send_file(
        io.BytesIO(xlsx_bytes),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


# ────────────────────────────────────────────────
# ROUTES — Web Dashboard
# ────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def dashboard():
    """Web dashboard chính."""
    return render_template("index.html")


@app.route("/api/dashboard-summary", methods=["GET"])
def dashboard_summary():
    """JSON tổng hợp cho dashboard — gọi 1 lần để load toàn bộ."""
    sensor  = _fetch_sensor_latest()
    plant   = _fetch_analysis_latest()
    # Lịch sử cảnh báo 7 ngày
    since = (datetime.now() - timedelta(days=7)).isoformat()
    with _get_db() as conn:
        alerts = conn.execute(
            "SELECT * FROM alerts WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT 20",
            (since,)
        ).fetchall()
    return jsonify({
        "sensor":  sensor,
        "plant":   plant,
        "alerts":  [dict(a) for a in alerts],
    })


# ────────────────────────────────────────────────
# SCHEDULER — Tự động gửi báo cáo Chủ nhật 20:00
# ────────────────────────────────────────────────

def _scheduled_weekly_report():
    week_end   = datetime.now()
    week_start = week_end - timedelta(days=7)
    records    = _fetch_analysis_records(week_start, week_end)
    if not records:
        logger.warning("Scheduler: không có dữ liệu tuần này.")
        return
    xlsx_bytes = generate_weekly_excel(records, week_start)
    filename   = (
        f"BaoCao_CaiNgot_"
        f"{week_start.strftime('%d%m%Y')}-{week_end.strftime('%d%m%Y')}.xlsx"
    )
    try:
        _send_email(xlsx_bytes, filename, week_start, week_end, len(records))
        logger.info(f"Scheduler: đã gửi {filename}")
    except Exception as e:
        logger.error(f"Scheduler lỗi gửi email: {e}")


def init_scheduler():
    scheduler = BackgroundScheduler(timezone="Asia/Ho_Chi_Minh")
    scheduler.add_job(
        func=_scheduled_weekly_report,
        trigger="cron",
        day_of_week="sun",
        hour=20,
        minute=0,
        id="weekly_report",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler sẵn sàng: Chủ nhật 20:00 ICT")
    return scheduler


# ────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────

init_db()
scheduler = init_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
