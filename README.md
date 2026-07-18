# 匠传 SkillForge

把专家操作视频、设备手册和口述经验，自动转化为可追溯、可验证、会自我修订的多模态培训包。

## 当前目标

在 2026-07-22 前交付一条稳定、可演示的主链路：

```text
视频 / PDF / 专家录音
        ↓
多模态解析与证据提取
        ↓
结构化 SOP
        ↓
培训内容创作
        ↓
规则质检 + 模型复核
        ↓
局部自动修订
        ↓
SOP / 检查清单 / 测验 / 海报 / 培训视频
```

## 真实主案例

唯一真实主案例已经锁定为“汉印 N31 电子面单打印机更换标签纸、介质学习与试印验收”。真实操作者口述已经转为12条带时间点证据，13步 `Gold v1` 和最终评测见 [N31 案例目录](./cases/n31/README.md)。

N31 原始视频、录音、厂商手册和完整授权书只保存在被 Git 忽略的 `cases/n31/input/`；仓库只保存脱敏来源记录、模板和人工审核结果。

## 已持久化内容

- [赛事与方向评估](./评估NVIDIA的黑客松方向.md)
- [赛事要求对齐矩阵](./docs/赛事要求对齐矩阵.md)
- [详细任务拆解](./docs/SkillForge任务拆解.md)
- [环境与接入说明](./docs/环境与接入.md)
- [执行状态](./docs/执行状态.md)
- [模型路由配置](./config/models.json)
- [架构与数据边界](./docs/架构与数据边界.md)
- [参赛提交材料](./docs/参赛提交材料.md)

## 安全约定

- 真实 API Key 只存放在本地 `.env`，该文件已被 `.gitignore` 排除。
- `.env` 应保持 `600` 权限。
- README、日志、截图、演示视频中不得出现完整密钥。
- 原始视频、设备手册和专家录音默认不提交代码仓库。
- 若项目将公开或多人共享，应立即轮换当前 API Key，并使用团队密钥管理工具。

## 快速验证

验证 Step Plan：

```bash
python3 scripts/verify_step_plan.py
```

验证 DGX Spark：

```bash
bash scripts/check_dgx.sh
```

以上两个检查脚本从本地 `.env` 读取所需配置，不输出密钥。

安装只监听 DGX 回环地址的用户级离线演示服务，并通过 SSH 隧道访问：

```bash
# 在 DGX 的 ~/skillforge/app 中执行
bash scripts/manage_dgx_demo_service.sh install
bash scripts/manage_dgx_demo_service.sh restart-test

# 在本机项目目录执行
bash scripts/dgx_demo_tunnel.sh --smoke
bash scripts/dgx_demo_tunnel.sh
```

用户服务启用失败自动重启，SSH 隧道默认映射到本机 `127.0.0.1:17860`。服务不开放公网端口、不需要 Docker；完整说明见 [DGX 用户级演示服务](./deploy/systemd/README.md)。

## 运行 P0 模拟闭环

模拟案例明确标注为虚构数据，只用于验证工程闭环，不能作为真实设备操作指南：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e . --no-deps --no-build-isolation
.venv/bin/python -m pytest
.venv/bin/python -m skillforge.demo
```

也可以直接运行 `bash scripts/setup_native.sh` 完成上述环境安装。

演示输出写入被 Git 忽略的 `outputs/demo_run/`，包含首轮 SOP、问题与证据、局部修订审计、修订后 SOP、检查清单、测验和工作流记录。

原生摄取和 Web Demo：

```bash
.venv/bin/python scripts/generate_synthetic_assets.py
.venv/bin/python -m skillforge.ingest \
  --video outputs/synthetic_assets/synthetic_operation.mp4 \
  --pdf outputs/synthetic_assets/synthetic_manual.pdf \
  --audio outputs/synthetic_assets/synthetic_expert.wav \
  --output outputs/synthetic_ingest \
  --frame-interval 2 \
  --synthetic
