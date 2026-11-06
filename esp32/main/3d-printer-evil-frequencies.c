#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "mqtt_client.h"
#include "driver/i2c_master.h"
#include "driver/gpio.h"

// ==== Configure these ====
#define WIFI_SSID        "wifi_ssid"
#define WIFI_PASS        "password"
#define MQTT_BROKER_URI  "mqtt://broker.hivemq.com:1883"
#define MQTT_TOPIC       "secret_topic"
// =========================

// Configure i2c
#define I2C_PORT        I2C_NUM_0
#define I2C_SDA_PIN     GPIO_NUM_18
#define I2C_SCL_PIN     GPIO_NUM_19
#define ADXL345_ADDR    0x53
#define CS_GPIO         GPIO_NUM_25
#define I2C_SPEED_HZ    400000

#define WIFI_MAX_RETRY 5
#define SAMPLE_PERIOD_MS 1

static const char *TAG = "MAIN";

static EventGroupHandle_t s_wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1

static int s_retry_num = 0;
static esp_mqtt_client_handle_t s_mqtt_client = NULL;

// Write register
static esp_err_t adxl_write_reg(i2c_master_dev_handle_t dev_handle, uint8_t reg, uint8_t val) {
    uint8_t buf[2] = {reg, val};
    return i2c_master_transmit(dev_handle, buf, 2, 100);
}

// Read register
static esp_err_t adxl_read_reg(i2c_master_dev_handle_t dev_handle, uint8_t reg, uint8_t *buf, size_t len) {
    esp_err_t ret = i2c_master_transmit(dev_handle, &reg, 1, 100);
    if (ret != ESP_OK) return ret;
    return i2c_master_receive(dev_handle, buf, len, 100);
}

// Initialize: enable measurement, ±2g
static esp_err_t adxl_init(i2c_master_dev_handle_t dev_handle) {
    esp_err_t ret = adxl_write_reg(dev_handle, 0x2D, 0x08); // Measurement mode
    if (ret != ESP_OK) return ret;
    ret = adxl_write_reg(dev_handle, 0x31, 0x00);           // ±2g
    if (ret != ESP_OK) return ret;
    
    // Set Output Data Rate to 800 Hz (0x0D)
    return adxl_write_reg(dev_handle, 0x2C, 0x0D); 
}

// Read acceleration, output in g
static esp_err_t adxl_read_accel(i2c_master_dev_handle_t dev_handle, float *x, float *y, float *z) {
    uint8_t data[6];
    esp_err_t ret = adxl_read_reg(dev_handle, 0x32, data, 6);
    if (ret != ESP_OK) return ret;

    int16_t raw_x = (int16_t)((data[1] << 8) | data[0]);
    int16_t raw_y = (int16_t)((data[3] << 8) | data[2]);
    int16_t raw_z = (int16_t)((data[5] << 8) | data[4]);

    // ±2g range, 3.9 mg/LSB
    *x = raw_x * 0.0039f;
    *y = raw_y * 0.0039f;
    *z = raw_z * 0.0039f;

    return ESP_OK;
}

// ---------- Wi-Fi ----------

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_retry_num < WIFI_MAX_RETRY) {
            esp_wifi_connect();
            s_retry_num++;
            ESP_LOGI(TAG, "Retrying Wi-Fi connect (%d/%d)...", s_retry_num, WIFI_MAX_RETRY);
        } else {
            xEventGroupSetBits(s_wifi_event_group, WIFI_FAIL_BIT);
            ESP_LOGE(TAG, "Wi-Fi connect failed");
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_num = 0;
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init_sta(void)
{
    s_wifi_event_group = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT,
                                                        ESP_EVENT_ANY_ID,
                                                        &wifi_event_handler,
                                                        NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT,
                                                        IP_EVENT_STA_GOT_IP,
                                                        &wifi_event_handler,
                                                        NULL, NULL));

    wifi_config_t wifi_config = {
        .sta = {
            .ssid = WIFI_SSID,
            .password = WIFI_PASS,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "Waiting for Wi-Fi connection...");
    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group,
                                           WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
                                           pdFALSE, pdFALSE, portMAX_DELAY);

    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "Wi-Fi connected");
    } else {
        ESP_LOGE(TAG, "Wi-Fi failed to connect");
    }
}

// ---------- MQTT ----------

