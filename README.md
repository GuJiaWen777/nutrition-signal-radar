# 营养健康行业信号雷达

本仓库是「EarlyData电商大数据」公众号的自运营工作台（本机单人、独立 Git 仓库 `GuJiaWen777/nutrition-signal-radar`）：把营养健康产业公开信息变成可审阅、可发布的行业内容，对标庶正康讯等行业媒体。工作形态是 CLI `nutrition-radar` + 只监听 `127.0.0.1` 的看板；`var/` 为本地运行状态，不入库。

**当前主线是资讯自动线**（2026-08-31 确认，需求见 `docs/DSA自动化文章需求.docx`、边界见 [ADR-0014](docs/adr/0014-news-line-and-wechat-draft-publishing.md)）；行业信号线（周报/研究）为既有的次高频产物线。

## 两条内容线

| 线 | 输入 | 产物 | 发布 |
| --- | --- | --- | --- |
| **资讯自动线（主线）** | 人工确认的白名单直采信源（政府/交易所/品牌官网/展会/科研组织） | 《营养健康行业资讯｜XXXX》栏目文章（每期 3–5 条，周一周四成文） | 自动写入公众号**草稿箱**；定时发布在公众号后台人工操作；**群发永远禁止** |
| 行业信号线 | RedFox 监测公众号（发现源，待补证） | 行业信号卡、本周信号快报、可发布周报文章、深度研究稿 | 人工复制发布 |

## 当前状态（2026-11-14）

- 资讯线全链路已真实运行：36 个白名单源（**18 个已启用**，含浏览器渲染与按源代理策略），`fetch → select` 每日、`build` 周一/周四的调度节奏，跨期条目不重复（schema v17）。
- 成文门槛「≥3 条日报 / ≥2 条周报形态 / 不足跳过」兜底；同模板备案公示合并成简讯、薄料（<60 字）硬闸不进模型，政策类细分国家级 P0 / 省市实质 P1 / 省市备案 P3；封面由入库模板 + 本机 Edge 无头截图生成（PIL 兜底）；LLM 只按已抓取原文摘要，不补充事实。
- 微信草稿箱网关就绪（`news publish --live --confirm` 双重人工门禁，每稿限推一次；`item_count`/`no_forbidden_terms`/`sources_complete` 结构性 QC 未过时拒绝推送），首次真实推送待操作者演练。
- 行业信号线按需运转（想做深度选题时跑一轮 sync → 快报 → 研究，见 [WEEKLY_WORKFLOW](docs/WEEKLY_WORKFLOW.md)；开放周次已过期，恢复时先关闭再新建）。
- 测试基线：`pytest tests/` 全绿（222 项）。
- `doctor` 新增 `news_fetch` 检查（超过 48h 未抓取即 WARNING），`news list-sources` 与看板展示每个信源近 30 天「入选/已评估」有效率。
- 本机最近一次成功抓取记录为 2026-09-09（`news_sources.last_fetched_at`）；若要继续周一周四成文，请先确认任务计划程序仍在运行。

## 快速上手

```powershell
cd D:\Earlydata\nutrition-signal-radar
uv sync --extra dev
.\.venv\Scripts\nutrition-radar.exe doctor

# ── 资讯线（主线；真实外呼必须 --live）──
.\.venv\Scripts\nutrition-radar.exe news list-sources                # 白名单与健康状态
.\.venv\Scripts\nutrition-radar.exe news set-source-enabled --name "XX" --enable   # 人工确认后启用
.\.venv\Scripts\nutrition-radar.exe news fetch --live --lookback-hours 24          # 抓取已启用源
.\.venv\Scripts\nutrition-radar.exe news select                      # 确定性筛选分级（无 LLM）
.\.venv\Scripts\nutrition-radar.exe news list-items --selected-only  # 查看入选与落选理由
.\.venv\Scripts\nutrition-radar.exe news readiness                  # 今日成文就绪度（无网络、无 LLM）
.\.venv\Scripts\nutrition-radar.exe news build --dry-run            # 占位摘要骨架：条数/去重/标题，不调模型
.\.venv\Scripts\nutrition-radar.exe news build --live                # 摘要成文 + 本地产物
.\.venv\Scripts\nutrition-radar.exe news qc --slug news-YYYY-MM-DD --version 1   # 只读复核 QC 与发布门禁
.\.venv\Scripts\nutrition-radar.exe news diff --slug news-YYYY-MM-DD --from 1 --to 2   # 两版条目/摘要差异
.\.venv\Scripts\nutrition-radar.exe news publish --live --confirm --slug news-YYYY-MM-DD --version 1
# 本地阅读站（独立端口，直接展示成稿 HTML；只读，不进调度链）：
.\.venv\Scripts\nutrition-radar.exe news serve --port 8780 --open

# ── 行业信号线（按需：想做深度选题时跑一轮）──
.\.venv\Scripts\nutrition-radar.exe sync --live                      # 受控同步公众号文章
.\.venv\Scripts\nutrition-radar.exe weekly brief                     # 本周信号快报草稿
.\.venv\Scripts\nutrition-radar.exe weekly article generate --live   # 可发布周报文章（pi）

# ── 本地看板（行业信号线；资讯线视图待接入）──
.\.venv\Scripts\nutrition-radar.exe serve --port 8765 --open
```

无人值守调度见 `scripts/news_daily.ps1`（fetch→select 每日、build 仅周一/周四，永不含 publish），任务计划程序注册命令与日常操作见 [NEWS_WORKFLOW](docs/NEWS_WORKFLOW.md)。

## 文档地图

| 想了解什么 | 去哪看 |
| --- | --- |
| Agent 工作流、硬边界、术语优先级 | `AGENTS.md` |
| 资讯线日常操作、信源实调记录 | `docs/NEWS_WORKFLOW.md` |
| 周报人工节奏 | `docs/WEEKLY_WORKFLOW.md` |
| 资讯线需求原文 | `docs/DSA自动化文章需求.docx`（不入学库） |
| 资讯线边界与微信草稿箱 | [ADR-0014](docs/adr/0014-news-line-and-wechat-draft-publishing.md) |
| 可发布周报文章契约 | [ADR-0013](docs/adr/0013-publishable-weekly-article.md) |
| 全部架构决策（0001–0014，追加式） | `docs/adr/` |
| 术语与对象定义 | `CONTEXT.md` |
| 旧需求基线、切片流水账、来源清单草案 | `docs/archive/`（已归档，仅历史参考） |

## 硬边界（完整版见 AGENTS.md）

- 所有配置、源码、测试、数据库与运行产物留在本仓库；独立于历史 `SentimentTracker` / Weekly Publisher。
- 凭据只走 `.env`，不写入代码、SQLite、日志、文档或版本库。
- 群发（mass-send）路径永远禁止；资讯线自动化的上限是**草稿箱**，定时发布在公众号后台人工完成。
- 白名单首次确认、发布、研究启动、归档等人工门禁不可绕过；AI 分数只排序解释，不改变状态。
- 资讯线筛选与去重是确定性规则，LLM 只按已抓取原文摘要；不抓竞对公众号，不用 EarlyData 电商数据填充。
- `docs/archive/` 里的文档仅作历史参考，不再更新；现行需求以资讯线 docx 与 ADR-0014 为准。
