#include <stdio.h>
#include <stdint.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/i2c.h"
#include "esp_log.h"
#include "esp_err.h"

static const char *TAG = "ADXL345";

// I2C Configuration for standard ESP32
#define I2C_MASTER_SCL_IO           22          /*!< GPIO number for I2C master clock */
#define I2C_MASTER_SDA_IO           21          /*!< GPIO number for I2C master data  */
#define I2C_MASTER_NUM              I2C_NUM_0   /*!< I2C master port number */
#define I2C_MASTER_FREQ_HZ          400000      /*!< I2C master clock frequency (400kHz Fast Mode) */
#define I2C_MASTER_TX_BUF_DISABLE   0           /*!< I2C master doesn't need buffer */
#define I2C_MASTER_RX_BUF_DISABLE   0           /*!< I2C master doesn't need buffer */
#define I2C_MASTER_TIMEOUT_MS       1000

// ADXL345 I2C Address & Registers (from ADXL345 datasheet)
#define ADXL345_ADDR                0x53        /*!< Default I2C address when ALT ADDRESS pin is grounded (0x1D if high) */
#define ADXL345_EXPECTED_DEVID      0xE5        /*!< Expected Device ID value */

#define ADXL345_REG_DEVID           0x00        /*!< Device ID */
#define ADXL345_REG_BW_RATE         0x2C        /*!< Data rate and power mode control */
#define ADXL345_REG_POWER_CTL       0x2D        /*!< Power-saving features control */
#define ADXL345_REG_DATA_FORMAT     0x31        /*!< Data format control */
#define ADXL345_REG_DATAX0          0x32        /*!< X-Axis Data 0 (6 bytes total: X0, X1, Y0, Y1, Z0, Z1) */

// Scale factor for Full Resolution mode: ~3.9 mg/LSB (1/256 g/LSB)
#define ADXL345_MG2G_MULTIPLIER     (0.00390625f)
#define GRAVITY_EARTH               (9.80665f)

/**
 * @brief Initialize ESP32 I2C master peripheral
 */
static esp_err_t i2c_master_init(void)
{
    i2c_config_t conf = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = I2C_MASTER_SDA_IO,
        .scl_io_num = I2C_MASTER_SCL_IO,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = I2C_MASTER_FREQ_HZ,
    };

    esp_err_t err = i2c_param_config(I2C_MASTER_NUM, &conf);
    if (err != ESP_OK) {
        return err;
    }

    return i2c_driver_install(I2C_MASTER_NUM, conf.mode,
                              I2C_MASTER_RX_BUF_DISABLE,
                              I2C_MASTER_TX_BUF_DISABLE, 0);
}

/**
 * @brief Write a single byte to an ADXL345 register
 */
static esp_err_t adxl345_write_reg(uint8_t reg_addr, uint8_t data)
{
    uint8_t write_buf[2] = {reg_addr, data};
    return i2c_master_write_to_device(I2C_MASTER_NUM, ADXL345_ADDR,
                                      write_buf, sizeof(write_buf),
                                      pdMS_TO_TICKS(I2C_MASTER_TIMEOUT_MS));
}

/**
 * @brief Read one or more bytes starting from an ADXL345 register
 */
static esp_err_t adxl345_read_regs(uint8_t reg_addr, uint8_t *data, size_t len)
{
    return i2c_master_write_read_device(I2C_MASTER_NUM, ADXL345_ADDR,
                                        &reg_addr, 1,
                                        data, len,
                                        pdMS_TO_TICKS(I2C_MASTER_TIMEOUT_MS));
}

/**
 * @brief Verify and configure the ADXL345 sensor
 */
