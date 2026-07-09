/*
 * ============================================================
 * ĐỒ ÁN TỐT NGHIỆP
 * File  : ESP32CAM_Main.ino
 * Board : ESP32-CAM AI-Thinker
 * ============================================================
 *
 * LỊCH CHỤP ẢNH: 6:00 / 9:00 / 12:00 / 15:00 / 18:00
 * CHẾ ĐỘ NGỦ  : Deep Sleep giữa các lần chụp (~3 tiếng)
 *
 * NGUYÊN LÝ HOẠT ĐỘNG:
 *   - Mỗi lần wake up: kết nối WiFi → đồng bộ NTP → kiểm tra giờ
 *   - Nếu đúng giờ chụp (±10 phút): chụp ảnh → gửi server → sleep
 *   - Tính thời gian sleep đến lần chụp tiếp theo rồi mới ngủ
 *   - Deep Sleep tiêu thụ ~10µA thay vì ~200mA lúc thức
 *
 * LƯU Ý PHẦN CỨNG:
 *   - GPIO0 phải để HỞ khi vận hành (không nối GND)
 *   - Chỉ nối GPIO0 → GND khi nạp code bằng FTDI
 *   - Nguồn 5V ổn định ≥ 500mA (qua Buck Converter)
 * ============================================================
 */

#include "esp_camera.h"
#include "esp_sleep.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <time.h>

// ------------------------------------------------------------
// [1] CẤU HÌNH
// ------------------------------------------------------------
const char* WIFI_SSID     = "TEN_WIFI_CUA_BAN";
const char* WIFI_PASSWORD = "MAT_KHAU_WIFI";
const char* SERVER_URL    = "https://ten-app-cua-ban.onrender.com"; // TODO: đổi sang domain Render thật của bạn

// ------------------------------------------------------------
// [2] LỊCH CHỤP ẢNH
//     Chỉ chụp đúng các giờ này (6, 9, 12, 15, 18)
// ------------------------------------------------------------
const int SHOOT_HOURS[]  = {6, 9, 12, 15, 18};
const int NUM_SLOTS      = 5;
const int WINDOW_MINUTES = 10; // Chấp nhận wake up sớm/trễ ±10 phút

// ------------------------------------------------------------
// [3] GPIO CAMERA - AI-Thinker (KHÔNG THAY ĐỔI)
// ------------------------------------------------------------
#define PWDN_GPIO_NUM  32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM   0
#define SIOD_GPIO_NUM  26
#define SIOC_GPIO_NUM  27
#define Y9_GPIO_NUM    35
#define Y8_GPIO_NUM    34
#define Y7_GPIO_NUM    39
#define Y6_GPIO_NUM    36
#define Y5_GPIO_NUM    21
#define Y4_GPIO_NUM    19
#define Y3_GPIO_NUM    18
#define Y2_GPIO_NUM     5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM  23
#define PCLK_GPIO_NUM  22
#define FLASH_PIN       4

// ============================================================
//  KHỞI TẠO CAMERA
// ============================================================
bool initCamera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM; config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM; config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM; config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM; config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk     = XCLK_GPIO_NUM;
  config.pin_pclk     = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM;
  config.pin_href     = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn     = PWDN_GPIO_NUM;
  config.pin_reset    = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;

  if (psramFound()) {
    config.frame_size   = FRAMESIZE_VGA;
    config.jpeg_quality = 10;
    config.fb_count     = 2;
  } else {
    config.frame_size   = FRAMESIZE_QVGA;
    config.jpeg_quality = 12;
    config.fb_count     = 1;
  }

  if (esp_camera_init(&config) != ESP_OK) return false;

  sensor_t* s = esp_camera_sensor_get();
  s->set_brightness(s, 1);
  s->set_saturation(s, 1);
  s->set_whitebal(s, 1);
  s->set_awb_gain(s, 1);
  s->set_exposure_ctrl(s, 1);
  s->set_aec2(s, 1);
  s->set_gainceiling(s, GAINCEILING_4X);
  return true;
}

// ============================================================
//  KẾT NỐI WiFi
// ============================================================
bool connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("[..] WiFi");
  for (int i = 0; i < 20 && WiFi.status() != WL_CONNECTED; i++) {
    delay(500); Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf(" OK (%s)\n", WiFi.localIP().toString().c_str());
    return true;
  }
  Serial.println(" FAIL");
  return false;
}

// ============================================================
//  ĐỒNG BỘ THỜI GIAN NTP
// ============================================================
bool syncTime() {
  configTime(7 * 3600, 0, "pool.ntp.org");
  Serial.print("[..] NTP");
  time_t now = time(nullptr);
  for (int i = 0; now < 100000 && i < 20; i++) {
    delay(500); Serial.print("."); now = time(nullptr);
  }
  if (now > 100000) { Serial.println(" OK"); return true; }
  Serial.println(" FAIL");
  return false;
}