static void mqtt_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    switch ((esp_mqtt_event_id_t)event_id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "MQTT connected");
        break;
    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "MQTT disconnected");
        break;
    case MQTT_EVENT_ERROR:
        ESP_LOGE(TAG, "MQTT error");
        break;
    default:
        break;
    }
}

static void mqtt_start(void)
{
    esp_mqtt_client_config_t cfg = {
        .broker.address.uri = MQTT_BROKER_URI,
    };
    s_mqtt_client = esp_mqtt_client_init(&cfg);
    esp_mqtt_client_register_event(s_mqtt_client, ESP_EVENT_ANY_ID,
                                   mqtt_event_handler, NULL);
    esp_mqtt_client_start(s_mqtt_client);
}

// ---------- app_main ----------

void app_main(void)
{
  // 0. Power-up CS for I2C mode
  gpio_set_direction(CS_GPIO, GPIO_MODE_OUTPUT);
  gpio_set_level(CS_GPIO, 1);
  vTaskDelay(pdMS_TO_TICKS(50));

  // 1. Initialize I2C bus
  i2c_master_bus_config_t bus_cfg = {
    .i2c_port = I2C_PORT,
    .sda_io_num = I2C_SDA_PIN,
    .scl_io_num = I2C_SCL_PIN,
    .clk_source = I2C_CLK_SRC_DEFAULT,
    .glitch_ignore_cnt = 7,
    .flags.enable_internal_pullup = false,
  };
  i2c_master_bus_handle_t bus_handle;
  ESP_ERROR_CHECK(i2c_new_master_bus(&bus_cfg, &bus_handle));
  ESP_LOGI(TAG, "I2C bus initialized");

  // 2. Add ADXL345 device handle
  i2c_device_config_t dev_cfg = {
      .dev_addr_length = I2C_ADDR_BIT_LEN_7,
      .device_address = ADXL345_ADDR,
      .scl_speed_hz = I2C_SPEED_HZ,
  };
  i2c_master_dev_handle_t dev_handle;
  ESP_ERROR_CHECK(i2c_master_bus_add_device(bus_handle, &dev_cfg, &dev_handle));

  // 3. Verify DEVID
  uint8_t dev_id = 0;
  uint8_t reg = 0x00;
  ESP_ERROR_CHECK(i2c_master_transmit(dev_handle, &reg, 1, 100));
  ESP_ERROR_CHECK(i2c_master_receive(dev_handle, &dev_id, 1, 100));
  if (dev_id == 0xE5) {
      ESP_LOGI(TAG, "ADXL345 verified! Device ID: 0x%02X", dev_id);
  } else {
      ESP_LOGE(TAG, "Unexpected Device ID: 0x%02X (Expected 0xE5)", dev_id);
  }

  // 4. Initialize sensor (measurement mode, ±2g)
  ESP_ERROR_CHECK(adxl_init(dev_handle));
  ESP_LOGI(TAG, "Sensor configured. Reading data...");

  // 5. NVS / Wi-Fi / MQTT
  esp_err_t nvs_ret = nvs_flash_init();
  if (nvs_ret == ESP_ERR_NVS_NO_FREE_PAGES || nvs_ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
      ESP_ERROR_CHECK(nvs_flash_erase());
      nvs_ret = nvs_flash_init();
  }
  ESP_ERROR_CHECK(nvs_ret);

  wifi_init_sta();
  mqtt_start();
  vTaskDelay(pdMS_TO_TICKS(2000));  // let MQTT handshake complete

  // 6. Publish loop
  float x, y, z;
  char payload[128];

  while (1) {
    if (adxl_read_accel(dev_handle, &x, &y, &z) == ESP_OK) {
      int n = snprintf(payload, sizeof(payload),
                       "%.2f;%.2f;%.2f", x, y, z);

      if (n > 0 && n < sizeof(payload)) {
        int msg_id = esp_mqtt_client_publish(s_mqtt_client, MQTT_TOPIC, payload, 0, 1, 0);
        if (msg_id != -1) {
          ESP_LOGI(TAG, "Published: %s", payload);
        } else {
          ESP_LOGW(TAG, "Publish failed (not connected?)");
        }
      } else {
        ESP_LOGW(TAG, "Payload truncated!");
      }
    } else {
      ESP_LOGE(TAG, "Failed to read sensor");
    }

    vTaskDelay(pdMS_TO_TICKS(SAMPLE_PERIOD_MS));
  }
}
