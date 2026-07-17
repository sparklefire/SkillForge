# N31 评测结果

本目录只保存可公开复现的结构化评测结果，不保存关键帧、原始视频、录音、手册或真实面单。

- `visual_sequence_review_v1.json`：Step 3.7 对13个Gold步骤附近安全关键帧序列的严格视觉复核。
- `multisource_comparison_v1.json`：手册、视频、专家口述和多源组合的覆盖消融，以及质检修订前后对比。
- `dgx_visual_compute_v1.json`：DGX Spark GB10 对6段自摄安全派生视频执行原生 CUDA 特征计算和场景变化候选筛选的可复现指标。
- `temporal_action_windows_v1.json`：将同一来源时间线上的已审核关键帧区间合并为19个Gold步骤对齐候选窗口，并绑定附近的DGX场景候选时间点。
- `pdf_structure_v1.json`：两份手册58页的结构分块、中文OCR质量门禁和三项页码检索验证；不含手册正文、页面图或私有检索索引。

视觉复核是 `MODEL_INFERENCE`，不能覆盖手册事实或实际操作者确认。`PARTIAL` 和 `NOT_VISIBLE` 表示当前抽帧不足，不等价于Gold步骤错误。

DGX视觉计算只输出来源ID、源文件哈希、视频时间点和数值特征，不保存关键帧路径。它的语义范围固定为 `CANDIDATE_SELECTION_ONLY`：可以证明视频帧确实在DGX上经过GPU计算，但不能自动证明某个SOP动作已经完成。

连续动作报告的语义范围固定为 `GOLD_ALIGNED_CANDIDATE_WINDOW_ONLY`。它只整合已经过Gold步骤对齐和视觉复核的时间区间，不等于无Gold引导的通用动作识别；S04即使绑定到60–75秒窗口和3个DGX候选时间点，仍保留 `NOT_VISIBLE`，不会因时间邻近自动升级为视觉支持。

PDF结构报告绑定两份本地手册的SHA-256，只公开页数、结构类型计数、OCR状态和检索命中页码。原始手册、607个含正文检索块和页面预览均保留在Git忽略目录，且不发送外部模型。
