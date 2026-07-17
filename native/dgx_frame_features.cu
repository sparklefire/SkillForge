#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fs = std::filesystem;

#define CUDA_CHECK(call)                                                       \
  do {                                                                         \
    cudaError_t error = (call);                                                 \
    if (error != cudaSuccess) {                                                 \
      throw std::runtime_error(std::string(#call) + ": " +                    \
                               cudaGetErrorString(error));                      \
    }                                                                          \
  } while (0)

struct Image {
  int width = 0;
  int height = 0;
  std::vector<unsigned char> pixels;
};

std::string next_token(std::istream &input) {
  std::string token;
  while (input >> token) {
    if (!token.empty() && token[0] == '#') {
      std::string ignored;
      std::getline(input, ignored);
      continue;
    }
    return token;
  }
  throw std::runtime_error("unexpected end of PGM header");
}

Image read_pgm(const fs::path &path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) {
    throw std::runtime_error("cannot open frame: " + path.string());
  }
  if (next_token(input) != "P5") {
    throw std::runtime_error("only binary PGM (P5) is supported");
  }
  Image image;
  image.width = std::stoi(next_token(input));
  image.height = std::stoi(next_token(input));
  const int maximum = std::stoi(next_token(input));
  if (image.width <= 0 || image.height <= 0 || maximum != 255) {
    throw std::runtime_error("invalid PGM dimensions or maximum value");
  }
  input.get();
  image.pixels.resize(static_cast<size_t>(image.width) * image.height);
  input.read(reinterpret_cast<char *>(image.pixels.data()),
             static_cast<std::streamsize>(image.pixels.size()));
  if (input.gcount() != static_cast<std::streamsize>(image.pixels.size())) {
    throw std::runtime_error("truncated PGM frame");
  }
  return image;
}

__global__ void stats_kernel(const unsigned char *image, int width, int height,
                             unsigned long long *sum,
                             unsigned long long *sum_squares,
                             unsigned long long *edge_sum) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  const int count = width * height;
  if (index >= count) {
    return;
  }
  const unsigned long long value = image[index];
  atomicAdd(sum, value);
  atomicAdd(sum_squares, value * value);
  const int x = index % width;
  const int y = index / width;
  if (x + 1 < width) {
    atomicAdd(edge_sum, static_cast<unsigned long long>(
                            abs(static_cast<int>(value) - image[index + 1])));
  }
  if (y + 1 < height) {
    atomicAdd(edge_sum, static_cast<unsigned long long>(
                            abs(static_cast<int>(value) - image[index + width])));
  }
}

__global__ void diff_kernel(const unsigned char *current,
                            const unsigned char *previous, int count,
                            unsigned long long *difference_sum) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index < count) {
    atomicAdd(difference_sum,
              static_cast<unsigned long long>(
                  abs(static_cast<int>(current[index]) - previous[index])));
  }
}

std::vector<fs::path> frame_paths(const fs::path &directory) {
  std::vector<fs::path> paths;
  for (const auto &entry : fs::directory_iterator(directory)) {
    if (entry.is_regular_file() && entry.path().extension() == ".pgm") {
      paths.push_back(entry.path());
    }
  }
  std::sort(paths.begin(), paths.end());
  if (paths.empty()) {
    throw std::runtime_error("no PGM frames found");
  }
  return paths;
}

