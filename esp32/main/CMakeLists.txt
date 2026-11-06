idf_component_register(SRCS "3d-printer-evil-frequencies.c"
                    INCLUDE_DIRS "."
                    REQUIRES esp_driver_i2c esp_wifi esp_event esp_netif mqtt nvs_flash esp_driver_gpio)
