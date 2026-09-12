/*
 * ============================================================
 * ĐỒ ÁN TỐT NGHIỆP
 * File  : ESP32CAM_Main.ino
 * Board : ESP32-CAM AI-Thinker
 * ============================================================
 *
 * LỊCH CHỤP ẢNH: 3 lần/ngày, 7:00 / 12:00 / 17:00 (tự động)
 *                + CHỤP THỦ CÔNG bất kỳ lúc nào qua nút bấm trên dashboard
 * CHẾ ĐỘ HOẠT ĐỘNG: LUÔN THỨC (không Deep Sleep) — phù hợp khi cắm nguồn
 *                    tường liên tục (Adapter 12V/5A qua LM2596), không chạy pin.
 *
 * NGUYÊN LÝ HOẠT ĐỘNG:
 *   - setup(): kết nối WiFi + đồng bộ NTP 1 LẦN DUY NHẤT. Camera CHỈ bật khi
 *     thật sự cần chụp (tự động hoặc thủ công), tắt (PWDN) ngay sau khi xong
 *     -> giảm nhiệt đáng kể so với giữ camera chạy 24/7.
 *   - loop(): lặp liên tục, mỗi vòng kiểm tra:
 *       1) WiFi có bị mất không (VD: tắt hotspot điện thoại để sạc pin) ->
 *          tự động kết nối lại, không cần khởi động lại board.
 *       2) Có yêu cầu "chụp thủ công" từ dashboard không (poll server mỗi ~10s).
 *       3) Có đúng giờ chụp lịch trình (7h/12h/17h) và chưa chụp giờ đó chưa.
 *
 * LƯU Ý PHẦN CỨNG:
 *   - GPIO0 phải để HỞ khi vận hành (không nối GND)
 *   - Chỉ nối GPIO0 → GND khi nạp code bằng FTDI
 *   - Nguồn 5V ổn định ≥ 500mA (qua Buck Converter LM2596)
 *   - Khuyến nghị: tụ lọc 470-1000µF/25V ngay tại chân 5V/GND của board
 *     ESP32-CAM để chống brownout lúc khởi động/phát WiFi.
 * ============================================================
 */

#include "esp_camera.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <time.h>
#include "esp_wifi.h"

// ------------------------------------------------------------
// [1] CẤU HÌNH
// ------------------------------------------------------------
const char* WIFI_SSID     = "TRANTRI";
const char* WIFI_PASSWORD = "trantritri";
const char* SERVER_URL    = "https://giam-sat-vuon-iot.onrender.com"; // TODO: đổi sang domain Render thật của bạn

// ------------------------------------------------------------
// [2] LỊCH CHỤP ẢNH TỰ ĐỘNG
//     Chụp 3 lần/ngày: 7:00 (sáng) / 12:00 (trưa) / 17:00 (chiều)
// ------------------------------------------------------------
const int SHOOT_HOURS[]  = {7, 12, 17};
const int NUM_SLOTS      = 3;

// Gio (0-23) da chup thanh cong gan nhat trong ngay. Vi khong con Deep Sleep,
// dung bien RAM binh thuong la du (mat gia tri khi mat nguon/RST, chap nhan
// duoc vi board luon thuc lien tuc, hiem khi reset giua chung).
int lastCapturedHour = -1;
bool ntpSynced = false; // Đánh dấu đã từng đồng bộ NTP thành công lần nào chưa — dùng để thử
                         // đồng bộ NGAY khi WiFi có lại, không chờ đủ NTP_RESYNC_INTERVAL (1 tiếng).

// ------------------------------------------------------------
// [3] CÁC MỐC THỜI GIAN LẶP TRONG loop() (đơn vị: mili-giây)
// ------------------------------------------------------------
const unsigned long WIFI_CHECK_INTERVAL     = 5000;   // Kiểm tra & tự reconnect WiFi mỗi 5s (khi đang OK)
const unsigned long WIFI_RETRY_BACKOFF_MAX  = 300000; // Nếu mất WiFi liên tục (VD: tắt hotspot ban đêm),
                                                        // giãn dần thời gian chờ giữa các lần thử, tối đa 5 phút
