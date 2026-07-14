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

## 当前目录

```text
skillforge/
├── README.md
├── .env.example
├── config/
│   └── models.json
├── docs/
├── scripts/
├── cases/
│   └── demo_case/
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
