# 从一沓歪面单到100%覆盖：SkillForge如何让培训不再靠"师傅带"

> 团队：星星之火 · NVIDIA DGX SPARK 黑客松 2026
> 项目：SkillForge（匠传）

## 项目背景

物流仓库里，新员工第一次给面单打印机换纸，打出来一沓全是歪的。不是他笨，是没人告诉他"导纸夹要调到多宽""换完纸要做介质学习""试印要看对齐线"。老师傅觉得"这还用说？"，新手觉得"这还用学？"——知识断层就藏在这种"谁都觉得不需要解释"的缝隙里。

制造业和物流现场，这类经验断层每天都在发生。设备手册写了，但没人看；师傅说了，但没录下来；录了视频，但新手不知道哪一秒是关键动作。SkillForge（匠传）要解决的不是"写一份教程"的效率问题，而是"培训知识能不能被相信"的信任问题。

## SkillForge如何工作

输入三样东西：专家操作视频（精确到第几秒）、设备PDF手册（精确到第几页）、专家口述录音（精确到哪个时间段）。输出五样东西：结构化SOP、手机检查清单、培训测验、A4海报和60-90秒培训视频。

中间跑的是五类Agent流水线：Perception提取证据，SOP Agent规划步骤，Creator生成成品，Verifier逐条质检，Revision精准修改。当前追踪记录了五类Agent、13种工具和14次工具调用，见[Agent与工具调用报告](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/evaluations/agent_tool_trace_v1.json)。

关键约束：模型不能自己编证据。每个步骤的工具、参数、警告和完成标准都必须有对应的PDF页码、视频时间点或录音时间段。没有来源的内容会被Verifier拦截，不会出现在最终培训包里。

## 真实案例

主案例是"汉印N31电子面单打印机更换标签纸、介质学习与试印验收"——就是开头那个"换纸打出歪面单"的场景。

真实设备、自摄安全视频、两份设备手册（共58页、607个页码绑定结构块、9页本地OCR）、实际操作者口述。最终形成13步Gold SOP，10步必需、3步按条件执行，由操作者本人审核确认。

视频、手册和口述分别产生18、7、8条候选，经过合并归并和依赖图检查，收敛为13个有序步骤。12步至少两类来源印证，10步三类来源全覆盖。详见[PDF结构报告](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/evaluations/pdf_structure_v1.json)和[来源候选合成报告](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/evaluations/source_candidate_synthesis_v1.json)。

## Agent质检与局部修订

培训包最怕的不是"写得不好"，而是"看起来对但实际有错"。SkillForge的Verifier专门干这件事。

受控测试中注入5类严重问题：缺少介质学习步骤、前置依赖断裂、步骤顺序错误、无依据参数、无依据工具。Verifier逐条引用证据定位问题，Revision执行4项局部修订：插入缺失步骤、删除无依据参数、删除无依据工具、恢复正确顺序。结果：严重问题5→0，必要步骤覆盖90%→100%，证据覆盖90%→100%。

"局部"意味着：只改了4个地方，其他内容一个字没动。选择性重建分析确认冲突只影响7个步骤、1道题和7个视频镜头。对培训管理者来说，这意味着修订不会引入新错误，审核成本可控。见[闭环评测摘要](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/demo_bundle/summary.json)和[选择性重建报告](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/evaluations/selective_rebuild_v1.json)。

## DGX Spark与技术实现

为什么需要DGX Spark？因为操作视频是敏感材料。工厂产线、医院手术间、物流仓库的操作画面，不能随便上传到云端API。

P0主链原生Python + FFmpeg，不依赖Docker。DGX Spark的NVIDIA GB10用CUDA 13原生内核在本地处理6段视频、420帧、50个候选时间点，端到端13秒。视频不出门，计算在本地完成。GPU只负责"哪些画面值得看"，语义判断由Agent结合手册和口述完成。

DGX同时承载离线Gold闭环和回环地址Web演示。Step 3.7语义规划使用Step Plan API；原始视频、手册和录音不发送给外部模型。见[DGX视觉计算报告](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/evaluations/dgx_visual_compute_v1.json)。

## 可复现结果

一个有意思的发现：只给手册，步骤覆盖80%；只给师傅口述，覆盖90%；两样合在一起，才到100%。老师傅嘴上那些"顺便说一句"，真的是手册里写不到的。这解释了为什么单一材料永远做不出完整培训包。

DGX环境40轮复现测试全部成功，唯一语义指纹1个。不是碰巧对了，是稳定可复现。见[多源对比报告](https://github.com/sparklefire/SkillForge/blob/main/cases/n31/evaluations/multisource_comparison_v1.json)和[DGX运行时基准](https://github.com/sparklefire/SkillForge/blob/main/output/evaluation/runtime_benchmark_dgx.json)。三种演示模式确保任何网络条件下都能完成答辩展示。

## 交付成果

同一份证据约束SOP自动生成13项手机检查清单、5题培训测验、A4海报和80秒培训视频（15镜头、13/13步覆盖、25条去重Evidence）。新员工拿着手机清单对着打印机走一遍，就能完成以前需要师傅在旁边盯着才能做对的操作。见[培训视频清单](https://github.com/sparklefire/SkillForge/blob/main/output/video/n31_training_video_manifest_v1.json)。

## 边界与下一步

SkillForge当前仍需人工审核，不是全自动决策系统。Gold由一名操作者审核，第二名领域复核属于后续增强；稀疏关键帧不足以单独证明完整动作；Step Plan API承担部分语义推断，本地大模型部署尚未作为P0。何老师参考代码只完成静态分析，以自有N31闭环为主。

下一步：扩展更多真实设备案例的人工接受率评测，验证"换一台设备、换一个行业"的泛化能力；在不改变Evidence边界的前提下提升开放式动作候选发现能力。

## 项目信息

- 项目名称：SkillForge（匠传）
- 团队：星星之火（武汉以清软件有限公司）
- 应用领域：制造业 / 物流培训
- 公开代码：[github.com/sparklefire/SkillForge](https://github.com/sparklefire/SkillForge)
- 核心定位：把专家操作视频、设备手册和口述经验，转化为可追溯、可验证、会自我修订的多模态培训包
