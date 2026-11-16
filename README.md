# 营养健康行业信号雷达

面向营养健康产业决策者（品牌方、原料商、企业决策人员）的本机单人内容工作台。它把公开信息变成可审阅的行业信号、选题与可发布文章，服务于自有公众号「EarlyData电商大数据」。

- **CLI**：`nutrition-radar`（Python ≥ 3.10）
- **看板**：只监听 `127.0.0.1` 的只读工作台，覆盖资讯线与行业信号线
- **运行状态**：全部落在本地 `var/`，不入库
- **独立仓库**：不复用 `SentimentTracker` / Weekly Publisher 的代码、数据库或密钥

## 两条内容线

| | 资讯自动线（主线） | 行业信号线（按需） |
| --- | --- | --- |
| 输入 | 人工确认的白名单直采信源（政府 / 交易所 / 品牌官网 / 展会 / 科研组织） | RedFox 监测公众号（发现源，待补证） |
| 产物 | 《营养健康行业资讯｜XXXX》，每期 3–5 条 | 行业信号卡、本周信号快报、可发布周报文章、深度研究稿 |
| 节奏 | 每日 `fetch → select`；周一 / 周四 `build` | 想做深度选题时跑一轮，闲置零成本 |
| 发布 | 可写入公众号**草稿箱**；定时发布在后台人工操作；**群发永远禁止** | 人工复制发布 |

## 它怎么运转

```text
白名单信源
   │  news fetch --live（24h 窗口；浏览器渲染 / RSS / 开放接口）
   ▼
news select（无网络、无 LLM）
   │  入选条件 → P0–P3 → URL/标题去重 → 同模板备案合并 → 薄料硬闸
   ▼
候选池 ──► news readiness（今天能否成文，零成本预检）
   │        news build --dry-run（占位摘要骨架）
   ▼
news build --live（周一 / 周四）
   │  DeepSeek 仅按已抓取原文逐条摘要；确定性模板组装
   ▼
var/exports/news-articles/<slug>/vNNN/（HTML、Markdown、封面、源数据表、QC 日志、manifest）
   │
   ▼
人工复核 ──► news publish --live --confirm ──► 公众号草稿箱
```

几条不变量：

- 筛选、分级、去重、拦截全部是**确定性规则**；LLM 不参与筛选，也不能补充原文没有的事实、数据或金额。
- 成文门槛：日报 3–5 条；当日不足时从 7 日已入选池按 P0→P3 补位；补后仍不足 3 条转周报形态，不足 2 条直接跳过——不为凑数注水。
- 发布是唯一触及外部的写操作，必须同时给出 `--live` 与 `--confirm`，且每稿只推一次；任何调度链都不包含 `publish`。
- 白名单首次确认、研究启动、归档等人工门禁不可绕过。

## 运行现状

- 资讯线全链路已真实运行：36 个白名单源、18 个启用（实时数字以 `news list-sources` 为准）。
- 微信草稿箱网关就绪，首次真实推送待操作者演练。
- 测试基线：`pytest tests/` 全绿（222 项）。
- 抓取停摆、解析腐化、信源无效供给都有显式告警：`doctor` 的 `news_fetch`（超过 48h 未抓取即 WARNING）、`news list-sources` 的停摆提示与「30 天入选/已评估」、看板资讯线页的就绪度与新鲜度。

## 安装

```powershell
cd D:\Earlydata\nutrition-signal-radar
uv sync --extra dev
Copy-Item .env.example .env    # 按需填写凭据；不填也能跑只读命令
.\.venv\Scripts\nutrition-radar.exe doctor
```

凭据只放仓库根目录 `.env`，不写入代码、SQLite、日志、文档或版本库。未配置凭据时，所有真实外呼命令都会被拒绝。

## 快速上手

### 资讯线（主线）

```powershell
# 白名单与健康状态
.\.venv\Scripts\nutrition-radar.exe news list-sources
.\.venv\Scripts\nutrition-radar.exe news set-source-enabled --name "XX" --enable   # 人工确认后启用

# 每日执行：抓取 + 确定性筛选（无 LLM）
.\.venv\Scripts\nutrition-radar.exe news fetch --live --lookback-hours 24
.\.venv\Scripts\nutrition-radar.exe news select
.\.venv\Scripts\nutrition-radar.exe news list-items --selected-only

# 成文日执行：预检 → 骨架 → 成文
.\.venv\Scripts\nutrition-radar.exe news readiness
.\.venv\Scripts\nutrition-radar.exe news build --dry-run
.\.venv\Scripts\nutrition-radar.exe news build --live

# 人工复核：QC 门禁、版本差异、推送草稿箱
.\.venv\Scripts\nutrition-radar.exe news qc --slug news-YYYY-MM-DD --version 1
.\.venv\Scripts\nutrition-radar.exe news diff --slug news-YYYY-MM-DD --from 1 --to 2
.\.venv\Scripts\nutrition-radar.exe news publish --live --confirm --slug news-YYYY-MM-DD --version 1
```