int main(int argc, char **argv) {
  try {
    if (argc != 2) {
      std::cerr << "usage: dgx_frame_features FRAME_DIRECTORY\n";
      return 2;
    }
    const auto paths = frame_paths(argv[1]);
    const Image first = read_pgm(paths.front());
    const int pixel_count = first.width * first.height;
    const size_t bytes = static_cast<size_t>(pixel_count);

    cudaDeviceProp properties{};
    CUDA_CHECK(cudaGetDeviceProperties(&properties, 0));
    int runtime_version = 0;
    CUDA_CHECK(cudaRuntimeGetVersion(&runtime_version));
    CUDA_CHECK(cudaSetDevice(0));

    unsigned char *current = nullptr;
    unsigned char *previous = nullptr;
    unsigned long long *counters = nullptr;
    CUDA_CHECK(cudaMalloc(reinterpret_cast<void **>(&current), bytes));
    CUDA_CHECK(cudaMalloc(reinterpret_cast<void **>(&previous), bytes));
    CUDA_CHECK(cudaMalloc(reinterpret_cast<void **>(&counters),
                          4 * sizeof(unsigned long long)));

    cudaEvent_t start{};
    cudaEvent_t stop{};
    CUDA_CHECK(cudaEventCreate(&start));
    CUDA_CHECK(cudaEventCreate(&stop));
    float total_kernel_ms = 0.0f;
    bool has_previous = false;
    const int threads = 256;
    const int blocks = (pixel_count + threads - 1) / threads;

    struct FrameMetrics {
      double mean = 0.0;
      double contrast = 0.0;
      double edge = 0.0;
      double scene_change = 0.0;
    };
    std::vector<FrameMetrics> metrics;
    metrics.reserve(paths.size());

    for (const auto &path : paths) {
      const Image image = read_pgm(path);
      if (image.width != first.width || image.height != first.height) {
        throw std::runtime_error("all frames must have identical dimensions");
      }
      CUDA_CHECK(cudaMemcpy(current, image.pixels.data(), bytes,
                            cudaMemcpyHostToDevice));
      CUDA_CHECK(cudaMemset(counters, 0, 4 * sizeof(unsigned long long)));
      CUDA_CHECK(cudaEventRecord(start));
      stats_kernel<<<blocks, threads>>>(current, first.width, first.height,
                                        counters, counters + 1, counters + 2);
      CUDA_CHECK(cudaGetLastError());
      if (has_previous) {
        diff_kernel<<<blocks, threads>>>(current, previous, pixel_count,
                                         counters + 3);
        CUDA_CHECK(cudaGetLastError());
      }
      CUDA_CHECK(cudaEventRecord(stop));
      CUDA_CHECK(cudaEventSynchronize(stop));
      float elapsed = 0.0f;
      CUDA_CHECK(cudaEventElapsedTime(&elapsed, start, stop));
      total_kernel_ms += elapsed;

      unsigned long long host[4]{};
      CUDA_CHECK(cudaMemcpy(host, counters, sizeof(host),
                            cudaMemcpyDeviceToHost));
      const double count = static_cast<double>(pixel_count);
      const double mean = host[0] / count;
      const double variance = std::max(0.0, host[1] / count - mean * mean);
      const double edge_count =
          static_cast<double>((first.width - 1) * first.height +
                              first.width * (first.height - 1));
      metrics.push_back({mean, std::sqrt(variance), host[2] / edge_count,
                         has_previous ? host[3] / (count * 255.0) : 0.0});
      CUDA_CHECK(cudaMemcpy(previous, current, bytes, cudaMemcpyDeviceToDevice));
      has_previous = true;
    }

    CUDA_CHECK(cudaEventDestroy(start));
    CUDA_CHECK(cudaEventDestroy(stop));
    CUDA_CHECK(cudaFree(current));
    CUDA_CHECK(cudaFree(previous));
    CUDA_CHECK(cudaFree(counters));

    std::cout << std::fixed << std::setprecision(6);
    std::cout << "{\"backend\":\"cuda_native\",\"device\":{";
    std::cout << "\"name\":\"" << properties.name << "\",";
    std::cout << "\"compute_capability\":\"" << properties.major << "."
              << properties.minor << "\",";
    std::cout << "\"total_global_memory_bytes\":"
              << properties.totalGlobalMem << "},";
    std::cout << "\"cuda_runtime\":\"" << runtime_version / 1000 << "."
              << (runtime_version % 1000) / 10 << "\",";
    std::cout << "\"width\":" << first.width << ",\"height\":"
              << first.height << ",\"gpu_kernel_ms\":" << total_kernel_ms
              << ",\"frames\":[";
    for (size_t index = 0; index < metrics.size(); ++index) {
      if (index) {
        std::cout << ',';
      }
      const auto &item = metrics[index];
      std::cout << "{\"frame_index\":" << index + 1
                << ",\"mean_luma\":" << item.mean
                << ",\"contrast\":" << item.contrast
                << ",\"edge_energy\":" << item.edge
                << ",\"scene_change_score\":" << item.scene_change << '}';
    }
    std::cout << "]}\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "dgx_frame_features: " << error.what() << '\n';
    return 1;
  }
}
