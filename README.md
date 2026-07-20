# 🌿 Hệ thống IoT Giám sát Sinh trưởng Cải Ngọt

Đồ án tốt nghiệp — Hệ thống giám sát và điều khiển tự động môi trường trồng cải ngọt (cải bẹ xanh) ứng dụng IoT, kết hợp cảm biến môi trường, xử lý ảnh (Computer Vision) và cảnh báo qua Telegram.

**Thực hiện bởi:** Trần Trí & Trần Văn Thịnh

---

## 📋 Tổng quan hệ thống

Hệ thống giám sát 16 cây cải ngọt trồng trong khay 40×40cm (lưới 4×4), tự động:
- Đo nhiệt độ, độ ẩm không khí, độ ẩm đất, cường độ ánh sáng theo thời gian thực.
- Tự động tưới nước (bơm dạng xung) và bật quạt thông gió theo ngưỡng ứng với từng giai đoạn sinh trưởng.
- Chụp ảnh định kỳ (5 lần/ngày) và phân tích màu sắc lá (OpenCV) để đánh giá sức khoẻ từng cây.
- Gửi cảnh báo tức thời qua Telegram Bot khi có bất thường.
- Xuất báo cáo Excel hàng tuần và gửi qua email tự động.
- Dashboard web theo dõi trực tiếp, điều khiển thủ công/tự động.

---

## 🏗️ Kiến trúc hệ thống

```
┌─────────────────┐     HTTP POST      ┌──────────────────────┐
│  ESP32 38-pin    │ ─────────────────► │                      │
│  (DHT22, BH1750, │   /api/sensor      │   Flask Backend      │
│   Soil, Relay)   │ ◄───────────────── │   (Render.com)       │
└─────────────────┘   lệnh điều khiển   │                      │
                                         │  - PostgreSQL        │
┌─────────────────┐     HTTP POST      │  - APScheduler       │
│  ESP32-CAM       │ ─────────────────► │  - OpenCV analysis   │
│  (Deep Sleep,    │  /api/upload-image │  - Telegram Bot      │
│   chụp 5 lần/ngày)│                    │  - Email SMTP        │
└─────────────────┘                     └──────────┬───────────┘
                                                    │
                                         ┌──────────▼───────────┐
                                         │  Dashboard Web        │
                                         │  (templates/index.html)│
                                         └───────────────────────┘
```

---

## 🔧 Phần cứng

| Thành phần | Model |
|---|---|
| Vi điều khiển chính | ESP32 38-pin |
| Module camera | ESP32-CAM AI-Thinker + MB carrier board |
| Cảm biến nhiệt độ/độ ẩm | DHT22 |
| Cảm biến ánh sáng | BH1750 |
| Cảm biến độ ẩm đất | Capacitive Soil Moisture v1.2 |
| Module relay | SONGLE SRD-12VDC-SL-C (2 kênh, Active LOW) |
| Bơm nước | Motor 365 12V |
| Quạt thông gió | WFX W8025SM 12V brushless |
| Nguồn hạ áp | LM2596 buck converter |
| Nạp code ESP32-CAM | Adapter MG-328 (FTDI) |

**Lưu ý wiring:** Relay dùng logic Active LOW, đấu terminal NO, nguồn 12V vào DC+/DC−, chung GND với ESP32. Nên gắn diode chống dòng ngược (flyback diode) 1N4007 song song với bơm/quạt.

---

## 💻 Công nghệ sử dụng

- **Firmware:** Arduino (C++) cho ESP32 & ESP32-CAM
- **Backend:** Python 3 + Flask, Gunicorn
- **Database:** PostgreSQL (Render free tier)
- **Xử lý ảnh:** OpenCV (opencv-python-headless)
- **Lịch chạy nền:** APScheduler
- **Thông báo:** Telegram Bot API
- **Báo cáo:** openpyxl (Excel) + SMTP Gmail
- **Hosting:** Render.com (free tier, region Singapore) + UptimeRobot (ping giữ service không sleep)

---

## 📂 Cấu trúc thư mục

```
.
├── app.py                  # Flask backend chính — routes, scheduler, logic điều khiển
├── image_analysis.py       # Phân tích ảnh OpenCV (HSV, lưới 4x4, đánh giá sức khoẻ lá)
├── weekly_report.py        # Sinh báo cáo Excel hàng tuần + gửi email
├── backup_db.py            # Backup/restore PostgreSQL qua Telegram (DB free hết hạn 30 ngày)
├── requirements.txt        # Thư viện Python
├── Procfile                # Lệnh khởi động Gunicorn cho Render
├── templates/
│   └── index.html          # Dashboard web
├── .env.example            # Mẫu biến môi trường
├── .gitignore
├── ESP32_Main.ino          # Firmware ESP32 38-pin (cảm biến + relay)
└── ESP32CAM_Main.ino       # Firmware ESP32-CAM (chụp ảnh định kỳ, Deep Sleep)
```

---

## ⚙️ Cài đặt & Triển khai

### 1. Firmware ESP32

Mở `ESP32_Main.ino` và `ESP32CAM_Main.ino`, sửa 3 dòng cấu hình đầu file:

