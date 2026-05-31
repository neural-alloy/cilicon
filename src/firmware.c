/* Bare-metal Cortex-M3 firmware (stand-in for STM32/Zephyr style MCU code).
 * No libc, no OS. cilicon builds it with arm-none-eabi-gcc and BOOTS it in
 * qemu-system-arm. ARM semihosting is the virtual UART: SYS_WRITE0 prints to
 * the emulator console, proving the firmware reset, reached main, and ran. */

/* ---- ARM semihosting (talks to the emulator host) ---- */
static int semihost(int op, void *arg) {
    register int r0 __asm__("r0") = op;
    register void *r1 __asm__("r1") = arg;
    __asm__ volatile("bkpt 0xAB" : "+r"(r0) : "r"(r1) : "memory");
    return r0;
}
static void uart_puts(const char *s) {
    semihost(0x04 /* SYS_WRITE0 */, (void *)s);
}

int main(void) {
    uart_puts("BOOT OK\n");
    uart_puts("firmware: reached main, UART handshake ok\n");

    /* SYS_EXIT(ApplicationExit) so qemu returns cleanly */
    int args[2] = {0x20026, 0};
    semihost(0x18, args);
    for (;;) { }
    return 0;
}

/* ---- minimal startup: reset sets SP/PC from this vector table ---- */
void reset_handler(void) {
    main();
    for (;;) { }
}

extern unsigned long _stack_top;
__attribute__((section(".vectors"), used))
void (*const vectors[])(void) = {
    (void (*)(void))(&_stack_top),  /* initial stack pointer */
    reset_handler,                  /* reset vector */
    reset_handler,                  /* NMI  */
    reset_handler,                  /* HardFault */
};
