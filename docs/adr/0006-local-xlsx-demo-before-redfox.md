---
status: accepted
date: 2026-08-07
---

# 先用只读本地 Excel 构建 Demo，再接入 RedFox

## 背景

RedFox 和 DeepSeek 密钥尚未配置，但仓库根目录已经有一份包含公众号标题、账号、日期、链接、互动指标和正文的真实 P&B 工作簿。等待外部接口会阻塞主题聚合和看板的产品验证。

## 决定

- 第一版先只读使用 `D:/Earlydata/SentimentTracker/data/input/P&B微信公众号3月数据.xlsx` 构建 Demo。
- 原工作簿不移动、不修改；所有数据库和派生产物只写入扩展自己的 `var/`。
- Demo 使用 `var/demo/radar.sqlite3`，未来 RedFox 正式数据使用 `var/radar.sqlite3`，两者不混用。
- Excel 和 RedFox 都必须输出相同的来源中立文章契约；去重、聚类、行业信号和选题看板只依赖该契约。
- 当前 P&B 数据可用于验证机制，但不能替代营养健康目标公众号的覆盖率和内容质量验收。

## 结果

团队可以在没有外部密钥时先验证完整操作体验，未来接入 RedFox 时不重写下游流程。代价是 Demo 的行业内容与最终营养健康服务范围并不完全一致，因此界面和文档必须持续标明“Demo 数据”，正式上线前仍需完成六个目标公众号的 RedFox 验证。
