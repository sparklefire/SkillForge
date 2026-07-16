# N31 评测结果

本目录只保存可公开复现的结构化评测结果，不保存关键帧、原始视频、录音、手册或真实面单。

- `visual_sequence_review_v1.json`：Step 3.7 对13个Gold步骤附近安全关键帧序列的严格视觉复核。
- `multisource_comparison_v1.json`：手册、视频、专家口述和多源组合的覆盖消融，以及质检修订前后对比。

视觉复核是 `MODEL_INFERENCE`，不能覆盖手册事实或实际操作者确认。`PARTIAL` 和 `NOT_VISIBLE` 表示当前抽帧不足，不等价于Gold步骤错误。
