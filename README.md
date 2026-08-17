# 营养健康行业信号雷达

这是“营养健康行业信号雷达”的独立工作区。当前版本完成行业信号审阅、选题判断和选题包确认，提供“已选题 → 可编辑研究任务书 → 人工确认 → 离线文章 Demo → 人工审阅/版本 → 最终本地 Markdown”的交互切片，并可导出等待人工审阅的“本周信号快报”横向扫描草稿；同一期次还可由 pi 生成标题为《营养保健行业周报|主题词》的“可发布周报文章”成品，正文不含“待补证”等内部标记，发布前仍需人工确认。

2026-08-14 起，本项目以独立 Git 仓库维护：`GuJiaWen777/nutrition-signal-radar`。它从历史的 EarlyData 周报实践中派生，但不再是 `SentimentTracker` 仓库中的扩展目录，也不共享代码、运行库、数据库或密钥。原项目只可作为外部 Demo 输入文件的可选位置，不能被本项目写入或修改。

当前状态：需求基线和实施计划已经确认；`F-01` 至 `F-07` 已完成，包括独立脚手架、SQLite 基础、本地 Excel 导入、可操作的选题看板、双审阅队列、可编辑选题包、监测期次隔离和本期选题归档。`P0-01/P0-02` RedFox 窄适配器与六账号受控真实验证已完成：4 个账号获“建议启用”、1 个需修正名称、1 个在优质库未收录；结论不会自动写入来源配置。`P1-01/P1-02` 来源注册表、人工启用门禁、增量同步与账号健康切片已实现；`P1-07` 新增版本化“本周信号快报”横向扫描草稿；2026-08-17 新增 schema v9 `weekly_articles` 表与 pi 生成的可发布周报文章切片。`P2-01` 已完成研究任务书人工门禁；schema v8/v9 在稿件、来源和版本记录基础上增加来源注册、同步审计、周信号快报与可发布周报文章记录，并实现一个必须人工启动的离线文章 Demo。它不调用微信接口，也不能自动发布。

行业信号向下游分叉为两条已确认的路径：每周“本周信号快报”做横向扫描、周度输出；另一路经人工确认选题后进入纵向深度研究，形成高价值文章。

2026-08-15 已完成首次真实数据演练：4 个优质库账号加庶正康讯广域库账号共 5 个来源启用。3 天回看同步得到 34 篇文章和 3 张行业信号卡并生成首份正式“本周信号快报”；随后完成 30 天回看同步，共沉淀 407 篇文章，并经 `knowledge-base backfill` 达到 407/407 正文全覆盖。初始 3 天窗口期次已关闭，新开 `cycle-2026-07-17-08-15` 演练期次重建出 254 个主题簇、15 个行业信号，并生成 30 天横向快报。本地知识库已固化不可覆盖快照 `var/knowledge-base/export-v003/`。Bonny营养领航已补录：RedFox 广域库作者名精确为“Bonny营养领航”，但广域关键词搜索无法按账号名枚举其文章，故启用人工链接补录路径（`sources import-article`），操作者提供的益生菌监管修订文章已入库。同日 P2-02 首次烟雾测试显示本机 Codex CLI 在 ChatGPT 登录态下无法执行 `codex exec`，自动研究接入继续停止；研究任务保持“待启动”，离线文章 Demo 仍是唯一稿件生成切片。

同日已把纵向研究后端切换为 pi coding-agent：本机 pi 0.84.1 已登录 deepseek provider，并改装 `npm:pi-web-access`（`web_search` / `fetch_content`，零配置 Exa；`pi-search-multi` 因 Windows 解析错误已卸载）。`agent_gateway.py` 提供程序化调用与 `var/raw/agent/` 审计存档；`research smoke-test-pi --live` 已通过 P2-02 烟雾测试——围绕题目实际执行网页检索并返回 SAMR 官方公告等具体 URL、区分来源类型、标注待补证。随后完成 P2-03 在线切片：`research run --task-id N --live` 把人工确认的任务书交给 pi，按 JSON 契约写回文章型深度研究稿与逐条“待补证”来源，失败自动回退待启动。RedFox 数据已落两层本地状态（SQLite 归一化文章 + `var/raw/redfox/` 脱敏原始响应），并新增 `knowledge-base export / backfill` 把知识库正文补全并固化版本化快照。

