---
status: accepted
date: 2026-08-15
---

# 纵向深度研究改用 pi coding-agent，并保留烟雾测试门禁

## 背景

纵向分支需要一个能访问公开互联网、返回具体页面 URL 的研究 Agent。原候选 Codex CLI 0.144.5 在桌面应用使用 ChatGPT 登录态时，`codex exec` 直接拒绝运行并要求 API Key 登录；切换登录方式会干扰用户当前会话，因此不能作为稳定后端。

## 决定

- 纵向研究后端改为 pi coding-agent：本机 pi 0.84.1，通过 `agent_gateway.py` 调用其 JSON 事件流模式，并安装 `npm:pi-web-access` 扩展获得 `web_search` / `fetch_content` 工具（零配置 Exa，可用 pi 登录态复用 OpenAI 搜索）。早期试装的 `pi-search-multi` 在 Windows 下加载报解析错误，已卸载。
- 真实后端仍需先通过 `research smoke-test-pi --live` 的 P2-02 烟雾测试：必须返回具体页面 URL、区分来源类型并遵守文章型深度研究稿契约；测试失败则继续停在“待启动”，不得用无来源模型冒充深度研究。
- pi 的订阅登录或 API Key 属于操作者步骤，仅经 `.env` 或进程环境传入；Agent 的输入、事件流与输出保存到 `var/raw/agent/`，日志和存档不落凭据。
- 人工门禁不变：确认选题包 → 创建并确认研究任务书 → 手动启动 → 稿件人工审阅 → 最终文章确认。

## 结果

纵向研究拥有一个可审计、可替换的程序化 Agent 边界；在操作者完成 pi 登录/密钥并跑通烟雾测试前，系统不会自动启动任何真实研究。

2026-08-15 更新：操作者已完成 pi 登录（deepseek provider），`pi-search-multi` 因 Windows 解析错误卸载并改装 `pi-web-access`。`research smoke-test-pi --live` 已通过：Agent 实际调用 5 次 `web_search` / `fetch_content`，返回 SAMR 官方公告、权威媒体与研报等具体 URL，区分来源类型并标注待补证；交换存档位于 `var/raw/agent/`。P2-02 门禁通过，后续可按同一网关把真实公开来源写回研究任务与稿件版本。