static esp_err_t adxl345_init(void)
{
    uint8_t devid = 0;
    esp_err_t err = adxl345_read_regs(ADXL345_REG_DEVID, &devid, 1);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to communicate with ADXL345 over I2C (err=0x%x)", err);
        return err;
    }

    if (devid != ADXL345_EXPECTED_DEVID) {
        ESP_LOGE(TAG, "Unexpected Device ID: 0x%02X (expected 0x%02X)", devid, ADXL345_EXPECTED_DEVID);
        return ESP_ERR_INVALID_RESPONSE;
    }
    ESP_LOGI(TAG, "ADXL345 detected! Device ID: 0x%02X", devid);

    // Set Output Data Rate to 100 Hz (0x0A)
    err = adxl345_write_reg(ADXL345_REG_BW_RATE, 0x0A);
    if (err != ESP_OK) return err;

    // Set Data Format: FULL_RES bit (D3 = 1) and +/- 16g range (D1:D0 = 11) -> 0x0B
    // Keeps 4 mg/LSB scale factor across all g-ranges
    err = adxl345_write_reg(ADXL345_REG_DATA_FORMAT, 0x0B);
    if (err != ESP_OK) return err;

    // Enable Measurement Mode: Measure bit (D3 = 1) in POWER_CTL -> 0x08
    err = adxl345_write_reg(ADXL345_REG_POWER_CTL, 0x08);
    if (err != ESP_OK) return err;

    uint8_t bw_rate = 0, data_format = 0, power_ctl = 0;
    adxl345_read_regs(ADXL345_REG_BW_RATE, &bw_rate, 1);
    adxl345_read_regs(ADXL345_REG_DATA_FORMAT, &data_format, 1);
    adxl345_read_regs(ADXL345_REG_POWER_CTL, &power_ctl, 1);

    ESP_LOGI(TAG, "Sensor Info -> Rate: 100Hz (0x%02X) | Format: Full-Res +/-16g (0x%02X) | Power: Measure (0x%02X)",
             bw_rate, data_format, power_ctl);

    return ESP_OK;
}

void app_main(void)
{
    ESP_ERROR_CHECK(i2c_master_init());
    ESP_LOGI(TAG, "I2C initialized successfully (SDA: GPIO%d, SCL: GPIO%d)",
             I2C_MASTER_SDA_IO, I2C_MASTER_SCL_IO);

    while (adxl345_init() != ESP_OK) {
        ESP_LOGW(TAG, "Retrying ADXL345 initialization in 1 second...");
        vTaskDelay(pdMS_TO_TICKS(1000));
    }

    uint8_t raw_data[6];

    while (1) {
        // Multi-byte read of all 6 axis data registers (0x32 to 0x37) to ensure data consistency
        esp_err_t err = adxl345_read_regs(ADXL345_REG_DATAX0, raw_data, sizeof(raw_data));
        if (err == ESP_OK) {
            // Data is two's complement little-endian (DATAx0 is LSB, DATAx1 is MSB)
            int16_t raw_x = (int16_t)((raw_data[1] << 8) | raw_data[0]);
            int16_t raw_y = (int16_t)((raw_data[3] << 8) | raw_data[2]);
            int16_t raw_z = (int16_t)((raw_data[5] << 8) | raw_data[4]);

            // Convert raw counts to g and m/s^2
            float accel_x_g = raw_x * ADXL345_MG2G_MULTIPLIER;
            float accel_y_g = raw_y * ADXL345_MG2G_MULTIPLIER;
            float accel_z_g = raw_z * ADXL345_MG2G_MULTIPLIER;

            float accel_x_ms2 = accel_x_g * GRAVITY_EARTH;
            float accel_y_ms2 = accel_y_g * GRAVITY_EARTH;
            float accel_z_ms2 = accel_z_g * GRAVITY_EARTH;

            printf("Raw [X:%6d Y:%6d Z:%6d] | "
                   "Accel [X:%+6.3f g, Y:%+6.3f g, Z:%+6.3f g] | "
                   "[X:%+6.2f m/s^2, Y:%+6.2f m/s^2, Z:%+6.2f m/s^2]\n",
                   raw_x, raw_y, raw_z,
                   accel_x_g, accel_y_g, accel_z_g,
                   accel_x_ms2, accel_y_ms2, accel_z_ms2);
        } else {
            ESP_LOGE(TAG, "Failed to read acceleration data (err=0x%x)", err);
        }

        vTaskDelay(pdMS_TO_TICKS(200));
    }
}