2026-08-17 新增“可发布周报文章”切片：schema v9 增加独立 `weekly_articles` 表（与快报各自 v001 起编号），`weekly article generate --live` 让 pi 读取本期信号快照并按两篇参考周报文体写作；标题与正文硬性禁止“待补证”等内部标记，不核实项只写入 `manifest.json` 的 `editor_notes` 与 `sources`。生成后停在 `draft_for_human_review`，由操作者 `confirm` 定稿或 `reopen` 退回；看板“本周快报”标签内分“信号快报 / 可发布文章”两种视图。仍然不包含任何微信公众号写入或群发路径。

2026-08-18 已用当前 30 天期次 `cycle-2026-07-17-08-15` 真实生成第一篇周报文章 `var/exports/weekly-articles/cycle-2026-07-17-08-15/v001/`：正文约 3800 字、32 条来源、零内部标记，内容覆盖益生菌注册新规、央视虚假宣传曝光、低 GI 白皮书、美国 GRAS 拟议规则与日本 FFC 等本期信号，当前状态为 `draft_for_human_review`，等待人工确认。

已确认的基本边界：

- 本项目不得修改历史 EarlyData Weekly Publisher 的代码、命令、状态机、配置、文档或行为。
- 所有代码、配置、文档、测试和运行产物均属于本仓库；`var/` 为本机状态，不纳入版本控制。
- 它不是 `earlydata-weekly` 的新模块，拥有独立的生命周期、数据、配置、命令和产物。
- 任何共享模块抽取、跨项目状态复用或发布流程交接，都需要用户另行明确批准。
- 选题包、研究任务书和文章型深度研究稿是不同产物；深度研究稿经过人工事实与编辑审阅后直接成为最终文章 Markdown。
- 深度研究默认由操作者手动启动；可显式开启自动启动，但不能绕过研究任务书的人工确认。
- 第一版优先保持简单：误建研究任务只取消并留痕，不支持任务合并、报告改挂或物理删除。
- 公众号监测首先形成“行业信号卡”，不把有限的行业媒体来源称为完整“舆情分析”；选中的信号才继续生成选题。
- 当前扩展已经实现本地离线文章 Demo、人工退回重生成、人工编辑另存版本和最终 Markdown 归档；它只根据任务书与已有发现线索组织结构，明确不联网、不新增事实、不提升证据状态。选题看板中的“查看文章 Demo”仍只是静态 HTML 结构样稿，也不会发布到微信公众号。

当前可运行：

```powershell
cd D:\Earlydata\nutrition-signal-radar
# 首次克隆后先建立本项目自己的环境：
uv sync --extra dev
.\.venv\Scripts\nutrition-radar.exe doctor
.\.venv\Scripts\nutrition-radar.exe db status
# 需要创建真实本地运行库时再显式执行：
.\.venv\Scripts\nutrition-radar.exe db init
# 用外部 Excel 构建隔离 Demo（输入文件只读；真实工作簿不纳入本仓库）：
.\.venv\Scripts\nutrition-radar.exe demo import-xlsx `
  --input "D:\Earlydata\SentimentTracker\data\input\P&B微信公众号3月数据.xlsx"
# 先聚合主题，再生成明确标注为规则 Demo 的行业信号和候选选题：
.\.venv\Scripts\nutrition-radar.exe demo build
# 启动只监听本机的看板：
.\.venv\Scripts\nutrition-radar.exe demo serve --port 8765 --open

# RedFox 正式管线：先建来源提案，人工确认后才启用，再受控同步。
# 以下所有真实外部请求都必须显式加 --live：
.\.venv\Scripts\nutrition-radar.exe sources seed-priority-sites
.\.venv\Scripts\nutrition-radar.exe sources configure-account `
  --name "营养健康评论" --redfox-name "<RedFox 精确展示名>" `
  --redfox-account "<account 标识>" --validation-status matched
