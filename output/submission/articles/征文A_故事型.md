# 老师傅走了，经验怎么留下来？——SkillForge的Agent培训闭环

> 团队：星星之火 · NVIDIA DGX SPARK 黑客松 2026
> 项目：SkillForge（匠传）

## 项目背景

仓库新来了一位小伙子，任务是给汉印N31面单打印机换纸。他看了二十秒短视频，觉得"就这？"结果导纸夹没调、介质学习没做、试印没验收——打出来一沨全是歪的。旁边老师傅叹了口气："这还用教？"

问题恰恰在这里：老师傅觉得不用教的东西，新手根本不知道存在。经验散落在视频里、手册里、嘴上随口说的里，人一走，链条就断了。

SkillForge（匠传）做的事很明确：把专家操作视频、设备PDF手册和口述录音，自动转化为可追溯、可验证、会自我修订的多模态培训包。不是"让AI写一篇教程"，而是让Agent完成"发现问题→展示证据→局部修订→再次验证"的完整闭环。

## SkillForge如何工作

系统由五类Agent协作：Perception负责"看"——从视频、手册、录音中提取证据；SOP Agent负责"想"——规划步骤和依赖；Creator负责"写"——生成SOP、清单、测验；Verifier负责"查"——逐条质检；Revision负责"改"——只动出错的地方。

当前公开追踪记录了五类Agent、13种工具和14次工具调用，完整交接和输入输出哈希可在[Agent与工具调用报告](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/evaluations/agent_tool_trace_v1.json)中复核。

## 真实案例

主案例是"汉印N31电子面单打印机更换标签纸、介质学习与试印验收"。输入来自真实设备、自摄安全视频、两份设备手册和实际操作者口述。最终形成13步Gold SOP，其中10步必需、3步按条件执行，由实际操作者审核确认。

两份手册共58页，形成607个页码绑定结构块；视频、手册和口述分别产生18条、7条和8条候选，经粗粒度拆分、细粒度合并、同义项归并和依赖图检查，得到13个有序步骤。12步至少有两类来源，10步覆盖全部三类来源。详见[PDF结构报告](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/evaluations/pdf_structure_v1.json)和[来源候选合成报告](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/evaluations/source_candidate_synthesis_v1.json)。

## Agent质检与局部修订

我们在Gold副本中注入受控错误：缺少介质学习、前置依赖断裂、步骤顺序错误、无依据参数和无依据工具。Verifier引用当前步骤证据后，检测到5项严重问题；Revision执行4项局部修订——插入缺失步骤、删除无依据参数、删除无依据工具、恢复正确顺序。严重问题从5降至0，必要步骤覆盖和证据覆盖都从90%提升到100%。

关键是"局部"：选择性重建分析显示，这组冲突只影响7个步骤、1道题和7个视频镜头，无关内容保持不变。不是整包重写，是精准手术。结果见[闭环评测摘要](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/demo_bundle/summary.json)和[选择性重建报告](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/evaluations/selective_rebuild_v1.json)。

## DGX Spark与技术实现

P0主链采用原生Python与FFmpeg，不依赖Docker。DGX Spark上的NVIDIA GB10使用CUDA 13原生内核处理6段安全派生视频、420帧、50个候选时间点，端到端13秒。GPU只负责场景候选筛选，不把亮度或边缘分数写成操作事实。

DGX还承载完整离线Gold闭环和只监听回环地址的Web演示服务。Step 3.7语义规划使用Step Plan API；原始视频、手册页面和完整录音不发送给外部模型。设备信息和逐段统计见[DGX视觉计算报告](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/evaluations/dgx_visual_compute_v1.json)。

## 可复现结果

多源消融显示：手册单源覆盖80%，专家口述单源覆盖90%，至少两类来源联合覆盖100%。老师傅嘴上那些"顺便说一句"，真的是手册里写不到的。

在DGX Python 3.12环境中，直接Gold与Web现场重算各20次，共40轮全部成功且唯一P0语义指纹为1个。详情见[多源对比报告](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/evaluations/multisource_comparison_v1.json)和[DGX运行时基准](https://github.com/sparklefire/SkillForge/blob/main/output/evaluation/runtime_benchmark_dgx.json)。项目提供现场重算、预处理结果和无素材离线包三种演示模式。

## 交付成果

同一份证据约束SOP继续生成13项手机检查清单、5题培训测验、一页A4海报和80秒培训视频。视频包含15个镜头覆盖13/13个Gold步骤，绑定25条去重Evidence。时长、编码、覆盖率和来源策略记录在[培训视频清单](https://github.com/sparklefire/SkillForge/blob/main/output/video/n31_training_video_manifest_v1.json)中。

## 边界与下一步

SkillForge当前仍需人工审核，不是全自动工业决策系统。Gold由一名实际操作者审核，第二名领域复核属于后续增强；稀疏关键帧不足以单独证明完整动作；Step Plan API承担部分语义推断，本地大模型部署尚未作为P0能力。何老师参考代码只完成静态分析，当前项目以自有N31闭环为主。

下一步会扩展真实案例人工接受率评测，并在不改变Evidence边界的前提下提升开放式动作候选发现能力。

## 项目信息

- 项目名称：SkillForge（匠传）
- 团队：星星之火（武汉以清软件有限公司）
- 应用领域：制造业 / 物流培训
- 公开代码：[github.com/sparklefire/SkillForge](https://github.com/sparklefire/SkillForge)
- 核心定位：把专家操作视频、设备手册和口述经验，转化为可追溯、可验证、会自我修订的多模态培训包
