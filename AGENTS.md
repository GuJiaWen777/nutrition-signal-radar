# Agent Workflow

这些指令适用于整个 `nutrition-signal-radar` 仓库。下次动手前先读本文件，再按「深入文档」打开对应材料。

## 这是什么项目

本仓库是独立的「营养健康行业信号雷达」：把营养健康产业公开信息变成可审阅的行业信号、选题和高价值文章，并运营自有公众号「EarlyData电商大数据」（对标庶正康讯等行业媒体，用可持续发布的行业内容获取关注与点击）。

它从历史 EarlyData 周报实践派生，但是独立 Git 仓库（`GuJiaWen777/nutrition-signal-radar`），不共享 `SentimentTracker` / Weekly Publisher 的代码、运行库、数据库或密钥。

工作形态是**本机单人工作台**：CLI `nutrition-radar` + 只监听 `127.0.0.1` 的看板。`var/` 是本地运行状态，不入库。

两条已确认的内容线，输入与产物都不同，禁止混用：

| 线 | 输入 | 产物 | 发布 |
| --- | --- | --- | --- |
| 行业信号线 | RedFox 监测公众号（发现源，待补证） | 行业信号卡、选题包、本周信号快报、可发布周报文章、深度研究稿 | 人工复制发布 |
| 资讯自动线（Phase 4） | 人工确认的白名单直采信源（政府/交易所/品牌官网/展会主办方/科研组织） | 《营养健康行业资讯｜XXXX》栏目文章 | 可写入公众号草稿箱；群发禁止 |

资讯线需求原文：`docs/DSA自动化文章需求.docx`。边界见 [ADR-0014](docs/adr/0014-news-line-and-wechat-draft-publishing.md)。

**运营节奏分级（2026-09-03 确认）**：每周固定运转的只有资讯自动线（周一/周四成文）。行业信号线按需运转——想从行业媒体热点找深度选题时才跑一轮（sync → 快报 → 选题/研究）；可发布周报文章降为可选（深度研究可替代其大部分价值），不再是每周义务。信号线基础设施保持可用，闲置零成本；两者输入不同（公众号监测 vs 官方直采），产物不重复。

## 动手前必读

实施类任务先读：`README.md`（现状与命令）、`CONTEXT.md`（术语）。

改资讯线再加：`docs/DSA自动化文章需求.docx`、`docs/adr/0014-news-line-and-wechat-draft-publishing.md`、`docs/NEWS_WORKFLOW.md`。

现行需求以资讯线 docx 与 ADR-0014 为准。旧「行业信号线」需求基线与切片流水账已归档到 `docs/archive/`（DESIGN_DRAFT / IMPLEMENTATION_PLAN / SOURCE_REGISTRY_DRAFT），仅作历史参考，不再更新。

术语以 `CONTEXT.md` 为准。不要把公众号监测叫「舆情分析」，不要把资讯线当行业信号输入，不要把「本周信号快报」和「可发布周报文章」当成同一产物。

## 硬边界

- 所有新配置、源码、测试、数据库、运行产物和命令都留在本仓库。
- 本仓库独立于历史 `SentimentTracker` / Weekly Publisher。不要编辑它、写入它或复用其运行状态。只读参考其微信公众号集成为 2026-08-31 明确批准；超出只读仍需新决策。
- `docs/DSA自动化文章需求.docx` 的微信接入已批准：生成的资讯文章可以进入草稿箱，并按接口权限定时发布。**群发（mass-send）路径始终禁止。** 周报、深度研究等既有产物仍由人工复制发布。
- 不要把凭据写入代码、SQLite、日志、夹具、文档或版本库。凭据只走本仓库 `.env`。
- 把 DeepSeek 当作可配置的 OpenAI 兼容分析后端，不要当成已经具备公网检索；联网检索只走显式授权的 Agent 路径。
- 测试默认 mock RedFox 和 Agent。真实外呼只通过显式 `--live`（以及发布时的 `--confirm`），且凭据已配置。
- 保留人工门禁。AI 分数可以排序和解释，但不得自动归档、批准或启动未确认的研究。唯一自动发布路径是上面批准的微信草稿箱/定时发布；**白名单首次确认仍必须人工决定。**
- 资讯线：LLM 不参与采集与筛选；筛选、P0–P3 与拦截用确定性规则。模型只根据已抓取原文摘要，不得补充未出现的事实、数据或金额。
- 资讯线不使用 EarlyData 电商数据，不抓取无法核验的自媒体/营销软文，不把庶正康讯等竞对公众号当聚合源（对标对象，不是搬运对象）。
- 把 `var/` 当本地运行状态，保持不入库。
- 切片顺序见 `docs/archive/IMPLEMENTATION_PLAN.md`（历史记录）；新切片先在对话中确认范围再动手。
- 任何未来的共享模块抽取、跨项目状态复用或仓库外文件编辑，都需要一次新的明确决策。

## 资讯成文：条数目标与当前缺口

需求文档第五节：**单篇收录 3–5 条**，全文 1,000–1,800 字；「不追求信息越多越好，而要让读者 3 分钟内了解重点」。不足 2 条不强行日更，可转「本周营养健康行业资讯」。

成文实现（2026-09-01 起，2026-09-06 收紧）：

