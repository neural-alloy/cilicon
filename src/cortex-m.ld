/* Linker script for the qemu lm3s6965evb (Cortex-M3): flash @ 0, sram @ 0x20000000 */
MEMORY {
  FLASH (rx) : ORIGIN = 0x00000000, LENGTH = 256K
  SRAM  (rwx): ORIGIN = 0x20000000, LENGTH = 64K
}
ENTRY(reset_handler)
SECTIONS {
  .text : {
    KEEP(*(.vectors))
    *(.text*)
    *(.rodata*)
  } > FLASH

  .data : { *(.data*) } > SRAM AT > FLASH
  .bss  : { *(.bss*)  } > SRAM

  _stack_top = ORIGIN(SRAM) + LENGTH(SRAM);
}