bash scripts/check_native.sh
bash scripts/start_native.sh
```

Web 默认监听 `0.0.0.0:7860`。页面包含上传预处理、五类Agent与工具追踪、可恢复阶段与资源统计、三来源候选合并、质检问题与证据、修订前后对比、局部修订审计、无来源内容拒绝门禁、手机检查清单、培训测验、连续动作候选窗口、PDF结构验证、受众/语言/时长交付配置、6段低码率素材预览和80秒培训视频，并可下载最终 SOP、Agent工具追踪、候选合并报告、门禁报告、检查清单、测验、A4海报、培训视频、视频生成清单、视频证据包、连续动作候选窗口、PDF结构报告和修订记录。下载白名单不包含原始素材。ASR 默认关闭；只有同时勾选外部处理授权时，规范化音频才会发送给 StepAudio。

关键帧视觉理解和 SOP 规划同样默认关闭。只有勾选对应能力并确认外部处理授权时，关键帧或 Evidence Catalog 才会发送给 Step Plan；原始 PDF 和原始视频不会由上传接口自动外发。

真实 N31 案例可使用一条命令重跑本地预处理模式：

```bash
bash scripts/run_n31_local.sh
bash scripts/start_native.sh
```

如果需要从三段 `_private_review_v2` 原片重新生成隐私安全视频，再运行完整本地模式：

```bash
bash scripts/run_n31_local.sh --with-video-processing
```

两种模式都不会调用外部模型。默认模式复用已通过检查的安全成片，依次完成8来源摄取和142条本地 Evidence Catalog；存在已审核 Gold 时，还会从完整154条目录中生成视频18条、手册7条、口述8条来源候选，执行粗细粒度处理、同义合并和依赖排序，再运行 `GOLD / FINAL` 最终评测；否则运行13步候选 SOP 的 `NOT_GOLD / PROVISIONAL_ONLY` 彩排。若 N31 预处理输出不存在，才回退到无版权模拟案例。

单独重建三类来源候选与合并报告：

```bash
bash scripts/build_n31_source_candidates.sh
```

该阶段不读取 Gold 步骤文本，也不调用外部模型；它只读取真实 Evidence Catalog、审核后的来源候选和候选语义规范。33条候选经过Schema与来源交叉检查后合成为13步无环依赖图，8条过粗候选拆成18个片段，8条过细候选进入合并。每步置信度由证据分类权威性、审核状态、多源佐证和负面观察共同计算，并给出可复核分解。当前6步为高、6步为中、1步为低；S04因视频复核为不可见，只保留手册支持并以 `0.691 / LOW / HUMAN_REVIEW_REQUIRED` 进入人工确认，不把时间邻近误写成视觉事实。成功路径无需独立复位，异常回退则逐步记录。

单独重建确定性无来源内容门禁报告：

```bash
bash scripts/build_n31_grounding_gate.sh
```

该门禁对跨步骤工具、合法参数名但错误数值、无来源安全提示和“100%安全”承诺分别执行独立篡改，要求每项都被检出、引用当前步骤Evidence边界、完成局部恢复并在复检后保持0个残留冲突。全过程只处理公开结构化Gold，不读取原始媒体、不访问凭证，也不调用外部模型。

当仓库中存在 `cases/n31/gold/gold_sop.json` 时，`run_n31_local.sh` 会自动改用实际操作者审核的 Gold 约束，页面显示 `GOLD / FINAL`。重新执行专家录音ASR、术语核对、Gold固化和最终评测使用：

```bash
bash scripts/run_n31_expert.sh
```

该命令会把规范化录音发送给 StepAudio；原始录音和中间输出均处于 Git 忽略目录。

对已经通过本地隐私检查的安全关键帧执行 Step 3.7 严格视觉复核：

```bash
bash scripts/run_n31_visual.sh
```

该流程按 Gold 步骤组合直接关键帧和同源相邻帧，锁定 Evidence ID、来源和时间点，非法 JSON、未知引用或中途失败不会发布为评测结果。当前13步基线为12步 `PARTIAL`、1步 `NOT_VISIBLE`、0步 `CONTRADICTED`；它证明当前稀疏关键帧可观察大部分动作，但不能替代手册和操作者确认。

在 DGX Spark 上对6段自摄安全派生视频执行原生 CUDA 场景变化筛选：

```bash
bash scripts/run_n31_dgx_visual.sh
```

该命令只接受案例清单中 `LOCAL_QA_PASSED` 的视频，并且必须显式开启 DGX 安全派生素材门禁。FFmpeg保留真实帧时间戳，GB10 CUDA内核计算亮度、对比度、边缘能量和相邻帧变化，结果写入 `cases/n31/evaluations/dgx_visual_compute_v1.json`。第三方教程、手册、真实面单和私有照片不参与；GPU结果只筛选候选帧，不自动产生SOP语义结论。

从Gold步骤、严格视觉复核和DGX场景候选生成连续动作候选窗口：

```bash
bash scripts/build_n31_temporal_windows.sh
```

当前报告将51次已审核帧引用合并为19个同源时间窗口，覆盖13个Gold步骤和6段视频，其中12个窗口绑定到41个去重DGX候选时间点。范围固定为 `GOLD_ALIGNED_CANDIDATE_WINDOW_ONLY`：它不是通用动作识别，也不会把时间邻近自动当成动作完成证据；S04仍保持 `NOT_VISIBLE`。

对本地设备手册执行结构化分块、中文OCR和页码保真检索验证：

```bash
bash scripts/build_n31_pdf_structure.sh
```

脚本首次运行会从固定提交下载并校验 `chi_sim+eng` Tesseract数据，缓存于Git忽略目录。两份N31手册共58页、607个标题/段落/列表/表格/警告结构块，9页经过OCR后待处理页为0；检索验证分别命中用户手册第14页和第20页。公开报告不含手册正文、页面图、私有索引或绝对路径。

三种 Web 演示模式：

```bash
bash scripts/run_demo_mode.sh live
bash scripts/run_demo_mode.sh preprocessed
bash scripts/run_demo_mode.sh offline
```

`live` 现场重算已审核结构化 Gold 的质检与局部修订，`preprocessed` 重跑本地多源预处理，`offline` 只读取仓库内不含任何原始素材的 Gold 演示包。三种模式都使用原生 Python，不要求 Docker。

三分钟路演材料和开场前自动验收：

```bash
bash scripts/check_pitch.sh
```

验收器检查180秒时间轴、Gold指标、五类Agent与工具追踪、四场景无来源内容门禁、高推理语义复核、选择性重建边界、交付配置、低码率预览映射、DGX报告、PPT、海报、培训视频、证据包和三种演示兜底。当前状态为 `READY_WITH_HUMAN_GATES`：自动检查通过，但完整观看、真人彩排、最终录屏、团队资格和官方规则五项门禁仍待参赛者确认。正式材料见 [三分钟路演脚本](./docs/三分钟路演脚本.md)、[现场演示与录屏操作单](./docs/现场演示与录屏操作单.md) 和 [8页路演PPT](./output/presentation/SkillForge_三分钟路演_v1.pptx)。

人工门禁不能由程序自动通过。完成某项人工工作后，使用私有确认器绑定当前运行单和证据哈希，不要手改路演JSON：

```bash
bash scripts/manage_human_gates.sh status

