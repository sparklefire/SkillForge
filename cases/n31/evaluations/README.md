# N31 评测结果

本目录只保存可公开复现的结构化评测结果，不保存关键帧、原始视频、录音、手册或真实面单。

- `visual_sequence_review_v1.json`：Step 3.7 对13个Gold步骤附近安全关键帧序列的严格视觉复核。
- `multisource_comparison_v1.json`：手册、视频、专家口述和多源组合的覆盖消融，以及质检修订前后对比。
- `dgx_visual_compute_v1.json`：DGX Spark GB10 对6段自摄安全派生视频执行原生 CUDA 特征计算和场景变化候选筛选的可复现指标。
- `temporal_action_windows_v1.json`：将同一来源时间线上的已审核关键帧区间合并为19个Gold步骤对齐候选窗口，并绑定附近的DGX场景候选时间点。
- `pdf_structure_v1.json`：两份手册58页的结构分块、中文OCR质量门禁和三项页码检索验证；不含手册正文、页面图或私有检索索引。
- `source_candidate_synthesis_v1.json`：视频、PDF和口述候选的粗细粒度合并、依赖排序、置信度分解和审核路由。
- `deterministic_grounding_gate_v1.json`：跨步骤工具、错误参数值、无来源安全提示和绝对安全承诺四个隔离篡改场景的检出、Evidence边界、局部修订和复检结果。
- `semantic_review_v1.json`：Step 3.7 high对13步结构化Gold和36条引用Evidence陈述执行来源曲解、来源冲突、顺序风险和异常遗漏复核；不含原始素材或手册正文。
- `selective_rebuild_v1.json`：从Revision Audit重放After SOP，并确定只需失效的步骤、测验题和视频镜头；保存未变化单元验证和安全边界。
- `video_preview_manifest_v1.json`：6段 `LOCAL_QA_PASSED` 自摄安全视频的私有低码率预览清单；绑定交付配置、源/预览SHA-256、技术参数、同一时间轴映射和逐项QA，不保存媒体路径或原始内容。
- `agent_tool_trace_v1.json`：Perception、SOP、Creator、Verifier、Revision五类Agent的13个工具、14次调用、5次交接和19个成果哈希；包含质检—修订—复检环，不保存成果路径或原始媒体。

视觉复核是 `MODEL_INFERENCE`，不能覆盖手册事实或实际操作者确认。`PARTIAL` 和 `NOT_VISIBLE` 表示当前抽帧不足，不等价于Gold步骤错误。

DGX视觉计算只输出来源ID、源文件哈希、视频时间点和数值特征，不保存关键帧路径。它的语义范围固定为 `CANDIDATE_SELECTION_ONLY`：可以证明视频帧确实在DGX上经过GPU计算，但不能自动证明某个SOP动作已经完成。

连续动作报告的语义范围固定为 `GOLD_ALIGNED_CANDIDATE_WINDOW_ONLY`。它只整合已经过Gold步骤对齐和视觉复核的时间区间，不等于无Gold引导的通用动作识别；S04即使绑定到60–75秒窗口和3个DGX候选时间点，仍保留 `NOT_VISIBLE`，不会因时间邻近自动升级为视觉支持。

PDF结构报告绑定两份本地手册的SHA-256，只公开页数、结构类型计数、OCR状态和检索命中页码。原始手册、607个含正文检索块和页面预览均保留在Git忽略目录，且不发送外部模型。

确定性门禁报告只读取公开结构化Gold与约束，外部模型调用为0，不读取原始媒体或凭证。四个场景必须全部检出和恢复，任一场景出现额外冲突、未修订或复检残留都会把报告标为 `FAILED`。

高推理语义复核只发送结构化步骤、对应Evidence陈述和约束，模型结果固定归类为 `MODEL_INFERENCE`，不能自动修改Gold。响应必须逐步覆盖且只能引用当前步骤的Evidence；来源冲突和顺序风险还需满足独立来源或跨步骤边界。

选择性重建报告完全在本地生成，外部模型调用为0。它必须能用Revision Audit精确重放After SOP，并逐题比较测验、逐步骤绑定视频场景；无法证明边界时报告不能标为 `PASSED`。

低码率预览清单可以进入Git，但对应MP4不能进入Git。预览只从 `LOCAL_QA_PASSED` 自摄安全视频生成，固定为本机私有派生物，不发送第三方或外部模型；Web只有在文件大小和SHA-256与清单一致时才提供Range播放。

Agent工具追踪从当前成果重新计算，不重跑模型。它对每个Agent的授权工具、每次调用的输入输出成果ID和每个成果的大小/SHA-256做交叉验证；越权调用、未知成果、摘要漂移或复检闭环缺失时不能标为 `COMPLETED`。