- 日报门槛 3 条、上限 5 条；当天 24h 去重、合并、硬闸后不足 5 条时，一律从 7 日已入选池按 P0→P3 补位到单篇上限；补后仍不足 3 条才走周报形态。
- 当天 0 条，或补位后仍不足 3 条：改走周报形态（至少 2 条）；不足 2 条跳过。
- 同事件去重：标题近似（含日期/栏目前缀）或同一主机下 6 位以上 CMS 文章编号（`/notice/182630200.html` 与 `/acadconfg/182630200.html` 算一条）。筛选层拒绝重复入选，成文层再挡一次，避免历史已入选的双链接进同一篇。
- 成文层确定性闸门（2026-09-06）：同源同日的同模板备案/公示合并为一条简讯（「……备案信息公示（N 家企业）」，逐成员记账跨期排除）；快照不足 60 字或仅有标题的条目记 `thin_input` 剔除，不进摘要模型。
- 优先级细分（2026-09-06）：政策监管类不再一律 P0——国家级信源/标题 P0，省市实质性监管动作 P1，省市常规备案/登记公示降 P3。规则收紧后用 `news revalidate` 回溯清洗已入选存量（只降不升、不复活被拒条目）。
- 不要把 2 条近重复当成合格日报。

仍未做、且不要用低质量填充凑数的方向：

- 供给侧：36 个白名单信源里 18 个已启用；其余卡在 JS 列表、反爬/TLS、缺真实栏目页或未实现的交易所 API。官方源本来就不是每天都发稿。
- 筛选词表可按落选理由码实调，但不要让 LLM 参与筛选。
- 需求里的「每周一补抓 7 天遗漏 / Exa 兜底 / 高优先级源 2–4 小时加密抓取 / pi 单条正文补全」仍是二期。

改条数问题时，按下面顺序评估，不要用低质量填充凑数。

### 允许的修复方向（优先顺序）

1. **成文侧补齐到 3–5（最快、不改信源哲学）**  
   当日 24h 入选不足 3 条时，用已入选的 7 日窗口按 P0→P1→P2 补位，去重后凑满 3–5 条再成文；仍不足 3 条才走现有「周报形态 / 跳过」。不要把 2 条近重复当成合格日报。QC 的 3–5 条应从「记录失败仍发布」改为「不足 3 条不日更」。

2. **近重复与同事件去重**  
   现有标题 0.92 近似去重挡不住「同一通知、不同栏目 URL」。需要 canonical URL / 标题归一后再比，避免一篇文章里出现两条同一会议。去重只对已入选条目生效，不要误伤不同事件。

3. **把已启用源真正抓满**  
   对照 `docs/NEWS_WORKFLOW.md` 的 `parser_ok` 清单：为还在首页空转的源改到真实静态列表、RSS 或开放接口；列表页缺正文时用 pi `fetch_content` 做**单条**补全（不得改成批量联网写作）。启用策略不变：只有复验可解析的源才能 `set-source-enabled --enable`。

4. **筛选词表实调，而不是放宽到「有字就过」**  
   `INDUSTRY_TERMS` / `ACTION_TERMS` 可按落选理由码（`not_relevant`、`no_action_info`、`missing_body`）微调。先看 `news list-items` 和 `source-table.csv`，再改词表。不要让 LLM 参与筛选。

5. **扩大合格供给（中期）**  
   补齐 SEC EDGAR、港交所、剩余省市局与品牌栏目页；落实需求已写明的周一 7 天补捞（Exa 结果必须过滤回白名单域名）；高优先级政策/披露源 2–4 小时抓取。新增信源仍须人工确认后才启用。

### 禁止的捷径

- 不要为了凑条数去抓庶正康讯等行业媒体，或把公众号监测材料灌进资讯线。
- 不要用 EarlyData GMV、排行、商品数据填充。
- 不要让模型「根据常识补两条」。
- 不要把征求意见稿、预测、无日期/无链接条目放进正文。
- 不要把 `_TARGET_ITEMS` 提高到 5 以上来追求信息量；需求明确反对「越多越好」。
- 不要在调度脚本里加入 `news publish`。

## 常用命令（本仓库内）

```powershell
cd D:\Earlydata\nutrition-signal-radar
uv sync --extra dev
.\.venv\Scripts\nutrition-radar.exe doctor

# 资讯线（真实外呼必须 --live）
.\.venv\Scripts\nutrition-radar.exe news list-sources
.\.venv\Scripts\nutrition-radar.exe news fetch --live --lookback-hours 24
.\.venv\Scripts\nutrition-radar.exe news select
.\.venv\Scripts\nutrition-radar.exe news list-items --selected-only
.\.venv\Scripts\nutrition-radar.exe news build --live
.\.venv\Scripts\nutrition-radar.exe news publish --live --confirm --slug news-YYYY-MM-DD --version 1
```

每日无人值守跑 `fetch → select`，build 只在周一、周四触发（一周两期成文节奏，依据实测供给确定；见 `scripts/news_daily.ps1`）。跨期不重复由成文层的 `news_article_items` 排除保证（排除构建日零点前已成文条目，同日重建豁免）。`publish` 永远是人工显式动作。

## 深入文档

| 问题 | 去哪看 |
| --- | --- |
| 怎么跑、当前命令、文档地图 | `README.md` |
| 资讯线需求原文 | `docs/DSA自动化文章需求.docx` |
| 资讯线边界与微信草稿箱 | `docs/adr/0014-news-line-and-wechat-draft-publishing.md` |
| 资讯线日常操作与信源实调 | `docs/NEWS_WORKFLOW.md` |
| 周报人工节奏 | `docs/WEEKLY_WORKFLOW.md` |
| 可发布周报文章契约 | `docs/adr/0013-publishable-weekly-article.md` |
| 术语与对象定义 | `CONTEXT.md` |
| 旧需求基线 / 切片流水账（已归档） | `docs/archive/` |
