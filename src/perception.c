/* Stand-in for a robotics "perception" node cross-built for an ARM Linux SoC
 * (e.g. Jetson/i.MX userspace). cilicon cross-compiles it and runs it under
 * qemu-user to prove the ELF loads, libs resolve, and it reaches its work. */
#include <stdio.h>

int main(void) {
    /* pretend to load a tiny inference engine and run one inference */
    const float w[4] = {0.10f, 0.20f, 0.30f, 0.40f};
    float x = 2.0f, acc = 0.0f;
    for (int i = 0; i < 4; i++) acc += w[i] * x;

    printf("perception: engine ok, 1 infer -> %.3f\n", acc);
    return 0;
}