.\.venv\Scripts\nutrition-radar.exe sources set-enabled --name "营养健康评论" --enable
.\.venv\Scripts\nutrition-radar.exe sync --live --lookback-days 3

# 每周横向扫描：生成等待人工审阅的“本周信号快报”草稿：
.\.venv\Scripts\nutrition-radar.exe weekly brief
# 查看当期快报与可发布文章版本：
.\.venv\Scripts\nutrition-radar.exe weekly list
# 生成可发布周报文章（真实调用 pi 需要 --live；正文禁止内部标记）：
.\.venv\Scripts\nutrition-radar.exe weekly article generate --live
# 人工确认定稿 / 回到待编辑（仍由人工复制到公众号，不自动发布）：
.\.venv\Scripts\nutrition-radar.exe weekly article confirm --version 1
.\.venv\Scripts\nutrition-radar.exe weekly article reopen --version 1

# 本地知识库：查看文章/正文覆盖并固化不可覆盖的版本化快照：
.\.venv\Scripts\nutrition-radar.exe knowledge-base stats
.\.venv\Scripts\nutrition-radar.exe knowledge-base export
.\.venv\Scripts\nutrition-radar.exe knowledge-base backfill --live

# 对广域库无法按名称枚举的账号，操作者确认后人工补录单篇链接：
.\.venv\Scripts\nutrition-radar.exe sources import-article `
  --account "Bonny营养领航" --url "https://mp.weixin.qq.com/s/..."

# 纵向研究烟雾测试（pi 已登录；真实调用需显式加 --live）：
.\.venv\Scripts\nutrition-radar.exe research smoke-test-pi --live
# 启动一个已人工确认的研究任务书，pi 检索公开网页并写回待审稿件：
.\.venv\Scripts\nutrition-radar.exe research run --task-id 1 --live

# 正式来源配置完成并同步后，启动只监听本机的正式工作台：
.\.venv\Scripts\nutrition-radar.exe serve --port 8765 --open
```

当前 Excel Demo 从只读工作簿的 1,731 行中归一化得到 1,677 篇文章、17 个公众号；54 行微信跟踪链接差异被合并为同一文章，0 行跳过。结果保存在 `var/demo/radar.sqlite3`，与未来正式库 `var/radar.sqlite3` 隔离；重复导入只更新已有文章，不产生重复记录。为保护本地运行状态，迁移到独立仓库时不会复制 `var/`；需要 Demo 数据时按上方命令重新导入。该 P&B 数据集用于验证导入、去重、聚类和看板机制，不代表营养健康正式来源覆盖。

`demo build` 当前形成 1,358 个主题簇，其中 196 个多文章主题生成 196 张行业信号卡和 196 张候选选题卡。聚类使用正文近似去重、标题相似度和一组可审计的强行业主题词；“健康、营养、食品”等宽泛词不会单独触发合并。“独立来源估算”同时受账号覆盖和正文近似度约束，不能当作事实已经获得独立验证。会议活动、资讯汇总和同稿扩散优先进入观察队列，阅读量只作为弱信号并按独立来源估算折算。两类只用于分组和队列内排序，所有卡片仍停留在“候选池”。

选题看板默认用“待判断 → 待确认 → 已选题”三段展示；“值得研究 / 传播观察”只用于筛选和排序，不再与流程阶段混在一起。“待判断”中的新候选按当前队列排序默认展示前 10 条，可手动“显示全部”或再次收起；输入搜索词时会展示全部匹配项。该限制只影响界面展示，不会删除、归档或改变任何候选状态，正在处理的选题始终完整展示。