// ============================================================
//  TÍNH THỜI GIAN SLEEP ĐẾN LẦN CHỤP TIẾP THEO
// ============================================================
uint64_t calcSleepMicros(struct tm* now) {
  int curMinutes = now->tm_hour * 60 + now->tm_min;

  // Tìm slot chụp tiếp theo
  int nextSlotMinutes = -1;
  for (int i = 0; i < NUM_SLOTS; i++) {
    int slotMinutes = SHOOT_HOURS[i] * 60;
    // Thêm buffer nhỏ để tránh wake up quá sớm ngay slot vừa chụp
    if (slotMinutes > curMinutes + WINDOW_MINUTES) {
      nextSlotMinutes = slotMinutes;
      break;
    }
  }

  // Nếu đã qua hết slot trong ngày → sleep đến 6:00 hôm sau
  if (nextSlotMinutes == -1) {
    int minutesUntilMidnight = 24 * 60 - curMinutes;
    int minutesFrom6am       = SHOOT_HOURS[0] * 60;
    nextSlotMinutes = curMinutes + minutesUntilMidnight + minutesFrom6am;
    Serial.printf("[Sleep] Da qua het slot hom nay → sleep den 6:00 sang mai\n");
  } else {
    int hours = nextSlotMinutes / 60;
    int mins  = nextSlotMinutes % 60;
    Serial.printf("[Sleep] Slot tiep theo: %02d:%02d\n", hours, mins);
  }

  int sleepMinutes = nextSlotMinutes - curMinutes;

  // Trừ bớt 1 phút để wake up sớm hơn slot 1 chút (đủ thời gian kết nối WiFi+NTP)
  sleepMinutes = max(sleepMinutes - 1, 1);

  Serial.printf("[Sleep] Ngu %d phut (%d gio %d phut)\n",
    sleepMinutes, sleepMinutes / 60, sleepMinutes % 60);

  return (uint64_t)sleepMinutes * 60ULL * 1000000ULL; // đổi ra microseconds
}

// ============================================================
//  KIỂM TRA CÓ ĐÚNG GIỜ CHỤP KHÔNG
// ============================================================
bool isShootTime(struct tm* ti) {
  int curMinutes = ti->tm_hour * 60 + ti->tm_min;
  for (int i = 0; i < NUM_SLOTS; i++) {
    int slotMinutes = SHOOT_HOURS[i] * 60;
    if (abs(curMinutes - slotMinutes) <= WINDOW_MINUTES) {
      return true;
    }
  }
  return false;
}

// ============================================================
//  CHỤP ẢNH VÀ GỬI SERVER
// ============================================================
void captureAndSend(const char* timeStr) {
  Serial.printf("[Chup] %s\n", timeStr);

  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) { Serial.println("[!!] Chup FAIL"); return; }

  Serial.printf("[OK] %zu bytes (%dx%d)\n", fb->len, fb->width, fb->height);

  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    // Server (app.py) đọc thẳng request.get_data() như JPEG thô,
    // KHÔNG parse multipart -> phải gửi raw bytes, không bọc form-data.
    http.begin(String(SERVER_URL) + "/api/upload-image");
    http.setTimeout(30000);
    http.addHeader("Content-Type", "image/jpeg");

    int code = http.POST(fb->buf, fb->len);
    Serial.printf("[Server] HTTP %d\n", code);
    http.end();
  }

  // QUAN TRỌNG: trả buffer trước khi sleep
  esp_camera_fb_return(fb);
}

// ============================================================
//  SETUP - Chạy mỗi lần wake up từ Deep Sleep
// ============================================================
void setup() {
  Serial.begin(115200);
  Serial.println("\n=== ESP32-CAM WAKE UP ===");
  pinMode(FLASH_PIN, OUTPUT);
  digitalWrite(FLASH_PIN, LOW);

  // Kết nối WiFi
  if (!connectWiFi()) {
    Serial.println("[!!] WiFi fail → sleep 30 phut roi thu lai");
    esp_sleep_enable_timer_wakeup(30ULL * 60 * 1000000);
    esp_deep_sleep_start();
  }

  // Đồng bộ NTP
  if (!syncTime()) {
    Serial.println("[!!] NTP fail → sleep 10 phut roi thu lai");
    WiFi.disconnect(true);
    esp_sleep_enable_timer_wakeup(10ULL * 60 * 1000000);
    esp_deep_sleep_start();
  }

  // Lấy giờ hiện tại
  time_t now = time(nullptr);
  struct tm* ti = localtime(&now);
  char timeStr[25];
  strftime(timeStr, sizeof(timeStr), "%Y-%m-%d %H:%M:%S", ti);
  Serial.printf("[Gio] %s\n", timeStr);

  // Kiểm tra có đúng giờ chụp không
  if (isShootTime(ti)) {
    // Khởi tạo camera và chụp
    if (initCamera()) {
      delay(1000); // Ổn định camera
      captureAndSend(timeStr);
    } else {
      Serial.println("[!!] Camera FAIL");
    }
  } else {
    Serial.printf("[Skip] %02d:%02d khong phai gio chup\n", ti->tm_hour, ti->tm_min);
  }

  // Tính thời gian sleep và vào Deep Sleep
  uint64_t sleepMicros = calcSleepMicros(ti);
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  Serial.println("[Sleep] Vao Deep Sleep...");
  Serial.flush();

  esp_sleep_enable_timer_wakeup(sleepMicros);
  esp_deep_sleep_start();
  // Code bên dưới KHÔNG BAO GIỜ chạy đến
}

void loop() {
  // Không dùng loop() khi có Deep Sleep
  // Mỗi lần wake up sẽ chạy lại setup() từ đầu
}
