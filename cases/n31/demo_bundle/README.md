# N31 离线 Web 演示包

本目录用于现场网络或预处理输出不可用时的无素材兜底。JSON只包含Gold闭环的步骤、冲突、局部修订、指标、检查清单、测验和四场景无来源内容门禁，不包含原始视频、关键帧、录音、PDF、真实面单或凭证。

重新生成：

```bash
.venv/bin/python scripts/build_n31_demo_bundle.py
```

启动离线模式：

```bash
bash scripts/run_demo_mode.sh offline
```

现场重算模式使用 `live`，本地完整预处理模式使用 `preprocessed`。实际录屏文件仍需在最终界面和讲解词冻结后录制。