const unsigned long MANUAL_POLL_INTERVAL    = 10000;  // Hỏi server có yêu cầu chụp thủ công mỗi 10s
const unsigned long SCHEDULE_CHECK_INTERVAL = 30000;  // Kiểm tra lịch chụp tự động mỗi 30s
const unsigned long NTP_RESYNC_INTERVAL     = 3600000UL; // Đồng bộ lại giờ NTP mỗi 1 tiếng (chống trôi giờ)
const unsigned long CAPTURE_RETRY_INTERVAL  = 60000;  // Nếu chụp/gửi thất bại, thử lại sau 60s
const unsigned long LOW_RAM_CHECK_INTERVAL  = 60000;   // Kiểm tra RAM trống mỗi 60s
const uint32_t      LOW_RAM_THRESHOLD       = 30000;   // Dưới 30KB RAM trống -> coi là nguy hiểm,
                                                         // tự restart để tránh crash không kiểm soát
                                                         // (VD: đúng lúc đang chụp/gửi ảnh giữa chừng).
                                                         // Chỉ can thiệp khi thật sự cần, không restart
                                                         // định kỳ vô cớ.

// ------------------------------------------------------------
// [4] GPIO CAMERA - AI-Thinker (KHÔNG THAY ĐỔI)
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
//  KHỞI TẠO CAMERA — gọi TRƯỚC MỖI LẦN CHỤP, deinit ngay sau đó để tắt nguồn
//  cảm biến (PWDN) giữa các lần chụp, giảm nhiệt khi chạy 24/7 không nghỉ.
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
  s->set_saturation(s, 2); // Giữ nguyên 2 — ảnh xám/nhợt là do overexposed (đã sửa qua gain/AE
                            // bên dưới), không phải do saturation quá cao.
  s->set_whitebal(s, 1);
  s->set_awb_gain(s, 1);
  s->set_wb_mode(s, 0);      // Ép rõ chế độ Auto White Balance (mode 0)
  s->set_exposure_ctrl(s, 1);
  s->set_ae_level(s, -1);    // Giảm nhẹ mức phơi sáng mục tiêu — tránh overexposed
  s->set_aec2(s, 1);         // Bật AEC nâng cao — bù trừ tốt hơn khi ánh sáng ổn định
  s->set_gainceiling(s, GAINCEILING_2X); // Gain thấp — đủ sáng, tránh xám nhợt
  return true;
}

// ============================================================
//  "HÂM NÓNG" CẢM BIẾN — chụp bỏ vài frame để AWB/AE hội tụ đúng màu
//  Bộ Auto White Balance / Auto Exposure của OV2640 CHỈ cập nhật giá trị
//  khi có frame THỰC SỰ được đọc ra khỏi cảm biến — delay() thụ động không
//  giúp ích gì. Gọi trước MỖI lần chụp thật (không chỉ lúc init), vì ánh
//  sáng môi trường có thể đã đổi khác kể từ lần chụp trước.
// ============================================================
void warmupCamera(int frames = 8) {
  Serial.printf("[Cam] Warm-up AWB/AE (%d frame)...\n", frames);
  for (int i = 0; i < frames; i++) {
    camera_fb_t* wfb = esp_camera_fb_get();
    if (wfb) esp_camera_fb_return(wfb);
    delay(150); // Giữa mỗi frame, cho bộ AWB/AE 1 nhịp để cập nhật giá trị mới
  }
}

// ============================================================
//  TẮT CAMERA — gọi ngay sau mỗi lần chụp xong, để cảm biến không phải chạy
//  clock liên tục 24/7 (giảm nhiệt đáng kể). Chủ động kéo PWDN lên HIGH sau
//  deinit để đảm bảo chắc chắn cảm biến ở trạng thái tắt (không để chân đó
//  trôi nổi — floating pin có thể vô tình giữ cảm biến ở trạng thái bật).
// ============================================================
void deinitCamera() {
  esp_camera_deinit();
  pinMode(PWDN_GPIO_NUM, OUTPUT);
  digitalWrite(PWDN_GPIO_NUM, HIGH); // HIGH = power down (theo datasheet OV2640)
}

