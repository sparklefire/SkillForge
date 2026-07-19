# 何老师参考代码投递区

活动方将跑通何老师参考代码列为基础完赛标准。原始文件统一放入：

`external/teacher_he_reference/drop/`

约定：

- 已收到 `workshop-Copy.ipynb` 和开幕PPTX，原始文件名、只读权限和目录结构保持不变。
- `drop/` 整体受 Git 忽略；在代码许可和赛事提交边界核实前，不把外部代码提交为 SkillForge 自有代码。
- 不要在参考代码中填入真实密钥；运行时使用隔离的 Python 虚拟环境和本地环境变量。
- 收到代码后先核对 README、许可证、依赖、启动入口、模型与 DGX 要求；只有参赛策略改为需要实跑时，才在隔离环境运行并保存不含凭证的复现记录。
- 参考代码的运行不得覆盖 SkillForge 现有 `.env`、环境、案例素材或输出。

下载链接中的跟踪参数不写入仓库；需要复核来源时使用活动方提供的原始消息。

当前安全审计结果保存在 `audit_v1.json`。执行：

```bash
bash scripts/check_teacher_he_reference.sh
```

返回 `WAITING_ON_RUNTIME_BUNDLE`、退出码2是当前预期状态：Notebook依赖培训环境预装的Ollama、Qwen3.6 35B、ComfyUI/FLUX/PuLID、OpenClaw、Node 22、控制脚本和示例图片，这些内容没有随两份文件提供。Notebook自带的历史输出只能证明官方上游环境曾跑通，不能替代在本项目DGX上的实际复现。参赛者已决定当前只保留这份静态分析，以SkillForge真实N31项目为主；该退出码不进入每日主线或正式提交预检。