操作者可以人工改类和编辑候选题目、目标读者、核心问题、“为什么现在”及差异化角度；这些操作会留痕，并在重跑 Demo 时保留。为保留人工门禁，系统仍记录候选、判断、选题包完善和确认等细分操作；它们仅在详情和事件中说明当前下一步，不再各占一个看板列。详情默认只露出一项“下一步”操作，退回、改队列和归档收纳在“更多操作”中。进入“选题包完善”后，可继续编辑最终标题、受众、核心问题、判断角度、选题判断维度、公众号线索、待补证事实、研究方向和风险提示。草稿允许暂时不完整，但缺少任一提交必需项时不能进入“待确认”；提交后字段锁定，退回“选题包完善”才可继续修改。

选题仍需依次推进“待决策 → 选题包完善 → 待确认 → 已选题”，不能跳过门禁。公众号文章在选题包中只作为发现线索并标记“待补证”；确认选题本身不会自动创建或启动研究任务。

已选题的卡片和详情页可直接打开对应的微信公众号文章结构 Demo。该链接只对当前确有匹配样稿的选题显示；卡片本身仍可点击进入完整选题详情。详情页还可由人工生成一份独立研究任务书，编辑研究问题、受众、决策用途、研究范围、地域与时间范围、判断维度、排除项、待补证主张和稿件要求。任务书必须再次人工确认，确认后锁定并进入“待启动”；自动启动默认关闭。

“研究与成稿”标签页默认按“任务书 → 稿件 → 已定稿”显示本期的下游任务；任务详情用“补全并确认、等待人工启动、等待人工审阅”等下一步提示代替细分状态名。确认后的任务只能由操作者手动启动“离线文章 Demo”：系统只用任务书和随任务带入的公众号发现线索生成文章型 V1，页面和正文均标明“未联网检索”。所有发现源继续为“待补证”，不会生成或暗示任何新事实。操作者可退回生成 V2/V3、直接编辑正文并另存新版本；旧稿不会被覆盖。人工确认当前版本时，系统将它保存为数据库同级目录下的 `article-drafts/task-NNNN/article-vNNN-final.md`，并写入审阅说明和来源/证据状态。确认 Markdown 不等于授权发布，也不会触发公众号接口。

同一“待启动”任务也可由操作者用 `nutrition-radar research run --task-id N --live` 启动真实联网研究：pi 按任务书检索公开网页，按 JSON 契约返回文章型深度研究稿，写入正文与逐条来源（URL、发布机构、来源类型），所有来源保持“待补证”直到人工复核；Agent 失败或输出不符合契约时任务回退“待启动”并留痕，不生成伪稿件。

页面顶部现在提供“监测期次”工作区。来源文章在同一数据库中全局去重，但主题、信号、选题及其人工状态按期次隔离；同一时间只允许一个进行中的期次。关闭本期后历史内容只读，才能创建空白下一期。切换历史期次不会混入其他期的看板状态。

“导出本期选题归档”由人工触发，每次在数据库同级目录生成新的 `exports/cycles/<期次标识>/vNNN/`：其中包含 `topic-selection.md`、已有选题包和 `manifest.json`。它只保存选题看板、候选依据和人工状态，不生成行业周报、深度研究稿或最终文章，也不覆盖旧版本。已生成的旧版归档保持原样；新版本采用这一收窄后的格式。

### 本周信号快报（横向周度扫描）

`nutrition-radar weekly brief` 把当期活跃行业信号按候选选题辅助分、文章量和时效排序，生成不可覆盖的版本化 Markdown 草稿：`exports/weekly-signal-briefs/<期次标识>/vNNN/weekly-signal-brief.md`，默认最多 8 条，每条附公众号发现线索和待补证标记。快报固定为 `draft_for_human_review`：不核实事实、不改变选题或研究状态、不创建研究任务、不调用模型，也不产生任何微信公众号写入或发布动作。它与“本期选题归档”和可发布周报文章是不同产物，边界见 [ADR-0011](docs/adr/0011-weekly-signal-brief-horizontal-scan.md)。

看板已新增独立的「本周快报」标签页，与「研究与成稿」分开：按监测期次列出已有版本、展示 Markdown 全文，并由操作者点击「导出信号快报」生成新版本；页面只读，不自动生成、不改变任何状态。