# 180秒真人彩排前初始化私有计时记录；填写并完成彩排后先做机器检查
bash scripts/check_final_rehearsal.sh --init
bash scripts/check_final_rehearsal.sh

# 示例：实际完整观看当前80秒成片后，才可执行
bash scripts/manage_human_gates.sh confirm \
  --gate TRAINING_VIDEO_FULL_WATCH \
  --reviewer "确认人姓名" \
  --evidence-file output/video/n31_training_video_v1.mp4 \
  --note "已完整观看并确认旁白节奏"
```

彩排模板和QA报告位于Git忽略的 `outputs/submission/`。机器检查要求7段顺序与冻结运行单一致、实际时间连续、总时长处于内部175–180秒目标内，并逐段确认讲解、操作、证明点和故障兜底；通过后仍是 `READY_FOR_HUMAN_CONFIRMATION`。`FINAL_STAGE_REHEARSAL` 只能绑定这份本地计时记录，记录、运行单或内部策略变化后旧确认自动失效。

确认记录写入Git忽略的 `outputs/submission/human_gate_confirmations.json`，目录权限700、文件权限600。记录包含确认人和私有证据定位，但状态与提交预检只输出门禁编号和汇总，不输出确认人、说明或证据路径。运行单、门禁文案或本地证据发生变化时确认立即失效；更新证据必须显式使用 `--replace`，撤销使用 `revoke`，运行单变化后使用 `reset-stale` 清空过期确认。

可复现运行时基准：

```bash
bash scripts/run_runtime_benchmark.sh local
# 在 DGX 的 ~/skillforge/app 中执行
bash scripts/run_runtime_benchmark.sh dgx
```

基准执行2次预热和20次测量，分别覆盖直接 Python Gold 闭环和 Web 现场重算。当前 DGX 报告中位数为37.626毫秒和44.800毫秒，基准进程高水位RSS为86,491,136字节。该数字只描述已审核结构化Gold的确定性质检、局部修订和输出，不包含原始视频、PDF、录音预处理，也不调用外部模型。报告见 [运行时评测说明](./output/evaluation/README.md)。

最终提交预检：

```bash
bash scripts/check_submission.sh
```

预检会运行全量测试并核对项目身份、9份说明文档、18项成果、Git工作树、跟踪文件边界、`.env`忽略与600权限、本地密钥值泄漏、成果绝对路径、私有彩排记录及人工确认有效性。报告和确认目录均由Git忽略；报告只写门禁汇总，不记录密钥值、确认人、说明、私有备注或证据路径。只有 `READY_FOR_SUBMISSION` 返回0；`NOT_READY`返回1，`DEVELOPMENT_CHECK`或 `READY_WITH_HUMAN_GATES` 返回2。开发中可显式使用 `--allow-dirty`，但不能得到正式提交结论。

受证据约束的高推理语义复核需要显式确认允许发送结构化Gold步骤和Evidence陈述；它不会发送原始媒体、完整转写、手册页面、本地路径或凭证：

```bash
bash scripts/run_n31_semantic_review.sh
```

语义报告只作为 `MODEL_INFERENCE`，不能自动覆盖Gold。当前冻结报告使用 `step-3.7-flash / high` 复核13步和36条Evidence陈述，13步均为 `SUPPORTED`，发现项0，自动Gold修改0。

从Revision Audit确定性生成选择性重建边界：

```bash
bash scripts/build_n31_selective_rebuild.sh
```

当前N31受控错误只失效S07–S13、Q02和V07–V13；其余6个步骤、4道题和8个视频镜头保持不变。A4海报是固定单页原子成果，步骤插入或顺序变化时整页重建。该阶段外部模型调用为0。

实际运行完整Gold产物流水线，或从指定阶段复用上游并重建下游：

```bash
.venv/bin/python scripts/run_n31_stage_pipeline.py
.venv/bin/python scripts/run_n31_stage_pipeline.py --rerun RENDERING
```

八个产物阶段分别是素材载入、证据抽取、SOP规划、错误草稿、首轮质检、局部修订、最终复检和培训成果渲染。每次运行写入独立的Git忽略目录，逐文件绑定SHA-256；阶段重跑只复用哈希未变化的上游，失败运行保留在隔离目录且不会切换当前发布指针。Web页面可执行同样的完整运行或指定阶段重跑。

Web Gold结果提供操作者审核台：步骤可安全重排、锁定、人工确认和单步确定性重建；任何重排都会校验前置依赖和已锁定位置。审核会话只写入Git忽略的 `outputs/sop_review_sessions/`，目录权限700、文件权限600。单步重建只返回该步的三种SOP视图、一个检查卡和相关测验题，不修改Gold、不调用外部模型。页面内的Evidence ID可以点击，返回PDF页码或音视频时间点；视频只链接已审核安全预览，原始来源URL固定为空。

Web同时提供冲突裁决会话：每个冲突记录自动决策、路由原因、Revision Audit变更路径、Evidence、人工决定和最终采用结果。确定性冲突可自动采用；`UNSUPPORTED_SAFETY_CLAIM`、`MISSING_EVIDENCE`、`INVALID_EVIDENCE` 和 `REVIEW` 动作无条件转人工，即使上游错误标记为 `automatic=true` 也不能绕过。人工拒绝后最终SOP不可发布，重新复核批准且复检无残留后才生成可发布摘要。会话同样只保存在Git忽略的700/600权限目录。

Web Gold结果还提供专家口述ASR快速修正：12个问题逐段展示原始ASR、受控术语校正基线、当前修正文本、录音时间和Evidence ID。每次保存都记录修正前后文本、操作者、原因和时间，并重新计算由Evidence陈述、时间点和当前校正文本共同绑定的SHA-256摘要；原始转写和历史不会被覆盖。会话只写入Git忽略的 `outputs/asr_correction_sessions/`，目录权限700、文件权限600，不调用外部模型；修正不能删除已审核必要术语，Gold或转写源变化后旧会话会拒绝继续写入。

从 Gold SOP 重新生成一页式 A4 培训海报：

```bash
.venv/bin/python -m skillforge.poster
```

海报输出为 `output/pdf/n31_a4_training_poster.pdf`，包含13步操作、条件步骤、异常处理、完成标准和隐私提示。

从 Gold SOP、分镜和6段 `LOCAL_QA_PASSED` 自摄安全视频重新生成80秒横屏培训视频：

```bash
bash scripts/run_n31_training_video.sh
```

成片输出为 `output/video/n31_training_video_v1.mp4`。同目录的生成清单记录视频与旁白哈希、技术参数、来源门禁、覆盖率和检查状态；独立证据包把15个镜头绑定到25条去重 Evidence 及其 PDF 页码、视频或录音时间点，不嵌入原始媒体。脚本还会从这份已审核成片生成13张640像素宽的手机清单安全预览及哈希清单，因此DGX离线模式无需携带私有派生帧。旁白只向 StepAudio TTS 发送371字文本，未发送视频、手册、录音或面单。当前成片已通过自动检查和AI辅助联系表复核，状态仍为 `READY_FOR_HUMAN_REVIEW`，参赛者完整观看确认前不得标记为最终批准。

从6段 `LOCAL_QA_PASSED` 自摄安全视频生成本机私有低码率预览：

```bash
bash scripts/build_n31_video_previews.sh
```

交付配置固定为新操作员、简体中文普通话、源视频3–8分钟、培训视频60–90秒且目标80秒，并要求SOP、检查清单、测验、A4海报、培训视频和修订审计六类输出。预览最大854×480、15 fps、H.264/AAC、总平均码率不超过600 kbps，保留源时间轴到预览时间轴的一一映射。实际MP4只写入Git忽略的700/600权限目录；仓库只保存不含媒体路径的结构化清单。训练视频渲染入口会直接校验这份配置，case、语言或目标时长不匹配时拒绝生成。

从现有真实成果重建五类Agent与工具执行追踪：

```bash
bash scripts/build_n31_agent_trace.sh
```

追踪固定为 Perception、SOP、Creator、Verifier、Revision 五类职责，登记PDF页、关键帧、视频/录音区间、检索、规划、保存、渲染、质检、修订和人工确认等13个实际工具，并记录14次调用和5次Agent交接。每次调用都绑定输入输出成果ID；19个成果逐项核对大小和SHA-256，明确保留 `Verifier → Revision → Verifier` 复检环。公开追踪不含媒体路径、绝对路径或凭证，生成追踪本身不调用外部模型。

## 当前目录

```text
skillforge/
├── README.md
├── .env.example
├── pyproject.toml
├── config/
│   └── models.json
├── schemas/
├── src/skillforge/
├── tests/
├── docs/
├── scripts/
├── cases/
│   ├── demo_case/
│   │   ├── synthetic/
│   │   ├── input/
│   │   ├── derived/
│   │   ├── gold/
│   │   └── output/
│   └── n31/
│       ├── materials/
│       ├── input/
│       ├── derived/
│       ├── gold/
│       ├── evaluations/
│       ├── demo_bundle/
│       └── output/
├── logs/
└── outputs/
```

## 最小可交付版本

P0 能力只有五项：

1. 从视频、PDF、口述中生成 8–15 步 SOP。
2. 每个关键步骤能回溯到 PDF 页码或视频时间点。
3. 能发现缺步骤、顺序错误和无来源内容。
4. 能根据证据局部修订，而不是整包重做。
5. 能在三分钟演示中稳定呈现修订前后对比。

多语言、数字人、全生成视频、多案例和复杂权限系统均不在 P0 范围内。

## 当前工程状态

- 三类核心 JSON Schema、模型路由、Step Plan 安全客户端、显式状态机和结构化脱敏日志已实现。
- 模拟案例能稳定发现缺步骤、错误顺序、无依据工具和无依据参数，并用证据完成局部修订。
- 模拟闭环的严重错误从 5 项降到 0 项，九个必要步骤和证据覆盖率均达到 100%。
- N31 三段隐私安全正式视频已生成；6段视频和两份手册已完成本地摄取，形成142条 Evidence 候选。
- 专家口述已完成ASR、术语校正和12段时间点绑定，Evidence Catalog扩充到154条。
- 已生成13步实际操作者审核的 Gold v1：10步必需、3步条件执行，全部标记 `VERIFIED`。
- Gold评测发现5个高严重度问题，经4项局部修订后降为0；必要步骤和证据覆盖均恢复到100%。
- Step 3.7 已对13个 Gold 步骤的安全关键帧序列完成严格视觉复核：12步部分可见、S04不可见、0步视觉矛盾；结果保留为模型推断，不覆盖手册和操作者事实。
- 已将51次已审核关键帧引用合并为19个同源候选窗口，覆盖13步和6段视频；12个窗口绑定41个去重DGX场景候选，范围严格限定为Gold步骤对齐候选，不宣称通用动作识别。
- 两份N31手册已完成58页结构化分块、9页中文OCR和页码保真检索验证，共607个私有检索块；公开报告只保存统计、输入哈希和命中页码。
- 多源消融显示手册单源覆盖8/10、专家口述单源覆盖9/10、至少两种来源联合覆盖10/10个必要步骤。
- Web Demo 已接入 `GOLD / FINAL`、多源对比、视觉复核和现场重跑，同时保留候选、模拟与无素材离线回退。
- Web 成果区直接展示简洁版、详细版和带证据版SOP，以及一步一屏的13项手机检查清单、排序/工具/风险/状态/错误判断5类证据测验和80秒培训视频；完成记录与问题反馈只保存在本机忽略目录，白名单外文件名返回404。
- 三分钟路演已冻结为7段连续时间轴；8页PPT、逐秒讲解词、现场/预处理/离线操作单和自动验收器已经生成并通过本机验证。
- 已从 Gold SOP 生成单页A4培训海报，150 dpi渲染检查无裁切、重叠、乱码或越界文字。
- 已从6段自摄安全派生视频重剪15镜头、80秒、1080p横屏培训成片；13/13 Gold步骤、10/10必要步骤和30次证据引用均通过程序校验，StepAudio旁白响度为-16.18 LUFS。
- DGX已用原生CUDA实际处理6段自摄安全派生视频的420帧，筛出50个场景候选时间点；Web展示GPU指标和5步Agent决策/工具轨迹，外部API仍未获准处理这些视频。
- 本机与DGX Python 3.12当前均通过231项自动测试；逐阶段耗时、CPU、进程峰值内存和输出字节统计已在两端复验。DGX真实完整运行重建8阶段，随后从 `RENDERING` 重跑时复用7阶段、只重建1阶段，资源字段和输出大小逐项一致。五类Agent/13工具/14调用/19产物哈希追踪、私有人工门禁确认器、最终彩排证据链、Web接口、安全下载和路演断言已在两端复验；默认确认保持0/5，未替参赛者关闭门禁。交付配置、6段低码率预览、源文件哈希/时间轴映射和Web Range播放也已完成两端验证。DGX回环Web还实测了ASR人工修正、Evidence摘要重绑、冲突裁决和安全人工门禁。18项路演成果、真实产物阶段重跑、传输容错、可恢复工作流检查点、离线包跨文件绑定、操作者审核台、高推理语义复核、选择性重建边界、视频证据包、连续动作候选窗口、PDF结构报告、路演PPT、运行时基准、用户服务和Web白名单均已复验。
- 赛事公开要求对齐审计未发现方向性偏离；2–5人团队资格和官方评分/提交/API细则仍待参赛者从报名材料或训练营讲义确认。
- DGX服务监听与进程重启后的回环访问已经验证；公网入口请求未到达应用，现场暂使用SSH端口转发、离线包和录屏兜底。
- 提交预检已自动覆盖代码、18项成果、文档和敏感边界；尚待培训视频最终人工观看、180秒彩排、有声录屏、团队资格和官方规则确认。无Gold引导的通用动作发现仍属于可选增强，不在当前P0能力声明内。

## 冻结的 P0 运行路线

P0 核心闭环直接在 DGX Spark 上使用 Python 虚拟环境和用户级 FFmpeg 运行，不依赖 Docker或GPU容器。场景候选筛选使用机器已有但未加入PATH的 CUDA 13 `nvcc` 编译小型、可审计的原生内核；即使该增强不可用，仓库内结构化Gold离线演示仍可运行。Docker仅在后续明确采用本地GPU模型或 NVIDIA VSS 时再启用，当前权限问题不阻塞主链路。
