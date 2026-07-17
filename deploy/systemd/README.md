# DGX 用户级演示服务

该服务只监听 DGX 回环地址 `127.0.0.1:7860`，使用仓库内不含原始素材和凭证的离线 Gold 演示包。它不开放公网端口、不需要 root、不依赖 Docker，也不会读取本地 `.env` 中的 Step Plan 密钥。

在 DGX 项目目录执行：

```bash
bash scripts/manage_dgx_demo_service.sh install
bash scripts/manage_dgx_demo_service.sh verify
bash scripts/manage_dgx_demo_service.sh restart-test
```

在开发机项目目录建立前台 SSH 隧道：

```bash
bash scripts/dgx_demo_tunnel.sh
```

浏览器访问 `http://127.0.0.1:17860`。关闭命令后隧道立即结束，DGX 服务仍只监听回环地址。

自动烟测：

```bash
bash scripts/dgx_demo_tunnel.sh --smoke
```

卸载用户服务：

```bash
bash scripts/manage_dgx_demo_service.sh uninstall
```