无人值守调度由 `scripts/news_daily.ps1` 承担：每日 `fetch → select`，仅周一 / 周四追加 `build`，永远不含 `publish`。注册任务计划程序与排障见 [NEWS_WORKFLOW](docs/NEWS_WORKFLOW.md)。

### 行业信号线（按需）

```powershell
.\.venv\Scripts\nutrition-radar.exe sync --live                      # 受控同步公众号文章
.\.venv\Scripts\nutrition-radar.exe weekly brief                     # 本周信号快报
.\.venv\Scripts\nutrition-radar.exe weekly article generate --live   # 可发布周报文章
```

### 看板与阅读站

```powershell
# 正式看板：总览 / 资讯线 / 资讯信源 / 行业信号 / 选题看板 / 研究与成稿
.\.venv\Scripts\nutrition-radar.exe serve --port 8765 --open

# 资讯阅读站：直接展示成稿 HTML，适合发布前通读与手机宽度预览
.\.venv\Scripts\nutrition-radar.exe news serve --port 8780 --open
```

两者都只监听 `127.0.0.1`、只读、不进调度链。启用/停用信源等会改变状态的操作用 CLI 完成。

## 产物与本地状态

```text
var/
├── exports/news-articles/<slug>/vNNN/
│   ├── news-article.html   # 内联样式，可直接粘贴公众号编辑器
│   ├── news-article.md     # 母稿
│   ├── cover.png           # 1800×766 封面（本机 Edge 截图，PIL 兜底）
│   ├── cover.html          # 封面浏览器预览
│   ├── source-table.csv    # 入选与落选条目、理由码
│   ├── qc-log.json         # 质检结果
│   └── manifest.json       # 构建元数据
├── raw/sites/YYYY-MM-DD/   # 抓取原始响应存档，用于核对解析问题
└── ...                     # SQLite、导出、日志等本地运行状态
```

每次构建产生新的 `vNNN`，不覆盖旧版本。结构性 QC（`item_count` / `no_forbidden_terms` / `sources_complete`）未通过时 `publish` 直接拒绝；`total_length` 保留为软提示，由编辑判断。

## 配置

| 位置 | 内容 |
| --- | --- |
| `.env` | RedFox、LLM 分析后端（DeepSeek）、pi 写作 Agent、微信草稿箱凭据、`RADAR_DATA_DIR` |
| `news configure-source` | 单个信源的入口页、解析方式、按源代理等 `parser_config` |
| `var/assets/earlydata-logo.png` | 可选封面 Logo；缺失时封面正常生成并在 manifest 标注 |

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

测试默认 mock RedFox、LLM 与外部站点；真实外呼只通过显式 `--live`。数据库迁移是追加式的（schema v17），不要改历史迁移。改完筛选规则后，用 `news revalidate` 回溯清洗已入选存量（只降不升、不复活被拒条目）。

## 文档地图

| 想了解什么 | 去哪看 |
| --- | --- |
| Agent 工作流、硬边界、术语优先级 | `AGENTS.md` |
| 资讯线日常操作、信源实调记录 | `docs/NEWS_WORKFLOW.md` |
| 资讯线需求原文 | `docs/DSA自动化文章需求.docx` |
| 资讯线边界与微信草稿箱 | [ADR-0014](docs/adr/0014-news-line-and-wechat-draft-publishing.md) |
| 周报人工节奏 | `docs/WEEKLY_WORKFLOW.md` |
| 可发布周报文章契约 | [ADR-0013](docs/adr/0013-publishable-weekly-article.md) |
| 术语与对象定义 | `CONTEXT.md` |
| 全部架构决策（0001–0014，追加式） | `docs/adr/` |
| 旧需求基线、切片流水账、来源清单草案 | `docs/archive/`（已归档，仅历史参考） |

## 硬边界（完整版见 `AGENTS.md`）

- 所有配置、源码、测试、数据库与运行产物留在本仓库；独立于历史 `SentimentTracker` / Weekly Publisher。
- 凭据只走 `.env`，不写入代码、SQLite、日志、文档或版本库。
- **群发（mass-send）路径永远禁止**；资讯线自动化的上限是草稿箱，定时发布在公众号后台人工完成。
- 白名单首次确认、发布、研究启动、归档等人工门禁不可绕过；AI 分数只排序解释，不改变状态。
- 资讯线筛选与去重是确定性规则，LLM 只按已抓取原文摘要；不抓竞对公众号，不用 EarlyData 电商数据填充。
- `docs/archive/` 仅作历史参考，不再更新；现行需求以资讯线 docx 与 ADR-0014 为准。
