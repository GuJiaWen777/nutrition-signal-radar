# 资讯自动线每日调度（仅到本地产物；发布永远手动）

# 由 Windows 任务计划程序调用（示例：每日 08:30）：
#   schtasks /Create /TN "EarlyDataNewsDaily" /SC DAILY /ST 08:30 `
#     /TR "powershell -ExecutionPolicy Bypass -File D:\Earlydata\nutrition-signal-radar\scripts\news_daily.ps1"
#
# 链路只包含 fetch → select → build；微信草稿箱推送（news publish）有
# --live --confirm 双重人工门禁，永远不在本脚本内运行（ADR-0014）。

$ErrorActionPreference = "Stop"
Set-Location "D:\Earlydata\nutrition-signal-radar"
$radar = ".\.venv\Scripts\nutrition-radar.exe"

& $radar news fetch --live --lookback-hours 24
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $radar news select
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $radar news build --live
exit $LASTEXITCODE
