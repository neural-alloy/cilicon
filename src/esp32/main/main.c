/* ESP32 / FreeRTOS firmware. app_main() is a FreeRTOS task started by the
 * ESP-IDF bootloader. cilicon builds it with the Xtensa toolchain and boots it in
 * qemu-system-xtensa; the UART boot log proves the bootloader ran, the app
 * started, and FreeRTOS is scheduling. */
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

void app_main(void) {
    printf("Hello from cilicon on ESP32\n");
    for (int i = 3; i > 0; i--) {
        printf("cilicon: FreeRTOS tick %d\n", i);
        vTaskDelay(pdMS_TO_TICKS(200));   /* proves the scheduler is running */
    }
    printf("cilicon: app_main done\n");
}