// ============================================================
//  IN THONG TIN MANG (IP / Gateway / RSSI)
// ============================================================
void printNetworkInfo() {
  Serial.printf("[Network] IP: %s | GW: %s | RSSI: %d dBm\n",
    WiFi.localIP().toString().c_str(),
    WiFi.gatewayIP().toString().c_str(),
    WiFi.RSSI());
}

// ============================================================
//  KẾT NỐI WiFi — dùng cả lúc khởi động lẫn lúc tự reconnect trong loop()
// ============================================================
bool connectWiFi() {
  // Ngat ket noi cu triet de truoc khi thu lai — neu khong lam buoc nay, driver WiFi noi
  // bo co the con dang o trang thai "connecting" do dang (tu lan thu truoc chua kip xong/
  // timeout giua chung), khien WiFi.begin() moi bi tu choi ap cau hinh, bao loi
  // "wifi:sta is connecting, cannot set config".
  WiFi.disconnect(true, true);
  delay(200);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);   // Tắt WiFi Power Save — tránh lỗi "addba response cb: sta conn deleted"
  esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("[..] WiFi");
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {
    delay(500); Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf(" OK (%s)\n", WiFi.localIP().toString().c_str());
    printNetworkInfo();
    return true;
  }
  Serial.println(" FAIL");
  return false;
}

// ============================================================
//  ĐỒNG BỘ THỜI GIAN NTP
// ============================================================
bool syncTime() {
  configTime(7 * 3600, 0, "time.google.com", "time.cloudflare.com", "pool.ntp.org");
  Serial.print("[..] NTP");
  time_t now = time(nullptr);
  for (int i = 0; now < 100000 && i < 30; i++) {
    delay(500); Serial.print("."); now = time(nullptr);
  }
  if (now > 100000) { Serial.println(" OK"); return true; }
  Serial.println(" FAIL");
  return false;
}

// ============================================================
//  KIỂM TRA GIỜ HIỆN TẠI CÓ PHẢI GIỜ CHỤP VÀ CHƯA CHỤP XONG
// ============================================================
bool needCaptureThisHour(struct tm* ti) {
  if (ti->tm_hour == lastCapturedHour) return false; // gio nay chup roi
  for (int i = 0; i < NUM_SLOTS; i++) {
    if (SHOOT_HOURS[i] == ti->tm_hour) return true;
  }
  return false;
}

