# 运行时评测

本目录保存可公开复现、且不含原始素材的运行时报告：

- `runtime_benchmark_local.json`：本地开发机结果。
- `runtime_benchmark_dgx.json`：NVIDIA DGX Spark结果。

## 复现

```bash
# 本机
bash scripts/run_runtime_benchmark.sh local

# DGX：~/skillforge/app
bash scripts/run_runtime_benchmark.sh dgx
```

默认配置为2次预热和20次正式测量，包含两个基准：

1. `GOLD_WORKFLOW`：直接调用Python执行已审核结构化Gold的质检、局部修订和成果输出。
2. `WEB_LIVE_RERUN`：通过进程内HTTP测试客户端执行Web现场重算；不测网络传输。

每次迭代都必须得到 `GOLD / FINAL / COMPLETED`、严重问题5→0、4项局部修订和外部模型调用0次，否则基准失败。临时输出只写入项目 `outputs/` 下的临时目录，结束后自动删除。

## 指标口径

当前DGX结果：

| 基准 | 中位数 | P95 |
|---|---:|---:|
| 直接Python Gold闭环 | 37.626 ms | 37.758 ms |
| Web现场重算 | 44.800 ms | 56.040 ms |

基准进程高水位RSS为86,491,136字节。它不是整机内存、GPU显存或单次迭代增量。

这些数字不包含视频转码、PDF解析、ASR、视觉模型或其他外部模型调用，不能表述为“原始多模态素材完整处理耗时”或“GPU推理速度”。
