# 资讯自动线每日调度（仅到本地产物；发布永远手动）

# 由 Windows 任务计划程序调用（示例：每日 08:30）：
#   schtasks /Create /TN "EarlyDataNewsDaily" /SC DAILY /ST 08:30 `
#     /TR "powershell -ExecutionPolicy Bypass -File D:\Earlydata\nutrition-signal-radar\scripts\news_daily.ps1"
#
# 链路：fetch → select 每日运行（不走 LLM，保持候选池新鲜）；
# build 只在周一、周四触发（一周两期的成文节奏）。成文门槛
# 「≥3 条日报 / ≥2 条周报形态 / 不足跳过」继续兜底，跳过不产生
# LLM 调用。跨期不重复由成文层的 news_article_items 排除保证。
# 微信草稿箱推送（news publish）有 --live --confirm 双重人工门禁，
# 永远不在本脚本内运行（ADR-0014）。

$ErrorActionPreference = "Stop"
Set-Location "D:\Earlydata\nutrition-signal-radar"
$radar = ".\.venv\Scripts\nutrition-radar.exe"

& $radar news fetch --live --lookback-hours 24
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $radar news select
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# DayOfWeek: 0=Sunday ... 1=Monday ... 4=Thursday
$buildDays = @(1, 4)
if ($buildDays -contains [int](Get-Date).DayOfWeek) {
    & $radar news build --live
    exit $LASTEXITCODE
}

Write-Output "今日非成文日（仅周一/周四 build），fetch → select 完成。"
exit 0