```cpp
const char* WIFI_SSID     = "TEN_WIFI_CUA_BAN";
const char* WIFI_PASSWORD = "MAT_KHAU_WIFI";
const char* SERVER_URL    = "https://ten-app-cua-ban.onrender.com";
```

Nạp `ESP32_Main.ino` vào ESP32 38-pin, và `ESP32CAM_Main.ino` vào ESP32-CAM (nối GPIO0 → GND khi nạp, tháo ra khi chạy thật).

### 2. Backend trên Render.com

1. Tạo **Web Service** mới trên Render, kết nối repo GitHub này.
2. Tạo **PostgreSQL** database (free tier) trên Render, lấy **Internal Database URL**.
3. Vào **Environment**, thêm các biến (xem đầy đủ trong `.env.example`):

   | Biến | Mô tả |
   |---|---|
   | `DATABASE_URL` | Internal DB URL từ Render Postgres |
   | `TELEGRAM_TOKEN` | Token bot Telegram (từ BotFather) |
   | `TELEGRAM_CHAT_ID` | Chat ID nhận cảnh báo |
   | `EMAIL_SENDER` | Gmail gửi báo cáo |
   | `EMAIL_PASSWORD` | App Password Gmail (không phải mật khẩu đăng nhập) |
   | `EMAIL_RECIPIENT` | Email nhận báo cáo tuần |
   | `TEMP_MIN/MAX`, `SOIL_MIN/MAX`, `LIGHT_MIN` | Ngưỡng cảnh báo mặc định |

4. Build command: `pip install -r requirements.txt`
   Start command: (đã có sẵn trong `Procfile`)
5. Sau khi deploy, vào **UptimeRobot**, tạo monitor ping URL app mỗi 5 phút để tránh service bị sleep (free tier).

### 3. Chạy thử

- Kiểm tra dashboard tại `https://ten-app-cua-ban.onrender.com`.
- Bật ESP32, xem log Serial xác nhận gửi dữ liệu thành công (`HTTP 200`).
- Chờ đến khung giờ chụp gần nhất (6:00/9:00/12:00/15:00/18:00) để ESP32-CAM gửi ảnh, hoặc test thủ công bằng cách sửa tạm `SHOOT_HOURS`.

---

## 🔌 API chính (Flask)

| Endpoint | Method | Mô tả |
|---|---|---|
| `/api/sensor` | POST | ESP32 lưu dữ liệu vào DB (mỗi 30 phút) |
| `/api/sensor/live` | POST | ESP32 gửi dữ liệu tức thời cho dashboard (mỗi 1 phút) |
| `/api/sensor/latest` | GET | Dashboard poll dữ liệu mới nhất (mỗi 10 giây) |
| `/api/sensor/history` | GET | Lịch sử cảm biến theo khoảng giờ |
| `/api/device-state` | GET | ESP32 poll lệnh điều khiển thủ công (mỗi 30 giây) |
| `/api/control` | POST | Dashboard gửi lệnh bật/tắt/đổi chế độ bơm-quạt |
| `/api/growth-stage` | GET/POST | Xem/đổi giai đoạn sinh trưởng (4 giai đoạn) |
| `/api/upload-image` | POST | ESP32-CAM gửi ảnh JPEG thô để phân tích |
| `/api/latest-image` | GET | Lấy ảnh mới nhất hiển thị dashboard |
| `/api/image-history` | GET | Lịch sử số cây & điểm sức khoẻ theo ngày |
| `/api/dashboard-summary` | GET | Tổng hợp dữ liệu hiển thị dashboard |
| `/api/export-weekly` | GET | Xuất/gửi báo cáo Excel (`?days=7&send=1`) |

---

## 🌱 4 giai đoạn sinh trưởng & ngưỡng điều khiển

| Giai đoạn | Độ ẩm đất | Nhiệt độ bật quạt |
|---|---|---|
| Nảy mầm | 60–80% | > 30°C |
| Cây con | 55–75% | > 32°C |
| Sinh trưởng | 45–70% | > 35°C |
| Thu hoạch | 40–65% | > 35°C |

Ngưỡng có thể đổi trực tiếp trên dashboard mà **không cần nạp lại firmware**.

---

## 🛡️ Cơ chế an toàn & dự phòng

- **Bơm dạng xung** (5s bơm → 3s nghỉ → đọc lại đất), tự dừng nếu tổng thời gian bơm vượt 60s mà đất vẫn chưa đủ ẩm (nghi lỗi cảm biến/tắc ống/hỏng bơm).
- **Tự reconnect WiFi** nếu ESP32 bị rớt mạng giữa chừng.
- **Backup DB tự động** 2 lần/tuần (Thứ Hai & Thứ Năm, 6:00 sáng) qua Telegram, vì PostgreSQL free tier của Render hết hạn sau 30 ngày.
- **Cảnh báo Telegram** có cooldown riêng theo từng loại để tránh spam tin nhắn.

---

## 📄 Giấy phép

Đồ án phục vụ mục đích học tập — Trường [Tên trường], Khoa Điện – Điện tử.
