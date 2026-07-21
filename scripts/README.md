# 脚本索引

所有脚本在项目根目录执行，例如 `bash scripts/check_dgx.sh`。
Shell 脚本自动加载项目 `.env`（不打印密钥）；Python 脚本需要 `.venv` 已安装。

---

## 环境搭建

| 脚本 | 说明 |
|---|---|
| `setup_native.sh` | 一键创建 `.venv`、安装依赖、安装项目包 |
| `setup_ocr_languages.sh` | 下载 Tesseract 中文 OCR 语言包到本地缓存 |
| `check_dgx.sh` | 检查 DGX Spark SSH、GPU、FFmpeg、项目目录 |
| `verify_step_plan.py` | 最小请求验证 Step Plan 密钥连通性 |
| `check_native.sh` | 原生环境完整性检查（依赖、Schema、测试） |

## 启动与演示

| 脚本 | 说明 |
|---|---|
| `start_native.sh` | 启动 Web Demo（FastAPI，默认 `0.0.0.0:7860`），启动时提示本机访问地址 |
| `run_demo_mode.sh` | 三种演示模式：`live` / `preprocessed` / `offline`（默认），启动时提示所选模式 |
| `run_n31_local.sh` | N31 本地预处理模式重跑（不调用外部 API），输出分阶段进度 |
| `dgx_demo_tunnel.sh` | SSH 隧道映射 DGX 演示到本机 `127.0.0.1:17860` |
| `manage_dgx_demo_service.sh` | DGX 用户级 systemd 服务：`install` / `restart-test` / `status` |

## N31 案例构建

| 脚本 | 说明 |
|---|---|
| `process_n31_videos.py` | 本地 FFmpeg 视频隐私处理（遮挡 + 响度规范化） |
| `run_n31_expert.sh` | 专家口述 ASR、术语校正和时间点绑定 |
| `run_n31_visual.sh` | Step 3.7 关键帧视觉理解 |
| `run_n31_dgx_visual.sh` | DGX GPU 场景候选筛选（CUDA 原生） |
| `run_n31_training_video.sh` | 从 Gold SOP 生成 80 秒培训视频 |
| `run_n31_semantic_review.sh` | 语义质检（模型复核） |
| `run_n31_stage_pipeline.py` | Gold 工件流水线（全量或单阶段重建） |
| `run_safe_step_discovery.sh` | 无 Gold 的结构化步骤发现实验 |
| `build_n31_agent_trace.sh` | 生成 Agent 工具追踪工件 |
| `build_n31_grounding_gate.sh` | 无来源内容拒绝门禁测试 |
| `build_n31_pdf_structure.sh` | 手册结构化分块和页码检索验证 |
| `build_n31_selective_rebuild.sh` | 选择性重建报告 |
| `build_n31_source_candidates.sh` | 三来源候选合并和置信度 |
| `build_n31_temporal_windows.sh` | 连续动作候选窗口 |
| `build_n31_video_previews.sh` | 低码率安全预览视频 |
| `build_checklist_thumbnails.py` | 从培训成片提取检查清单缩略图 |
| `build_n31_demo_bundle.py` | 构建离线演示包（无素材、可入 Git） |
| `generate_n31_test_label.py` | 生成隐私安全测试面单 PDF |
| `generate_synthetic_assets.py` | 生成模拟视频/PDF/录音（闭环验证用） |

## 提交与审核

| 脚本 | 说明 |
|---|---|
| `check_submission.sh` | 提交预检（代码、成果、文档、敏感边界） |
| `check_submission_article.sh` | 征文合规检查（禁用词、必要标题、字数） |
| `check_submission_closeout.sh` | 11 阶段收尾链检查 |
| `check_submission_form_packet.sh` | 官方表单材料包完整性 |
| `check_submission_receipt.sh` | 提交回执验证 |
| `check_team_roster.sh` | 团队资格和成员信息 |
| `check_official_rules_review.sh` | 官方规则 6 项确认 |
| `check_final_recording.sh` | 最终录屏技术检查（分辨率、编码、时长） |
| `check_final_recording_review.sh` | 录屏人工观看审核门禁 |
| `check_final_rehearsal.sh` | 180 秒彩排验收 |
| `check_training_video_review.sh` | 培训视频人工观看审核 |
| `check_pitch.sh` | 路演 PPT + 口述稿 + 时间轴验收 |
| `check_demo_mode_parity.sh` | 三种演示模式输出一致性 |
| `check_publication_links.sh` | 公开链接可达性 |
| `check_public_checkout.sh` | 公开仓库纯净检出验证 |
| `check_project_board.sh` | 项目看板状态 |
| `check_teacher_he_reference.sh` | 何老师参考代码静态审计 |
| `run_guided_human_review.sh` | 引导式人工审核（培训视频/录屏/彩排） |
| `manage_human_gates.sh` | 人工门禁状态查看和管理 |
| `build_final_recording_candidate.sh` | 构建录屏候选 |
| `build_public_release_bundle.sh` | 公开发布包 |
| `build_release_manifest.sh` | 发布清单和人工门禁绑定 |
| `run_runtime_benchmark.sh` | 运行时基准测试（本地/DGX） |

## 常用组合

```bash
# 首次搭建
bash scripts/setup_native.sh
python3 scripts/verify_step_plan.py

# 离线演示（最稳定）
bash scripts/run_demo_mode.sh offline

# 现场演示（调用模型）
bash scripts/run_demo_mode.sh live

# DGX 远程演示
bash scripts/dgx_demo_tunnel.sh --smoke   # 先冒烟测试
bash scripts/dgx_demo_tunnel.sh           # 建立隧道

# 提交前全套检查
bash scripts/check_submission.sh
bash scripts/check_submission_closeout.sh
```
