# 模拟案例说明

本目录只包含为自动化测试编写的虚构数据，不对应真实设备、真实手册或真实安全程序，不能用于现场操作。

案例故意向一份九步“更换演示过滤件”SOP 注入四类错误：

1. 删除断电确认步骤。
2. 交换佩戴防护用品与打开盖板的顺序。
3. 添加无来源工具“扭矩扳手”。
4. 添加无来源参数“12 N·m”。

其用途仅为验证“发现问题 → 展示证据 → 自动局部修订 → 前后对比”的工程闭环。真实案例接入后必须替换本目录的数据，并由领域审核者确认。

`discovery_evidence.json` 与 `discovery_response_fixture.json` 是另一条独立工程回归：
模型只看到九条虚构 Evidence，不接收 Gold SOP、标准步骤文本或 semantic key；输出永远停在
`NEEDS_REVIEW / HUMAN_REVIEW_REQUIRED`，不能覆盖 Gold 或直接发布。运行：

```bash
bash scripts/run_safe_step_discovery.sh
```
