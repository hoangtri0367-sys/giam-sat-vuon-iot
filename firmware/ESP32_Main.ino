/*
 * ESP32_Main.ino — Hệ thống IoT Giám sát Cải Ngọt
 * =================================================
 * Chu kỳ:
 *   - Kiểm tra cảm biến (đất/quạt)      : 5 giây
 *   - Bơm dạng XUNG: bơm 5s -> dừng -> đọc lại đất -> nếu chưa đủ ẩm thì bơm tiếp 5s...
 *   - Đọc cảm biến đầy đủ + gửi live lên web : 1 phút
 *   - Lưu DB (biểu đồ + Excel)          : 30 phút
 *   - Polling lệnh thủ công             : 30 giây
 * KHÔNG dùng Blynk
 *
 * LOGIC BƠM MỚI (theo yêu cầu):
 *   1. Đất khô (< soil_min) -> bơm chạy 5 giây
 *   2. Dừng bơm -> nghỉ 3 giây (để nước ngấm, cảm biến đọc chính xác hơn thay vì đọc lúc đang xối nước)
 *   3. Đọc lại độ ẩm đất:
 *        - Nếu đã đủ ẩm (>= soil_min)  -> dừng hẳn, quay về trạng thái rảnh (IDLE)
 *        - Nếu vẫn chưa đủ             -> bơm thêm 1 đợt 5 giây nữa, lặp lại bước 2-3
 *   4. An toàn: nếu tổng thời gian bơm trong 1 đợt tưới vượt PUMP_MAX_TOTAL_ON (mặc định 60s)
 *      mà đất vẫn chưa đủ ẩm -> tự dừng và cảnh báo (nghi cảm biến lỗi/đường ống tắc/bơm hỏng),
 *      tránh trường hợp bơm chạy vô hạn gây úng hoặc cháy bơm.
 *   Toàn bộ được viết theo kiểu "state machine" không dùng delay() chặn luồng,
 *   nên WiFi/HTTP/Telegram vẫn hoạt động bình thường trong lúc chờ giữa các đợt bơm.
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <DHT.h>
#include <Wire.h>
#include <BH1750.h>

// ════════════════════════════════════════════════
// CẤU HÌNH — THAY ĐỔI Ở ĐÂY
// ════════════════════════════════════════════════
const char* WIFI_SSID     = "TEN_WIFI_CUA_BAN";
const char* WIFI_PASSWORD = "MAT_KHAU_WIFI";
const char* SERVER_URL    = "https://ten-app-cua-ban.onrender.com"; // TODO: đổi sang domain Render thật của bạn

// 0=Nảy mầm  1=Cây con  2=Sinh trưởng  3=Thu hoạch
// Giá trị mặc định lúc mất mạng / server chưa phản hồi lần nào.
// Sau khi có WiFi, giá trị này sẽ được cập nhật từ lựa chọn trên dashboard web
// thông qua pollDeviceState() / postSensor() — xem hàm setGrowthStage() bên dưới.
int GROWTH_STAGE = 2;
const char* STAGE_NAMES[] = {"Nay_Mam", "Cay_Con", "Sinh_Truong", "Thu_Hoach"};
const int GROWTH_STAGE_COUNT = 4;

// Đổi giai đoạn 1 cách an toàn (chặn giá trị lỗi từ server, tránh vượt mảng THRESHOLDS[])
void setGrowthStage(int newStage) {
  if (newStage < 0 || newStage >= GROWTH_STAGE_COUNT) return;
  if (newStage != GROWTH_STAGE) {
    GROWTH_STAGE = newStage;
    Serial.printf("[Stage] Đổi giai đoạn -> %s\n", STAGE_NAMES[GROWTH_STAGE]);
  }
}

// ════════════════════════════════════════════════
// CHÂN KẾT NỐI
// ════════════════════════════════════════════════
#define DHT_PIN    4
#define DHT_TYPE   DHT22
#define SOIL_PIN   34
#define RELAY_PUMP 26    // Active LOW
#define RELAY_FAN  27    // Active LOW
#define SDA_PIN    21
#define SCL_PIN    22

// ════════════════════════════════════════════════
// NGƯỠNG THEO GIAI ĐOẠN
// ════════════════════════════════════════════════
struct Threshold { float soil_min, soil_max, temp_max; };
Threshold THRESHOLDS[] = {
  {60, 80, 30},   // Nảy mầm
  {55, 75, 32},   // Cây con
  {45, 70, 35},   // Sinh trưởng
  {40, 65, 35},   // Thu hoạch
};

// ════════════════════════════════════════════════
// CHU KỲ
// ════════════════════════════════════════════════
const unsigned long SENSOR_CHECK_INTERVAL = 5UL  * 1000;      // 5 giây  — kiểm tra đất (lúc rảnh) + quạt
const unsigned long READ_LIVE_INTERVAL    = 1UL  * 60 * 1000; // 1 phút  — gửi dashboard
const unsigned long SAVE_DB_INTERVAL      = 30UL * 60 * 1000; // 30 phút — lưu DB
const unsigned long POLL_INTERVAL         = 30UL * 1000;      // 30 giây — nhận lệnh thủ công
const unsigned long WIFI_CHECK_INTERVAL   = 10UL * 1000;      // 10 giây — kiểm tra & tự reconnect WiFi nếu rớt mạng

// ---- Thông số bơm dạng xung ----
const unsigned long PUMP_PULSE_ON  = 5UL  * 1000;  // mỗi lần bơm chạy 5 giây
const unsigned long PUMP_SETTLE    = 3UL  * 1000;  // nghỉ 3 giây trước khi đọc lại đất (chờ nước ngấm)
const unsigned long PUMP_MAX_TOTAL_ON = 60UL * 1000; // an toàn: tổng thời gian bơm tối đa/1 đợt tưới

// ════════════════════════════════════════════════
// BIẾN TOÀN CỤC
// ════════════════════════════════════════════════
DHT    dht(DHT_PIN, DHT_TYPE);
BH1750 lightMeter;

unsigned long lastSensorCheck = 0;
unsigned long lastReadLive    = 0;
unsigned long lastSaveDB      = 0;
unsigned long lastPoll        = 0;
unsigned long lastWifiCheck   = 0;

bool  pumpOn = false;
bool  fanOn  = false;
float lastTemp  = 0, lastSoil = 0, lastLight = 0, lastHumidity = 0;

// ---- State machine cho bơm ----
enum PumpState { PUMP_IDLE, PUMP_RUNNING, PUMP_SETTLING };
PumpState pumpState = PUMP_IDLE;
unsigned long pumpStateStart  = 0;   // thời điểm bắt đầu trạng thái hiện tại
unsigned long pumpTotalOnTime = 0;   // tổng thời gian đã bơm trong đợt tưới hiện tại

// ════════════════════════════════════════════════
// SETUP
// ════════════════════════════════════════════════
void setup() {
  Serial.begin(115200);
  delay(500);

  pinMode(RELAY_PUMP, OUTPUT); digitalWrite(RELAY_PUMP, HIGH);
  pinMode(RELAY_FAN,  OUTPUT); digitalWrite(RELAY_FAN,  HIGH);

  dht.begin();
  Wire.begin(SDA_PIN, SCL_PIN);
  lightMeter.begin();

  Serial.printf("Kết nối WiFi: %s\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  int retry = 0;
  while (WiFi.status() != WL_CONNECTED && retry < 20) {
    delay(500); Serial.print("."); retry++;
  }
  Serial.println(WiFi.status() == WL_CONNECTED
    ? "\nWiFi OK!"
    : "\nWiFi thất bại — offline mode");

  // Chạy ngay lần đầu
  lastSoil = readSoil();
  float t0 = dht.readTemperature();
  if (!isnan(t0) && t0 > 0) lastTemp = t0;
  float h0 = dht.readHumidity();
  if (!isnan(h0)) lastHumidity = h0;
  autoControlFan(lastTemp);
  readAndSendLive();
  saveToDatabase();
}

// ════════════════════════════════════════════════
// LOOP
// ════════════════════════════════════════════════
void loop() {
  unsigned long now = millis();

  // (0) Mỗi 10 giây: kiểm tra WiFi, tự reconnect nếu bị rớt (không chặn luồng)
  if (now - lastWifiCheck >= WIFI_CHECK_INTERVAL) {
    lastWifiCheck = now;
    checkWiFi();
  }

  // (1) Mỗi 5 giây: kiểm tra quạt, và nếu bơm đang RẢNH thì kiểm tra đất để quyết định có tưới không
  if (now - lastSensorCheck >= SENSOR_CHECK_INTERVAL) {
    lastSensorCheck = now;

    float temp = dht.readTemperature();
    if (!isnan(temp) && temp > 0) lastTemp = temp;
    autoControlFan(lastTemp);

    if (pumpState == PUMP_IDLE) {
      float soil = readSoil();
      lastSoil = soil;
      if (soil < THRESHOLDS[GROWTH_STAGE].soil_min) {
        pumpTotalOnTime = 0;
        startPumpPulse(now);
      }
    }
  }

  // (2) Cập nhật state machine của bơm mỗi vòng lặp (bắt đúng thời điểm 5s/3s kết thúc)
  updatePumpStateMachine(now);

  // (3) Gửi dữ liệu lên dashboard — mỗi 1 phút
  if (now - lastReadLive >= READ_LIVE_INTERVAL) {
    lastReadLive = now;
    readAndSendLive();
  }

  // (4) Lưu DB — mỗi 30 phút
  if (now - lastSaveDB >= SAVE_DB_INTERVAL) {
    lastSaveDB = now;
    saveToDatabase();
  }

  // (5) Nhận lệnh thủ công từ server — mỗi 30 giây
  if (now - lastPoll >= POLL_INTERVAL) {
    lastPoll = now;
    pollDeviceState();
  }

  delay(100);
}

// ════════════════════════════════════════════════
// ĐỌC CẢM BIẾN ĐẦY ĐỦ + GỬI LIVE (mỗi 1 phút)
// ════════════════════════════════════════════════
void readAndSendLive() {
  float temp     = dht.readTemperature();
  float humidity = dht.readHumidity();
  float light    = lightMeter.readLightLevel();

  if (!isnan(temp) && temp > 0)  lastTemp     = temp;
  if (!isnan(humidity))          lastHumidity = humidity;
  if (light >= 0)                lastLight    = light;
  // lastSoil được cập nhật liên tục bởi state machine bơm / kiểm tra 5s, không đọc lại ở đây
  // để tránh đọc trúng lúc bơm đang xối nước gây sai số hiển thị.

  Serial.printf("[1min] T=%.1f°C | Am KK=%.1f%% | Đất=%.1f%% | Sáng=%.0flux\n",
                lastTemp, lastHumidity, lastSoil, lastLight);

  if (WiFi.status() == WL_CONNECTED) {
    postSensor("/api/sensor/live", false);
  }
}

// ════════════════════════════════════════════════
// LƯU DB MỖI 30 PHÚT (biểu đồ + Excel)
// ════════════════════════════════════════════════
void saveToDatabase() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[DB] Mất WiFi, bỏ qua");
    return;
  }
  Serial.println("[DB] Lưu vào database...");
  postSensor("/api/sensor", true);
}

// ════════════════════════════════════════════════
// HÀM GỬI SENSOR (dùng chung cho live và DB)
// ════════════════════════════════════════════════
void postSensor(const char* endpoint, bool saveDB) {
  StaticJsonDocument<288> doc;
  doc["temperature"]   = round(lastTemp  * 10) / 10.0;
  doc["humidity"]      = round(lastHumidity * 10) / 10.0;
  doc["soil_moisture"] = round(lastSoil  * 10) / 10.0;
  doc["light_lux"]     = round(lastLight);
  doc["growth_stage"]  = STAGE_NAMES[GROWTH_STAGE];
  doc["pump_on"]       = pumpOn;
  doc["fan_on"]        = fanOn;

  String body;
  serializeJson(doc, body);

  HTTPClient http;
  http.begin(String(SERVER_URL) + endpoint);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(8000);

  int code = http.POST(body);
  if (code == 200) {
    // Nhận lệnh thủ công từ server nếu có
    StaticJsonDocument<160> res;
    if (!deserializeJson(res, http.getString())) {
      String pMode = res["pump_mode"] | "auto";
      String fMode = res["fan_mode"]  | "auto";
      // Lệnh thủ công sẽ hủy state machine tự động của bơm
      if (pMode == "manual") {
        pumpState = PUMP_IDLE;
        setRelay("pump", res["pump_on"] | false);
      }
      if (fMode == "manual") setRelay("fan", res["fan_on"] | false);

      // Nhận giai đoạn sinh trưởng do người dùng chọn trên dashboard web
      if (res.containsKey("growth_stage")) {
        setGrowthStage(res["growth_stage"].as<int>());
      }
    }
  } else {
    Serial.printf("[POST %s] HTTP %d\n", endpoint, code);
  }
  http.end();
}

// ════════════════════════════════════════════════
// KIỂM TRA & TỰ RECONNECT WiFi (không chặn luồng — gọi mỗi 10 giây)
// ════════════════════════════════════════════════
void checkWiFi() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WiFi] Mất kết nối -> thử reconnect...");
    WiFi.disconnect();
    WiFi.reconnect();
    // Không dùng delay()/while() chờ ở đây để tránh chặn state machine bơm/quạt.
    // Lần checkWiFi() kế tiếp (10s sau) sẽ kiểm tra lại; nếu vẫn fail sẽ thử reconnect() tiếp.
  }
}

// ════════════════════════════════════════════════
// ĐIỀU KHIỂN QUẠT TỰ ĐỘNG — gọi mỗi 5 giây
// ════════════════════════════════════════════════
void autoControlFan(float temp) {
  Threshold& th = THRESHOLDS[GROWTH_STAGE];

  if (temp > th.temp_max && !fanOn) {
    setRelay("fan", true);
    Serial.printf("[Auto] Quạt BẬT — T %.1f°C\n", temp);
  } else if (temp <= th.temp_max && fanOn) {
    setRelay("fan", false);
    Serial.printf("[Auto] Quạt TẮT — T %.1f°C\n", temp);
  }
}

// ════════════════════════════════════════════════
// BẮT ĐẦU 1 ĐỢT BƠM 5 GIÂY
// ════════════════════════════════════════════════
void startPumpPulse(unsigned long now) {
  setRelay("pump", true);
  pumpState = PUMP_RUNNING;
  pumpStateStart = now;
  Serial.println("[Pump] Bắt đầu bơm (xung 5s)...");
}

// ════════════════════════════════════════════════
// STATE MACHINE BƠM DẠNG XUNG
// RUNNING (bơm 5s) -> SETTLING (nghỉ 3s, đọc lại đất) -> IDLE hoặc RUNNING tiếp
// ════════════════════════════════════════════════
void updatePumpStateMachine(unsigned long now) {
  Threshold& th = THRESHOLDS[GROWTH_STAGE];

  switch (pumpState) {

    case PUMP_IDLE:
      // Không làm gì — việc khởi động bơm do khối kiểm tra 5s trong loop() đảm nhiệm
      break;

    case PUMP_RUNNING:
      if (now - pumpStateStart >= PUMP_PULSE_ON) {
        setRelay("pump", false);
        pumpTotalOnTime += PUMP_PULSE_ON;
        pumpState = PUMP_SETTLING;
        pumpStateStart = now;
        Serial.println("[Pump] Hết 5s -> tạm dừng, chờ ngấm nước để đọc lại đất...");
      }
      break;

    case PUMP_SETTLING:
      if (now - pumpStateStart >= PUMP_SETTLE) {
        float soil = readSoil();
        lastSoil = soil;
        Serial.printf("[Pump] Đọc lại sau nghỉ: đất = %.1f%%\n", soil);

        if (soil >= th.soil_min) {
          Serial.println("[Pump] Đủ ẩm -> dừng tưới, về trạng thái rảnh");
          pumpState = PUMP_IDLE;
        } else if (pumpTotalOnTime >= PUMP_MAX_TOTAL_ON) {
          Serial.printf("[SAFETY] Đã bơm tổng %lus mà đất vẫn chưa đủ ẩm -> DỪNG. "
                        "Kiểm tra cảm biến đất / đường ống / bơm!\n", pumpTotalOnTime / 1000);
          pumpState = PUMP_IDLE;
        } else {
          // Vẫn chưa đủ ẩm và chưa vượt an toàn -> bơm thêm 1 đợt 5s nữa
          startPumpPulse(now);
        }
      }
      break;
  }
}

// ════════════════════════════════════════════════
// POLLING LỆNH THỦ CÔNG
// ════════════════════════════════════════════════
void pollDeviceState() {
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;
  http.begin(String(SERVER_URL) + "/api/device-state");
  http.setTimeout(6000);

  int code = http.GET();
  if (code == 200) {
    StaticJsonDocument<220> doc;
    if (!deserializeJson(doc, http.getString())) {
      String pMode = doc["pump_mode"] | "auto";
      String fMode = doc["fan_mode"]  | "auto";
      if (pMode == "manual") {
        pumpState = PUMP_IDLE;
        setRelay("pump", doc["pump_on"] | false);
      }
      if (fMode == "manual") setRelay("fan", doc["fan_on"] | false);

      // Nhận giai đoạn sinh trưởng do người dùng chọn trên dashboard web
      if (doc.containsKey("growth_stage")) {
        setGrowthStage(doc["growth_stage"].as<int>());
      }
    }
  }
  http.end();
}

// ════════════════════════════════════════════════
// SET RELAY
// ════════════════════════════════════════════════
void setRelay(const char* device, bool on) {
  if (strcmp(device, "pump") == 0 && pumpOn != on) {
    pumpOn = on;
    digitalWrite(RELAY_PUMP, on ? LOW : HIGH);
    Serial.printf("[Relay] Bơm → %s\n", on ? "ON" : "OFF");
  }
  if (strcmp(device, "fan") == 0 && fanOn != on) {
    fanOn = on;
    digitalWrite(RELAY_FAN, on ? LOW : HIGH);
    Serial.printf("[Relay] Quạt → %s\n", on ? "ON" : "OFF");
  }
}

// ════════════════════════════════════════════════
// ĐỌC ĐỘ ẨM ĐẤT
// ════════════════════════════════════════════════
float readSoil() {
  int raw = analogRead(SOIL_PIN);
  float pct = map(raw, 4095, 1500, 0, 100);
  return constrain(pct, 0, 100);
}
