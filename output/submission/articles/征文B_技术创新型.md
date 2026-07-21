# SkillForge：五个Agent如何把"改作文"变成工业培训闭环

> 团队：星星之火 · NVIDIA DGX SPARK 黑客松 2026
> 项目：SkillForge（匠传）

## 项目背景

大模型写教程不难，难的是写出来的东西能不能信。步骤漏了怎么办？顺序反了怎么办？参数是模型编的怎么办？

SkillForge（匠传）的出发点不是"让AI生成更多内容"，而是让Agent具备自我质检和局部修订能力。输入专家操作视频、设备PDF手册和口述录音，输出带证据链的SOP、检查清单、测验、海报和培训视频。每一句结论都能追到是哪个视频第几秒、哪本手册第几页说的。

## SkillForge如何工作

五类Agent形成流水线：Perception提取候选证据，SOP Agent规划步骤依赖，Creator生成培训成品，Verifier逐条质检，Revision精准修改。当前公开追踪记录了五类Agent、13种工具和14次工具调用，Agent交接、输入输出哈希与Evidence绑定完整保留，见[Agent与工具调用报告](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/evaluations/agent_tool_trace_v1.json)。

核心设计约束：模型不能自己发明证据。Evidence ID、来源类型和时间定位由程序强制锁定，模型只能在已有证据边界内工作。必要工具、参数、警告和完成标准都必须落在当前步骤的Evidence范围内，否则Verifier会标记为无依据。

## 真实案例

主案例："汉印N31电子面单打印机更换标签纸、介质学习与试印验收"。真实设备、自摄安全视频、两份手册（共58页、607个页码绑定结构块）、实际操作者口述。最终13步Gold SOP，10步必需、3步条件执行，由操作者本人审核。

视频、手册和口述分别产生18、7、8条候选，经粗粒度拆分、细粒度合并、同义项归并和依赖图检查，收敛为13个有序步骤。12步至少两类来源印证，10步三类来源全覆盖。详见[PDF结构报告](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/evaluations/pdf_structure_v1.json)和[来源候选合成报告](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/evaluations/source_candidate_synthesis_v1.json)。

## Agent质检与局部修订

这是SkillForge最核心的技术差异。

受控错误注入后，Verifier检测到5类严重问题：缺步骤、前置依赖断裂、顺序错误、无依据参数、无依据工具。它不是报告"质量不好请重写"，而是精确定位到哪个步骤、违反哪条规则、引用哪条证据。Revision据此执行4项局部修订：插入、删除参数、删除工具、恢复顺序。严重问题5→0，覆盖率90%→100%。

选择性重建分析证明"局部"不是口号：冲突只影响7个步骤、1道题和7个视频镜头，其余内容原封不动。就像改作文只改病句，不是把整篇撕了重写。边界逐项记录在[选择性重建报告](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/evaluations/selective_rebuild_v1.json)中，修订审计见[修订审计](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/demo_bundle/revision_audit.json)。

## DGX Spark与技术实现

P0主链原生Python + FFmpeg，不依赖Docker。DGX Spark的NVIDIA GB10用CUDA 13原生内核处理6段视频、420帧、50个候选时间点，端到端13秒。计算范围严格限定为场景候选筛选——GPU说"这几帧值得看"，Agent结合手册和口述判断"看到了什么"。机器筛选是候选，不是结论。

DGX同时承载离线Gold闭环和回环地址Web演示。Step 3.7语义规划使用Step Plan API；原始视频、手册和录音不发送给外部模型。本地计算与外部推断的边界在[DGX视觉计算报告](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/evaluations/dgx_visual_compute_v1.json)中逐段记录。

## 可复现结果

多源消融：手册单源80%，口述单源90%，联合100%。单一材料永远不够，这正是多源证据链存在的理由。

DGX Python 3.12环境，直接Gold与Web重算各20次，共40轮全部成功，唯一P0语义指纹1个。不是碰巧对了，是稳定可复现。见[多源对比报告](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/evaluations/multisource_comparison_v1.json)和[DGX运行时基准](https://github.com/sparklefire/SkillForge/blob/main/output/evaluation/runtime_benchmark_dgx.json)。三种演示模式（现场重算、预处理结果、无素材离线包）确保答辩不翻车。

## 交付成果

同一份SOP自动生成13项手机检查清单、5题培训测验、A4海报和80秒培训视频（15镜头覆盖13/13步、25条去重Evidence）。视频清单见[培训视频清单](https://github.com/sparklefire/SkillForge/blob/main/output/video/n31_training_video_manifest_v1.json)。

## 边界与下一步

当前仍需人工审核，不是全自动决策系统。Gold由一名操作者审核；稀疏关键帧不足以单独证明完整动作；Step Plan API承担部分语义推断，本地大模型部署尚未作为P0。何老师参考代码只完成静态分析，以自有N31闭环为主。

下一步：扩展真实案例人工接受率评测，提升开放式动作候选发现能力，在不改变Evidence边界的前提下增强泛化性。

## 项目信息

- 项目名称：SkillForge（匠传）
- 团队：星星之火（武汉以清软件有限公司）
- 应用领域：制造业 / 物流培训
- 公开代码：[github.com/sparklefire/SkillForge](https://github.com/sparklefire/SkillForge)
- 核心定位：把专家操作视频、设备手册和口述经验，转化为可追溯、可验证、会自我修订的多模态培训包