### 可发布周报文章（pi 生成、人工确认）

`nutrition-radar weekly article generate --live` 读取当期行业信号快照，交给 pi 公开网页 Agent 按《营养保健行业周报》参考文体写作：标题形如“营养保健行业周报|主题词1、主题词2”，正文按主题分节，每条以“日期 + 主体 + 事件 + 关键数据 + 影响/判断”呈现。生成结果写入 `exports/weekly-articles/<期次标识>/vNNN/`：

- `weekly-article.md` 是可直接粘贴发布的纯正文（无 frontmatter、无内部标记）；
- `manifest.json` 保存内部编辑备注、来源清单、生成时间与发布契约；
- `confirmation.json` 记录人工确认状态。

标题与正文中出现“待补证”“公众号发现线索”“内部状态”“人工审阅”“草稿”“未核实”等内部标记时，系统拒绝入库并要求重新生成；无法核实的事实只写入 `editor_notes` 供人工复核。文章停在 `draft_for_human_review`，由操作者 `weekly article confirm --version N` 定稿（可 `reopen` 退回）。确认后由人工把正文复制到微信公众号发布，本仓库不包含草稿箱或群发路径。边界见 [ADR-0013](docs/adr/0013-publishable-weekly-article.md)。

### 来源注册与增量同步（RedFox Phase 1）

`sources` 命令维护公众号监测名单与重点网站清单，全部默认停用。公众号只有先通过 `redfox validate --live` 完成精确名称校验、由操作者把结果人工标记为 `matched` 并显式 `set-enabled` 后，`sync --live` 才会发起计费请求；优质库正文抽样有全程预算，广域库按精确展示名分页，单个账号失败不影响其他账号。`redfox broad-search --live --keyword <名称>` 只做只读补录发现，永不自动启用来源。看板“来源”标签同时显示启用门禁、校验状态、库类型、优先级和同步健康。

当前验证结果：在独立仓库目录中已通过 105/105 项项目测试；历史迁移前现有 Weekly Publisher 回归测试 24/24 通过。覆盖旧数据库升级（至 schema v9）、跨期隔离、关闭后上游只读、WeRSS 跨日期接入试验标识、RedFox 分页/正文补全/抽样预算/瞬态重试/错误脱敏/人工名称门禁、广域库只读搜索、来源注册人工启用门禁、增量同步的优质/广域分流与单账号失败隔离、缺正文针对性回填、人工链接文章补录、本地知识库统计与不可覆盖快照、pi 研究 Agent 假网关与 JSON 事件解析、周信号快报连续版本、可发布周报文章的生成/内部标记拦截/多字段结构兼容/确认/重开/独立版本号、看板导出/列表/读取/确认与审计事件、强行业主题词聚合、从历史已选题创建下游研究任务、任务书编辑与人工确认、离线稿件启动门禁、在线研究稿件与待补证来源、稿件版本不可覆盖、最终 Markdown 存档、本地 HTTP API 和简化展示阶段的前端契约。浏览器 QA 已复验“历史期次 → 研究与成稿 → 任务书状态 → 隔离 QA 库中的待启动 → 离线 V1 → 待审 V2”界面闭环、“来源”注册表标签和新增的“本周快报”标签页（版本列表与 Markdown 渲染），控制台无新增应用错误。真实 Demo 已按上方命令在本仓库重新导入并升级到 schema v9，数量仍为 1,677 篇文章、1,358 个主题簇、196 个信号 / 候选，且已实际生成 `var/demo/exports/weekly-signal-briefs/demo-2026-03/v001` 快报草稿。正式运行库已完成 30 天真实同步演练，开放期次为 `cycle-2026-07-17-08-15`，本地知识库 408 篇正文全覆盖并固化为 `var/knowledge-base/export-v003/`；每周继续 `sync --live`、`weekly brief` 与可选的 `weekly article generate --live` 节奏由操作者执行。RedFox、DeepSeek 和 pi 的凭据均可后续在本项目自己的 `.env` 中配置，`doctor` 只报告是否配置，不显示密钥内容。