// ============================================================
//  CHỤP ẢNH + GỬI SERVER (dùng chung cho cả lịch tự động lẫn chụp thủ công)
// ============================================================
bool captureAndSend() {
  time_t now = time(nullptr);
  struct tm* ti = localtime(&now);
  char timeStr[25];
  strftime(timeStr, sizeof(timeStr), "%Y-%m-%d %H:%M:%S", ti);
  Serial.printf("[Chup] %s | RAM trong: %u byte\n", timeStr, ESP.getFreeHeap());

  // Bat nguon + khoi tao camera NGAY LUC CAN CHUP (thay vi giu chay lien tuc 24/7)
  // -> giam nhiet dang ke. Danh doi: moi lan chup cham hon ~1-2s do cam bien can
  // thoi gian khoi dong lai tu trang thai tat.
  bool camOk = initCamera();
  if (!camOk) {
    // Lan dau that bai -> co the do chu trinh deinit/init truoc do chua "sach" hoan
    // toan (I2C/SCCB bus chua on dinh). Deinit tuong minh + nghi lau hon roi thu lai
    // 1 lan nua truoc khi bao that bai han.
    Serial.println("[!!] Camera init FAIL lan 1 -> thu lai...");
    deinitCamera();
    delay(500);
    camOk = initCamera();
  }
  if (!camOk) {
    Serial.println("[!!] Camera init FAIL sau 2 lan thu");
    return false;
  }
  delay(300); // Cho cảm biến ổn định nguồn sau khi vừa bật lại

  warmupCamera(8); // Hâm nóng AWB/AE ngay trước khi chụp thật — ánh sáng có thể đã đổi
                    // kể từ lần chụp trước, đặc biệt với chụp thủ công bất kỳ lúc nào.

  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("[!!] Chup FAIL");
    deinitCamera();
    return false;
  }

  Serial.printf("[OK] %zu bytes (%dx%d)\n", fb->len, fb->width, fb->height);

  bool sent = false;
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[WiFi] RSSI: %d dBm\n", WiFi.RSSI());

    HTTPClient http;
    // Server (app.py) đọc thẳng request.get_data() như JPEG thô,
    // KHÔNG parse multipart -> phải gửi raw bytes, không bọc form-data.
    http.begin(String(SERVER_URL) + "/api/upload-image");
    // Render (goi free tier) tu ngu sau ~15 phut khong co traffic — danh thuc lai co the
    // mat 10-50s cho request dau tien. Neu khong dat setConnectTimeout(), thu vien mac
    // dinh chi cho 5000ms de bat tay ket noi -> luon bao loi -1 dung luc server dang
    // "thuc day", du server hoan toan binh thuong sau khi tinh tao.
    http.setConnectTimeout(45000);
    http.setTimeout(30000);
    http.addHeader("Content-Type", "image/jpeg");

    unsigned long t0 = millis();
    int code = http.POST(fb->buf, fb->len);
    unsigned long elapsedMs = millis() - t0;

    Serial.printf("[Server] HTTP %d (%lu ms)\n", code, elapsedMs);
    sent = (code == 200);

    if (sent) {
      float speedKBps = (fb->len / 1024.0f) / (elapsedMs / 1000.0f);
      Serial.printf("[NetSpeed] Upload: %.1f KB/s (%u KB / %lu ms)\n",
                     speedKBps, (unsigned)(fb->len / 1024), elapsedMs);
    }
    http.end();
  } else {
    Serial.println("[!!] WiFi mat ket noi, khong gui duoc anh");
  }

  esp_camera_fb_return(fb);
  deinitCamera(); // Tắt nguồn camera ngay sau khi chụp xong, không giữ bật chờ chụp tiếp theo
  return sent;
}

// ============================================================
//  KIỂM TRA YÊU CẦU CHỤP THỦ CÔNG TỪ DASHBOARD
//  GET  /api/manual-capture         -> {"manual_capture": true/false}
//  POST /api/manual-capture/clear   -> xóa cờ sau khi đã xử lý xong
// ============================================================
bool checkManualCaptureFlag() {
  HTTPClient http;
  http.begin(String(SERVER_URL) + "/api/manual-capture");
  // Tang tu 8s len 15s — du khong dai bang route upload anh (vi cho nay goi lap
  // moi 10s, khong nen block qua lau), nhung van du de bat duoc luc server vua
  // thuc day tu trang thai ngu (thuong nhanh hon lan dau tien goi upload-image).
  http.setConnectTimeout(15000);
  http.setTimeout(15000);
  int code = http.GET();
  bool requested = false;
  if (code == 200) {
    String payload = http.getString();
    requested = payload.indexOf("\"manual_capture\":true") >= 0 ||
                payload.indexOf("\"manual_capture\": true") >= 0;
  }
  http.end();
  return requested;
}

void clearManualCaptureFlag(bool success) {
  HTTPClient http;
  http.begin(String(SERVER_URL) + "/api/manual-capture/clear");
  http.setConnectTimeout(15000);
  http.setTimeout(15000);
  http.addHeader("Content-Type", "application/json");
  String body = String("{\"success\":") + (success ? "true" : "false") + "}";
  http.POST(body);
  http.end();
}

