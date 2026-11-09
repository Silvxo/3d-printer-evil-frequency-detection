#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
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
#define MQTT_TOPIC       "mqtt_topic"
// =========================

#define I2C_PORT        I2C_NUM_0
#define I2C_SDA_PIN     GPIO_NUM_18
#define I2C_SCL_PIN     GPIO_NUM_19
#define ADXL345_ADDR    0x53
#define CS_GPIO         GPIO_NUM_25
#define I2C_SPEED_HZ    400000 // 400kHz for 800Hz sampling

#define WIFI_MAX_RETRY 5

// ==== Burst Configuration ====
#define SAMPLES_PER_SECOND 800
#define SAMPLES_PER_PACKET 400   // Send a packet every 0.5 seconds
#define CSV_MAX_LEN (SAMPLES_PER_PACKET * 25) // ~10KB buffer

static const char *TAG = "MAIN";

static EventGroupHandle_t s_wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1

static int s_retry_num = 0;
static esp_mqtt_client_handle_t s_mqtt_client = NULL;

// Buffer and synchronization
typedef struct {
    float x, y, z;
} accel_sample_t;

static accel_sample_t sample_buffer[SAMPLES_PER_PACKET];
static int sample_count = 0;
static SemaphoreHandle_t buffer_mutex;

// ---------- ADXL345 I2C Helpers ----------

static esp_err_t adxl_write_reg(i2c_master_dev_handle_t dev_handle, uint8_t reg, uint8_t val) {
    uint8_t buf[2] = {reg, val};
    return i2c_master_transmit(dev_handle, buf, 2, 100);
}

static esp_err_t adxl_read_reg(i2c_master_dev_handle_t dev_handle, uint8_t reg, uint8_t *buf, size_t len) {
    esp_err_t ret = i2c_master_transmit(dev_handle, &reg, 1, 100);
    if (ret != ESP_OK) return ret;
    return i2c_master_receive(dev_handle, buf, len, 100);
}

static esp_err_t adxl_init(i2c_master_dev_handle_t dev_handle) {
    esp_err_t ret = adxl_write_reg(dev_handle, 0x2D, 0x08); // Measurement mode
    if (ret != ESP_OK) return ret;
    ret = adxl_write_reg(dev_handle, 0x31, 0x00);           // ±2g
    if (ret != ESP_OK) return ret;
    return adxl_write_reg(dev_handle, 0x2C, 0x0D);          // ODR = 800Hz
}

static esp_err_t adxl_read_accel(i2c_master_dev_handle_t dev_handle, float *x, float *y, float *z) {
    uint8_t data[6];
    esp_err_t ret = adxl_read_reg(dev_handle, 0x32, data, 6);
    if (ret != ESP_OK) return ret;
    
    int16_t raw_x = (int16_t)((data[1] << 8) | data[0]);
    int16_t raw_y = (int16_t)((data[3] << 8) | data[2]);
    int16_t raw_z = (int16_t)((data[5] << 8) | data[4]);
    
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

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, NULL));

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
    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT, pdFALSE, pdFALSE, portMAX_DELAY);
    
    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "Wi-Fi connected");
    } else {
        ESP_LOGE(TAG, "Wi-Fi failed to connect");
    }
}

// ---------- MQTT ----------

static void mqtt_event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data) {
    switch ((esp_mqtt_event_id_t)event_id) {
    case MQTT_EVENT_CONNECTED: ESP_LOGI(TAG, "MQTT connected"); break;
    case MQTT_EVENT_DISCONNECTED: ESP_LOGW(TAG, "MQTT disconnected"); break;
    case MQTT_EVENT_ERROR: ESP_LOGE(TAG, "MQTT error"); break;
    default: break;
    }
}

