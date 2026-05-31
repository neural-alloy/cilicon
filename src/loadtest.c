/* A target with a real bug: it dereferences a null pointer. On hardware you'd
 * flash it, watch it crash, and start over. cilicon runs it under qemu-user, the
 * emulator delivers SIGSEGV, and the failure is caught BEFORE any metal. */
#include <stdio.h>

int main(void) {
    printf("loadtest: starting up\n");
    fflush(stdout);      /* so the partial output survives the crash, as evidence */

    int *p = (int *)0;   /* oops: null pointer */
    *p = 42;             /* SIGSEGV under emulation */

    printf("loadtest: ok\n");   /* never reached */
    return 0;
}