// ============================================================
//  SETUP — chỉ chạy 1 LẦN lúc board khởi động (cắm điện / RST)
// ============================================================
void setup() {
  Serial.begin(115200);
  Serial.println("\n=== ESP32-CAM KHOI DONG (che do LUON THUC) ===");
  pinMode(FLASH_PIN, OUTPUT);
  digitalWrite(FLASH_PIN, LOW);

  // Cho nguon on dinh truoc khi khoi tao WiFi/Camera lucs COLD BOOT — dien ap
  // tu buck converter tang dan tu 0V co the chua on dinh hoan toan trong vai
  // tram ms dau, gay loi khoi tao (brownout).
  delay(800);

  if (!connectWiFi()) {
    Serial.println("[!!] WiFi that bai lucs khoi dong -> se tu dong thu lai trong loop()");
  }

  bool ntpOk = false;
  for (int attempt = 1; attempt <= 3 && !ntpOk; attempt++) {
    if (attempt > 1) { Serial.printf("[..] NTP thu lai lan %d/3...\n", attempt); delay(3000); }
    ntpOk = syncTime();
  }
  if (ntpOk) {
    ntpSynced = true;
  } else {
    Serial.println("[!!] NTP that bai -> se tu dong dong bo lai trong loop()");
  }

  // Test camera 1 lần lúc khởi động để phát hiện sớm nếu phần cứng lỗi, rồi TẮT NGAY
  // (không giữ bật thường trực) — mỗi lần chụp thật sự (lịch/thủ công) sẽ tự bật lại.
  if (!initCamera()) {
    Serial.println("[!!] Camera FAIL — kiem tra lai phan cung!");
  } else {
    deinitCamera();
  }

  Serial.println("[OK] San sang. Cho lich chup hoac yeu cau thu cong...");

  // Giam xung nhip CPU tu mac dinh 240MHz xuong 80MHz — giam nhiet dang ke, vi phan lon
  // thoi gian ESP32 chi dung cho trong loop(), khong can toc do toi da. WiFi van hoat dong
  // binh thuong o 80MHz (muc toi thieu ESP32 ho tro cho WiFi). Chup anh/JPEG encode se
  // cham hon 1 chut nhung khong dang ke voi tan suat chup thua (vai lan/ngay).
  setCpuFrequencyMhz(80);
  Serial.printf("[CPU] Giam xung nhip xuong %d MHz de giam nhiet\n", getCpuFrequencyMhz());
}

// ============================================================
//  LOOP — chạy liên tục, không Deep Sleep
// ============================================================
unsigned long lastWifiCheckMs     = 0;
unsigned long wifiRetryWaitMs     = WIFI_CHECK_INTERVAL; // Thời gian chờ hiện tại — tăng dần khi thất bại liên tiếp
unsigned long lastManualPollMs    = 0;
unsigned long lastScheduleCheckMs = 0;
unsigned long lastNtpSyncMs       = 0;
unsigned long lastRetryMs         = 0;
unsigned long lastLowRamCheckMs   = 0;
bool pendingScheduledRetry        = false;