static void mqtt_start(void) {
    esp_mqtt_client_config_t cfg = { .broker.address.uri = MQTT_BROKER_URI };
    s_mqtt_client = esp_mqtt_client_init(&cfg);
    esp_mqtt_client_register_event(s_mqtt_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    esp_mqtt_client_start(s_mqtt_client);
}

// ---------- FreeRTOS Tasks ----------

// Task 1: Runs at 800Hz, reads sensor, stores in buffer
void sensor_sampling_task(void *arg) {
    i2c_master_dev_handle_t dev_handle = (i2c_master_dev_handle_t)arg;
    float x, y, z;
    
    // Calculate exact ticks for 800Hz (1.25ms)
    const TickType_t sample_interval = pdMS_TO_TICKS(1000) / SAMPLES_PER_SECOND;
    TickType_t last_wake_time = xTaskGetTickCount();

    while (1) {
        if (adxl_read_accel(dev_handle, &x, &y, &z) == ESP_OK) {
            xSemaphoreTake(buffer_mutex, portMAX_DELAY);
            if (sample_count < SAMPLES_PER_PACKET) {
                sample_buffer[sample_count].x = x;
                sample_buffer[sample_count].y = y;
                sample_buffer[sample_count].z = z;
                sample_count++;
            }
            xSemaphoreGive(buffer_mutex);
        }
        
        // Use vTaskDelayUntil for precise timing
        vTaskDelayUntil(&last_wake_time, sample_interval);
    }
}

// Task 2: Runs every 500ms, converts buffer to CSV, publishes via MQTT
void publisher_task(void *arg) {
    char *csv_payload = malloc(CSV_MAX_LEN); 
    if (csv_payload == NULL) {
        ESP_LOGE(TAG, "Failed to allocate CSV buffer");
        vTaskDelete(NULL);
    }

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(500)); // Wait for buffer to fill (400 samples)

        xSemaphoreTake(buffer_mutex, portMAX_DELAY);
        
        if (sample_count > 0) {
            int len = 0;
            
            // Convert structs to semicolon-separated CSV string
            for (int i = 0; i < sample_count; i++) {
                len += snprintf(csv_payload + len, CSV_MAX_LEN - len,
                                "%.2f;%.2f;%.2f\n",
                                sample_buffer[i].x, 
                                sample_buffer[i].y, 
                                sample_buffer[i].z);
            }

            int samples_to_send = sample_count;
            sample_count = 0; // Reset buffer count
            xSemaphoreGive(buffer_mutex);

            // Publish
            int msg_id = esp_mqtt_client_publish(s_mqtt_client, MQTT_TOPIC, csv_payload, 0, 1, 0);
            
            if (msg_id != -1) {
                ESP_LOGI(TAG, "Published %d CSV lines (%d bytes)", samples_to_send, len);
            } else {
                ESP_LOGW(TAG, "Publish failed (not connected?)");
            }
        } else {
            xSemaphoreGive(buffer_mutex);
        }
    }
}

// ---------- Main ----------

void app_main(void)
{
  // 1. Initialize I2C and ADXL345
  gpio_set_direction(CS_GPIO, GPIO_MODE_OUTPUT);
  gpio_set_level(CS_GPIO, 1);
  vTaskDelay(pdMS_TO_TICKS(50));

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

  i2c_device_config_t dev_cfg = {
      .dev_addr_length = I2C_ADDR_BIT_LEN_7,
      .device_address = ADXL345_ADDR,
      .scl_speed_hz = I2C_SPEED_HZ,
  };
  i2c_master_dev_handle_t dev_handle;
  ESP_ERROR_CHECK(i2c_master_bus_add_device(bus_handle, &dev_cfg, &dev_handle));

  ESP_ERROR_CHECK(adxl_init(dev_handle));
  ESP_LOGI(TAG, "Sensor initialized at 800Hz");

  // 2. Network Setup
  esp_err_t nvs_ret = nvs_flash_init();
  if (nvs_ret == ESP_ERR_NVS_NO_FREE_PAGES || nvs_ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
      ESP_ERROR_CHECK(nvs_flash_erase());
      nvs_ret = nvs_flash_init();
  }
  ESP_ERROR_CHECK(nvs_ret);
  wifi_init_sta();
  mqtt_start();
  vTaskDelay(pdMS_TO_TICKS(2000)); // Wait for MQTT handshake

  // 3. Create Mutex and Start Tasks
  buffer_mutex = xSemaphoreCreateMutex();
  
  // Pin sampling to Core 0 and publishing to Core 1 to avoid network interference
  xTaskCreatePinnedToCore(sensor_sampling_task, "sampling", 4096, (void*)dev_handle, 10, NULL, 0);
  xTaskCreatePinnedToCore(publisher_task, "publisher", 8192, NULL, 5, NULL, 1);

  // app_main can now exit, FreeRTOS tasks will take over
  vTaskDelete(NULL);
}
