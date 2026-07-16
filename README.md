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
- [详细任务拆解](./docs/SkillForge任务拆解.md)
- [环境与接入说明](./docs/环境与接入.md)
- [执行状态](./docs/执行状态.md)
- [模型路由配置](./config/models.json)

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

两个脚本都从本地 `.env` 读取配置，不输出密钥。

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

Web 默认监听 `0.0.0.0:7860`。页面包含上传预处理、质检问题与证据、修订前后对比和局部修订审计。ASR 默认关闭；只有同时勾选外部处理授权时，规范化音频才会发送给 StepAudio。

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

两种模式都不会调用外部模型。默认模式复用已通过检查的安全成片，依次完成8来源摄取和142条本地 Evidence Catalog；存在已审核 Gold 时运行 `GOLD / FINAL` 最终评测，否则运行13步候选 SOP 的 `NOT_GOLD / PROVISIONAL_ONLY` 彩排；若 N31 预处理输出不存在，才回退到无版权模拟案例。

当仓库中存在 `cases/n31/gold/gold_sop.json` 时，`run_n31_local.sh` 会自动改用实际操作者审核的 Gold 约束，页面显示 `GOLD / FINAL`。重新执行专家录音ASR、术语核对、Gold固化和最终评测使用：

```bash
bash scripts/run_n31_expert.sh
```

该命令会把规范化录音发送给 StepAudio；原始录音和中间输出均处于 Git 忽略目录。

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
- Web Demo 已接入 `GOLD / FINAL` 真实结果并保留候选与模拟回退。
- 尚待增强真实视频的连续动作语义合并、视觉复核、DGX N31素材部署和现场录屏兜底。

## 冻结的 P0 运行路线

P0 直接在 DGX Spark 上使用 Python 虚拟环境和用户级 FFmpeg 运行，不依赖 Docker、GPU 容器或 `nvcc`。Docker 仅在后续明确采用本地 GPU 模型或 NVIDIA VSS 时再启用，当前权限问题不阻塞主链路。