void loop() {
  unsigned long nowMs = millis();

  // ---- 1) Kiểm tra & tự kết nối lại WiFi nếu mất (VD: tắt hotspot để sạc điện thoại) ----
  if (nowMs - lastWifiCheckMs >= wifiRetryWaitMs) {
    lastWifiCheckMs = nowMs;
    if (WiFi.status() != WL_CONNECTED) {
      Serial.printf("[WiFi] Mat ket noi -> dang thu ket noi lai (se cho %lus truoc lan sau neu that bai)...\n",
                     wifiRetryWaitMs / 1000);
      if (connectWiFi()) {
        wifiRetryWaitMs = WIFI_CHECK_INTERVAL; // Kết nối lại OK -> quay về nhịp kiểm tra bình thường 5s
      } else {
        // That bai -> giãn thời gian chờ ra gấp đôi (5s -> 10s -> 20s ... toi da 5 phut).
        // Tranh spam thu ket noi lien tuc suot dem khi hotspot tat de sac dien thoai.
        wifiRetryWaitMs = min(wifiRetryWaitMs * 2, WIFI_RETRY_BACKOFF_MAX);
      }
    } else {
      wifiRetryWaitMs = WIFI_CHECK_INTERVAL;
    }
  }

  if (WiFi.status() == WL_CONNECTED) {
    // ---- 2) Đồng bộ lại NTP định kỳ (chống trôi giờ khi thức liên tục nhiều ngày) ----
    // Nếu CHƯA TỪNG đồng bộ thành công (VD: restart lúc hotspot đang tắt, setup() bị fail
    // NTP) -> thử ngay khi WiFi vừa có lại, không chờ đủ NTP_RESYNC_INTERVAL (1 tiếng).
    if (!ntpSynced || nowMs - lastNtpSyncMs >= NTP_RESYNC_INTERVAL) {
      lastNtpSyncMs = nowMs;
      if (syncTime()) ntpSynced = true;
    }

    // ---- 3) Hỏi server có yêu cầu chụp thủ công không ----
    if (nowMs - lastManualPollMs >= MANUAL_POLL_INTERVAL) {
      lastManualPollMs = nowMs;
      if (checkManualCaptureFlag()) {
        Serial.println("[Manual] Co yeu cau chup thu cong -> chup ngay");
        bool manualOk = captureAndSend();
        Serial.println(manualOk ? "[OK] Chup thu cong thanh cong!" : "[!!] Chup thu cong that bai");
        clearManualCaptureFlag(manualOk); // Báo đúng kết quả thực tế cho dashboard
      }
    }

    // ---- 4) Kiểm tra lịch chụp tự động (7h/12h/17h) ----
    bool timeToCheck = (nowMs - lastScheduleCheckMs >= SCHEDULE_CHECK_INTERVAL);
    bool timeToRetry = pendingScheduledRetry && (nowMs - lastRetryMs >= CAPTURE_RETRY_INTERVAL);

    if (timeToCheck || timeToRetry) {
      lastScheduleCheckMs = nowMs;
      time_t now = time(nullptr);
      struct tm* ti = localtime(&now);

      if (needCaptureThisHour(ti)) {
        Serial.printf("[Schedule] Den gio chup (%02d:00) -> chup ngay\n", ti->tm_hour);
        if (captureAndSend()) {
          lastCapturedHour = ti->tm_hour;
          pendingScheduledRetry = false;
          Serial.println("[OK] Chup theo lich thanh cong!");
        } else {
          // That bai -> danh dau can retry sau CAPTURE_RETRY_INTERVAL, khong danh dau
          // lastCapturedHour nen se tiep tuc thu cho toi khi thanh cong hoac sang gio moi.
          pendingScheduledRetry = true;
          lastRetryMs = nowMs;
          Serial.println("[!!] Chup theo lich that bai -> se thu lai sau 60s");
        }
      } else {
        pendingScheduledRetry = false;
      }
    }
  }

  // ---- 5) Kiểm tra RAM thấp nguy hiểm — chỉ tự restart khi thật sự cần ----
  // Đặt NGOÀI khối "if WiFi connected": vẫn cần kiểm tra kể cả lúc mất WiFi, vì rò rỉ
  // bộ nhớ có thể xảy ra ở bất kỳ đâu trong loop() (camera, HTTPClient...), không riêng
  // lúc có mạng. Chạy 1 tháng liên tục không nghỉ (không Deep Sleep) nên cần lớp bảo vệ
  // này để tránh ESP32 tự crash không kiểm soát giữa chừng khi bộ nhớ cạn dần.
  if (nowMs - lastLowRamCheckMs >= LOW_RAM_CHECK_INTERVAL) {
    lastLowRamCheckMs = nowMs;
    uint32_t freeHeap = ESP.getFreeHeap();
    if (freeHeap < LOW_RAM_THRESHOLD) {
      Serial.printf("[Restart] RAM trong xuong muc nguy hiem (%u byte < %u byte) -> tu khoi dong lai\n",
                     freeHeap, LOW_RAM_THRESHOLD);
      Serial.flush();
      delay(500);
      ESP.restart();
    }
  }

  delay(200); // Nhịp nghỉ nhỏ, tránh loop() chạy nóng máy vô ích
}
