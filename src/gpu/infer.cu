/* GPU inference smoke test (stands in for "deserialize the engine, run one
 * inference"). forge cross-builds this with nvcc and runs it on a REAL cloud
 * GPU — one dense layer y = relu(W·x) — proving CUDA actually executes. */
#include <cstdio>
#include <cuda_runtime.h>

__global__ void dense_relu(const float *W, const float *x, float *y, int n) {
    int row = blockIdx.x * blockDim.x + threadIdx.x;
    if (row < n) {
        float acc = 0.0f;
        for (int k = 0; k < n; k++) acc += W[row * n + k] * x[k];
        y[row] = acc > 0.0f ? acc : 0.0f;   /* relu */
    }
}

int main(void) {
    cudaDeviceProp p;
    if (cudaGetDeviceProperties(&p, 0) != cudaSuccess) {
        printf("no CUDA device visible\n");
        return 1;
    }
    printf("gpu: %s\n", p.name);

    const int n = 64;
    float *W, *x, *y;
    cudaMallocManaged(&W, (size_t)n * n * sizeof(float));
    cudaMallocManaged(&x, n * sizeof(float));
    cudaMallocManaged(&y, n * sizeof(float));
    for (int i = 0; i < n * n; i++) W[i] = ((i % 7) - 3) * 0.01f;
    for (int i = 0; i < n; i++) x[i] = 1.0f;

    dense_relu<<<(n + 31) / 32, 32>>>(W, x, y, n);  /* one inference */
    cudaError_t e = cudaDeviceSynchronize();
    if (e != cudaSuccess) {
        printf("kernel failed: %s\n", cudaGetErrorString(e));
        return 1;
    }

    double s = 0;
    for (int i = 0; i < n; i++) s += y[i];
    printf("engine loaded, 1 infer -> checksum %.4f\n", s);
    return 0;
}