### WeRSS 五号接入试验

`we-mp-rss validate / import / build / serve` 使用独立的 `var/we-mp-rss-pilot/radar.sqlite3`，不会读写 Excel Demo 或正式库。首轮已为庶正康讯、营养健康评论、健康产业观察、营养品观察、功能食品圈各验证 1 篇完整正文，5/5 来源可导入；两篇明确包含“益生菌”的文章先聚合为 1 个主题，再生成 1 张“值得研究”候选卡。由于这些是跨 2024—2026 年的连接种子文章，界面明确标成“WeRSS 五号接入试验（跨日期样本）”，不把它误称为某一期正式行业周报。当前已知文章链接可直接抓取；公众号历史列表接口仍受上游微信变化影响，因此本阶段采用“人工补充文章链接 → WeRSS 抓正文 → 看板导入”的稳妥路径。

```powershell
.\.venv\Scripts\nutrition-radar.exe we-mp-rss validate
.\.venv\Scripts\nutrition-radar.exe we-mp-rss import
.\.venv\Scripts\nutrition-radar.exe we-mp-rss build
.\.venv\Scripts\nutrition-radar.exe we-mp-rss serve --port 8766 --open
```

### RedFox 六账号只读验证（P0）

RedFox 接入目前只完成“账号和数据质量验证”，还没有正式同步、来源自动启用、信号生成、研究启动或微信发布路径。适配器只使用 RedFox 公众号质量库的[账号搜索](https://redfox.hk/apis/gongzhonghao/DNVPQZEZ)、[文章列表](https://redfox.hk/apis/gongzhonghao/XNV30XZ3)和[文章详情](https://redfox.hk/apis/gongzhonghao/XEO0QQNF)接口，并把 API key 放在本项目根目录、不会纳入版本控制的 `.env` 中：

```dotenv
REDFOX_API_KEY=你的密钥
REDFOX_BASE_URL=https://redfox.hk
REDFOX_TIMEOUT_SECONDS=20
REDFOX_MAX_RETRIES=1
```

真实请求可能消耗 RedFox 配额，因此命令必须显式带上 `--live`；默认每个已精确匹配账号最多读取 3 页列表（每页 20 篇），全程最多请求 30 篇缺失正文。对 `429/502/503/504` 和连接超时，适配器默认只即时重试 1 次（可用 `REDFOX_MAX_RETRIES=0` 关闭）；可按需要提高页数或缩小到单个账号复核。

```powershell
# 不带 --live 时只显示保护提示，不会访问 RedFox：
.\.venv\Scripts\nutrition-radar.exe redfox validate

# 六账号、回溯 30 天的受控真实验证：
.\.venv\Scripts\nutrition-radar.exe redfox validate --live --lookback-days 30

# 仅复核一个账号，或在报告提示分页不完整时提高上限：
.\.venv\Scripts\nutrition-radar.exe redfox validate --live `
  --account "庶正康讯" --max-pages 10 --max-detail-requests 30
```

每次真实验证把已脱敏的原始请求/响应快照保存到 `var/raw/redfox/YYYY-MM-DD/`，并覆盖写出最新的 `var/exports/redfox_validation.json` 和 `var/exports/redfox_validation.md`。报告为每个账号给出“启用 / 修正名称 / 暂停”建议、RedFox 精确账号、文章数、最近发布时间、正文抽样成功率、当前正文覆盖率、重复率、分页状态和错误摘要。抽样预算耗尽导致的当前覆盖率偏低不会被误当作正文接口失败；“启用”仍只是建议：操作者必须人工确认账号与样本质量后，才能在后续 Phase 1 配置正式来源。

命令成功生成报告即返回成功码；“暂停 / 修正名称”是需要处理的验证结论，不是命令故障。需要自动化判断时读取 JSON 中的 `ready_for_next_phase`，它只有六个账号都获“建议启用”时才为 `true`。

2026-08-14 的完整验证使用 30 天窗口、最多 9 页列表和全程 30 次正文抽样；四个精确匹配账号的 30/30 次正文请求均成功。该次运行的结论如下：

| 账号 | 结论 | 证据摘要 |
| --- | --- | --- |
| 庶正康讯 | 暂停 | RedFox 优质库明确返回“暂未收录该内容”，本次未消耗积分。 |
| 营养健康评论 | 建议启用 | 50/50 篇，8/8 正文抽样成功。 |
| Bonny营养领航 | 修正名称 | 仅返回非精确名称候选，系统没有自动选择账号。 |
| 健康产业观察 | 建议启用 | 22/22 篇，8/8 正文抽样成功。 |
| 营养品观察 | 建议启用 | 168 条提供方记录归一化为 167 篇，9 页完整读取，7/7 正文抽样成功。 |
| 功能食品圈 | 建议启用 | 101/101 篇，6 页完整读取，7/7 正文抽样成功。 |

2026-08-15 广域库补录：`redfox broad-search --live` 显示“庶正康讯”在广域库有 38/40 篇精确作者命中且持续更新，已按广域库来源启用；“Bonny营养领航”在两个库均无精确作者命中，继续暂停，等待人工提供可核验的 account 标识后再补录。

当前材料：

- [领域语言](CONTEXT.md)
- [需求与设计基线](DESIGN_DRAFT.md)
- [实施计划与任务拆分](IMPLEMENTATION_PLAN.md)
- [重点网站清单草案](SOURCE_REGISTRY_DRAFT.md)
- [隔离边界 ADR（历史）](docs/adr/0001-isolated-extension.md)
- [独立仓库迁移 ADR](docs/adr/0010-independent-repository-migration.md)
- [阶段分离与人工门禁 ADR](docs/adr/0004-separate-topic-research-writing.md)
- [首版合并深度研究与文章初稿 ADR](docs/adr/0008-article-shaped-deep-research-draft.md)
- [离线文章 Demo 与真实深度研究边界 ADR](docs/adr/0009-offline-article-demo-is-not-deep-research.md)
- [每周横向信号快报与纵向深度研究双分支 ADR](docs/adr/0011-weekly-signal-brief-horizontal-scan.md)
- [纵向深度研究改用 pi coding-agent ADR](docs/adr/0012-pi-agent-for-vertical-research.md)
- [可发布周报文章与横向快报分离 ADR](docs/adr/0013-publishable-weekly-article.md)
- [先用本地 Excel Demo、后接 RedFox ADR](docs/adr/0006-local-xlsx-demo-before-redfox.md)
- [单库监测期次隔离 ADR](docs/adr/0007-logical-monitoring-cycles.md)

需求 grilling 已结束。当前先用真实本地数据完成“文章 → 主题聚合 → 行业信号 → 选题看板 → 研究任务书 → 离线文章 Demo → 版本化 Markdown”的简单 Demo；RedFox 作为同一归一化接口的后续正式来源接入。真正的联网深度研究仍须完成独立的公开网页来源烟雾测试，之后才接入同一稿件模型。

微信公众号文章结构 Demo 可从“已选题”卡片或详情页点击打开，也可在本地服务启动后直接访问 `http://127.0.0.1:8765/topic-article-demo.html`。它使用当前 Demo 中已确认的选题演示简洁正文、行动建议和证据说明，不代表已完成研究，也不可直接发布。

### 可直接发送给经理的单文件 Demo

[经理版选题看板交互 Demo](docs/demos/earlydata-topic-board-manager-demo.html) 是一份完全独立的 HTML 文件，不依赖本地服务、数据库、外部样式或网络请求。复制这一个文件即可通过邮件或聊天工具发送；收件人双击后可以切换“选题看板 / 研究任务”、筛选审阅队列、点击候选卡片、模拟人工推进、查看微信公众号文章结构、编辑研究任务书并演示“人工确认 → 待启动”。所有内容均明确标记为界面示意，操作只存在于当前页面，刷新即恢复，不会联网、保存或触发研究与发布。
