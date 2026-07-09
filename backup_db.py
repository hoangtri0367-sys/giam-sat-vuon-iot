"""
backup_db.py — Tự động backup PostgreSQL (Render free DB hết hạn sau 30 ngày)

Dùng pure Python (psycopg2) thay vì pg_dump, vì Render's free Python
runtime KHÔNG có pg_dump / postgresql-client cài sẵn và không cho
cài thêm system package (apt) trên native runtime.

Cách hoạt động:
  - Đọc toàn bộ dữ liệu từng bảng qua psycopg2 (SELECT * FROM ...)
  - Gộp lại thành 1 file JSON, nén gzip
  - Gửi file qua Telegram Bot (đây là nơi lưu trữ lâu dài thật sự,
    vì ổ đĩa trên Render free cũng bị xóa mỗi khi service restart)

Cách dùng:
  - Chạy độc lập:  python backup_db.py
  - Hoặc import run_backup() để gọi từ scheduler trong app.py (đã làm sẵn)

Khôi phục dữ liệu khi cần (ví dụ tạo DB Postgres mới sau khi DB cũ hết hạn):
  python backup_db.py --restore duong_dan_file_backup.json.gz
"""

import gzip
import json
import logging
import os
import sys
from datetime import datetime

import psycopg2
import psycopg2.extras
import requests

logger = logging.getLogger(__name__)

DATABASE_URL     = os.environ.get("DATABASE_URL", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BACKUP_DIR = "backups"

# Các bảng cần backup — khớp với schema trong app.py (init_db)
TABLES = ["sensor_data", "analysis_records", "alerts", "relay_log"]


def _export_all_tables() -> dict:
    """Đọc toàn bộ dữ liệu từ các bảng, trả về dict {ten_bang: [rows]}."""
    if not DATABASE_URL:
        raise RuntimeError("Thiếu biến môi trường DATABASE_URL")

    data = {}
    conn = psycopg2.connect(DATABASE_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    try:
        cur = conn.cursor()
        for table in TABLES:
            cur.execute(f"SELECT * FROM {table} ORDER BY id ASC")
            rows = cur.fetchall()
            # RealDictRow không tự serialize JSON được -> ép về dict thường
            data[table] = [dict(r) for r in rows]
            logger.info(f"  - {table}: {len(rows)} dòng")
        cur.close()
    finally:
        conn.close()
    return data


def _write_json_gz(data: dict, out_path: str) -> str:
    """Ghi dict ra file .json.gz, trả về đường dẫn."""
    payload = {
        "exported_at": datetime.now().isoformat(),
        "tables": data,
    }
    with gzip.open(out_path, "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=str)
    return out_path


def _send_telegram_document(file_path: str, caption: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Thiếu TELEGRAM_TOKEN/TELEGRAM_CHAT_ID — bỏ qua gửi backup")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    with open(file_path, "rb") as f:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
            files={"document": (os.path.basename(file_path), f)},
            timeout=60,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Gửi Telegram thất bại: {resp.status_code} {resp.text}")


def run_backup() -> str:
    """Hàm chính: đọc DB -> ghi JSON nén -> gửi Telegram. Trả về đường dẫn file đã gửi."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path  = os.path.join(BACKUP_DIR, f"backup_{timestamp}.json.gz")

    logger.info("Bắt đầu backup database...")
    data = _export_all_tables()
    _write_json_gz(data, out_path)

    total_rows = sum(len(v) for v in data.values())
    size_kb = os.path.getsize(out_path) / 1024
    caption = (
        f"🗄 Backup CSDL Cải Ngọt\n"
        f"Thời gian: {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
        f"Tổng số dòng: {total_rows}\n"
        f"Dung lượng: {size_kb:.1f} KB"
    )

    _send_telegram_document(out_path, caption)
    logger.info(f"Đã gửi backup qua Telegram: {out_path}")

    return out_path


def restore_backup(gz_path: str) -> None:
    """Khôi phục dữ liệu từ file backup .json.gz vào DATABASE_URL hiện tại.
    Dùng khi tạo DB Postgres mới (vì DB cũ hết hạn 30 ngày) và muốn nạp lại dữ liệu cũ.
    LƯU Ý: cần đã chạy init_db() (tạo bảng) trước khi restore.
    """
    if not DATABASE_URL:
        raise RuntimeError("Thiếu biến môi trường DATABASE_URL")

    with gzip.open(gz_path, "rt", encoding="utf-8") as f:
        payload = json.load(f)

    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        for table, rows in payload["tables"].items():
            if not rows:
                continue
            columns = list(rows[0].keys())
            col_names = ", ".join(columns)
            placeholders = ", ".join(["%s"] * len(columns))
            # ON CONFLICT DO NOTHING: cho phép chạy lại restore nhiều lần mà không lỗi trùng khóa
            insert_sql = (
                f"INSERT INTO {table} ({col_names}) VALUES ({placeholders}) "
                f"ON CONFLICT (id) DO NOTHING"
            )
            values = [[row[c] for c in columns] for row in rows]
            cur.executemany(insert_sql, values)
            logger.info(f"  - Đã khôi phục {len(rows)} dòng vào {table}")

            # QUAN TRỌNG: cập nhật lại sequence của cột id (SERIAL) sau khi insert
            # thủ công giá trị id có sẵn — nếu không, insert tiếp theo (ví dụ ESP32
            # gửi sensor mới) có thể bị lỗi "duplicate key value violates unique constraint"
            if "id" in columns:
                cur.execute(
                    f"SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 1))",
                    (table,),
                )
        conn.commit()
        cur.close()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    logger.info(f"Khôi phục hoàn tất từ {gz_path}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    if len(sys.argv) > 1 and sys.argv[1] == "--restore":
        if len(sys.argv) < 3:
            print("Cách dùng: python backup_db.py --restore duong_dan_file.json.gz")
            sys.exit(1)
        restore_backup(sys.argv[2])
    else:
        run_backup()
