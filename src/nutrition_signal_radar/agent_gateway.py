"""Isolated research-agent gateway for the pi coding-agent SDK.

The workbench talks to ``pi`` through its documented JSON event-stream mode,
which is the programmatic surface of the pi SDK.  Every run stores its input,
events, and extracted output under ``var/raw/agent/`` so agent calls can be
audited without ever persisting provider credentials.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_PI_PROVIDER = ""
DEFAULT_PI_TIMEOUT_SECONDS = 900
_URL_PATTERN = re.compile(r"https?://[^\s<>\"')\]]+")


class AgentGatewayError(RuntimeError):
    """Raised when the research agent cannot start or produces unusable output."""


@dataclass(frozen=True)
class PiAgentRequest:
    """A source-neutral research instruction for the pi web-search agent."""

    research_question: str
    audience: str = "营养健康行业品牌方、原料商及企业决策者"
    decision_use: str = ""
    scope: str = ""
    time_range: str = ""
    known_evidence: tuple[str, ...] = ()
    evidence_gaps: str = ""
    output_requirements: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "research_question": self.research_question,
            "audience": self.audience,
            "decision_use": self.decision_use,
            "scope": self.scope,
            "time_range": self.time_range,
            "known_evidence": list(self.known_evidence),
            "evidence_gaps": self.evidence_gaps,
            "output_requirements": self.output_requirements,
        }


@dataclass(frozen=True)
class AgentToolCall:
    tool_name: str
    arguments: Mapping[str, object]
    result: str
    is_error: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "result": self.result,
            "is_error": self.is_error,
        }


@dataclass(frozen=True)
class AgentRunResult:
    status: str
    final_text: str
    tool_calls: tuple[AgentToolCall, ...]
    event_lines: tuple[str, ...]
    exit_code: int
    stderr: str

    @property
    def urls(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    url.rstrip(".,;:!?。，；：！？")
                    for call in self.tool_calls
                    for url in _URL_PATTERN.findall(call.result)
                    if not call.is_error
                }
            )
        )

    @property
    def web_urls(self) -> tuple[str, ...]:
        web_tools = {"web_search", "web_read", "fetch_content", "get_search_content"}
        return tuple(
            sorted(
                {
                    url.rstrip(".,;:!?。，；：！？")
                    for call in self.tool_calls
                    if call.tool_name in web_tools and not call.is_error
                    for url in _URL_PATTERN.findall(call.result)
                }
            )
        )

    @property
    def web_tool_calls(self) -> int:
        web_tools = {"web_search", "web_read", "fetch_content", "get_search_content"}
        return sum(call.tool_name in web_tools and not call.is_error for call in self.tool_calls)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "final_text": self.final_text,
            "tool_calls": [call.as_dict() for call in self.tool_calls],
            "urls": list(self.urls),
            "web_tool_calls": self.web_tool_calls,
            "web_urls": list(self.web_urls),
            "exit_code": self.exit_code,
        }


@dataclass
class PiAgentRunner:
    """Configured, credentials-free runner around the local ``pi`` executable."""

    pi_command: Sequence[str] = field(default_factory=lambda: ("pi",))
    provider: str = DEFAULT_PI_PROVIDER
    model: str | None = None
    timeout_seconds: float = DEFAULT_PI_TIMEOUT_SECONDS
    working_directory: Path | None = None
    environment: Mapping[str, str] | None = None
    system_prompt: str | None = None
    json_output: bool = False

    def run(self, request: PiAgentRequest) -> AgentRunResult:
        command = list(self.pi_command) or ["pi"]
        executable = str(command[0])
        if not os.path.dirname(executable):
            resolved = shutil.which(executable)
            if resolved is None:
                raise AgentGatewayError(f"pi executable not found: {executable}")
            command[0] = resolved
        command.extend(("--print", "--mode", "json", "--no-session"))
        command.extend(
            ("--tools", "web_search,fetch_content")
        )
        command.append("--no-context-files")
        if self.provider:
            command.extend(("--provider", self.provider))
        if self.model:
            command.extend(("--model", self.model))
        system_prompt = self.system_prompt or build_pi_system_prompt(
            json_output=self.json_output
        )
        command.extend(("--system-prompt", system_prompt))
        command.append(
            build_pi_research_json_prompt(request)
            if self.json_output
            else build_pi_research_prompt(request)
        )
        environment = dict(os.environ)
        if self.environment:
            environment.update(self.environment)
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.working_directory) if self.working_directory else None,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AgentGatewayError(
                f"pi executable not found: {self.pi_command[0] if self.pi_command else 'pi'}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            return AgentRunResult(
                status="timeout",
                final_text="",
                tool_calls=(),
                event_lines=(),
                exit_code=-1,
                stderr=f"agent timed out after {self.timeout_seconds:.0f} seconds",
            )

        event_lines = _valid_json_lines(completed.stdout)
        final_text, tool_calls = _parse_pi_events(event_lines)
        stderr = _redact_secrets(completed.stderr, environment)
        if completed.returncode != 0 and not final_text:
            return AgentRunResult(
                status="failed",
                final_text=final_text,
                tool_calls=tool_calls,
                event_lines=event_lines,
                exit_code=completed.returncode,
                stderr=stderr[:2000],
            )
        if not final_text.strip():
            return AgentRunResult(
                status="failed",
                final_text="",
                tool_calls=tool_calls,
                event_lines=event_lines,
                exit_code=completed.returncode,
                stderr=(stderr or "agent returned no text").strip()[:2000],
            )
        return AgentRunResult(
            status="completed",
            final_text=final_text,
            tool_calls=tool_calls,
            event_lines=event_lines,
            exit_code=completed.returncode,
            stderr=stderr[:2000],
        )


def build_pi_research_prompt(request: PiAgentRequest) -> str:
    """Render a focused user task for the pi research agent."""

    known = "\n".join(f"- {item}" for item in request.known_evidence) or "（无）"
    sections = [
        "立即执行以下研究任务。直接开始用 web_search 与 fetch_content 检索，"
        "不要读取本地文件、不要自我介绍、不要用项目名推断题目、不要请求补充信息。",
        "",
        f"核心研究问题：{request.research_question or '（未提供）'}",
        f"目标读者：{request.audience}",
        f"决策用途：{request.decision_use or '（未提供）'}",
        f"研究范围：{request.scope or '（未提供）'}",
        f"时间范围：{request.time_range or '（未提供）'}",
        f"已知证据线索：\n{known}",
        f"证据缺口：{request.evidence_gaps or '（未提供）'}",
        f"稿件要求：{request.output_requirements or '简洁微信公众号正文，保留来源链与不确定性。'}",
        "",
        "只输出最终研究草稿，结构如下：",
        "1. 核心判断（一段话）；",
        "2. 正文（面向行业决策者、简洁可读、不堆砌资料）；",
        "3. 行动启示；",
        "4. 来源清单：每条列出具体 URL、发布机构、来源类型（监管原文/官方公告/论文/企业公告/权威媒体等）和它支持的事实；",
        "5. 不确定性与待补证项。",
    ]
    return "\n".join(sections)


def build_pi_system_prompt(*, json_output: bool = False) -> str:
    """Render the evidence contract as the pi system prompt."""

    contract = (
        "你是面向营养健康行业决策者的公开互联网研究 Agent。"
        "只检索无需登录的公开网页；每条重要事实必须落到具体页面 URL；"
        "找不到可靠来源就明确写“不确定”，绝不编造 URL。"
        "区分来源类型（监管原文、官方公告、论文、企业公告、权威媒体等）；"
        "公众号与媒体内容只是发现线索，监管、功效、科研与市场数据的主张必须回到一手来源，"
        "否则标记“待补证”。不要修改任何文件，只输出最终研究草稿。"
    )
    if json_output:
        contract += (
            "最终回复必须是单个合法 JSON 对象，不得使用 Markdown 代码围栏，"
            "不得附加任何解释文字；字段结构严格按用户消息中的示例。"
            "sources 必须包含实际检索到的具体 URL。"
        )
    return contract


def build_pi_research_json_prompt(request: PiAgentRequest) -> str:
    """Render the task plus the strict JSON output contract."""

    return (
        build_pi_research_prompt(request)
        + """

只输出一个 JSON 对象，结构如下：
{
  "title": "文章标题",
  "key_judgment": "一段话核心判断",
  "article_body": "面向行业决策者的 Markdown 正文",
  "action_implications": "行动启示",
  "uncertainty_notes": "不确定性与待补证项",
  "sources": [
    {
      "claim": "这条来源支持的事实",
      "title": "页面标题",
      "url": "https://具体页面",
      "publisher": "发布机构",
      "source_type": "regulator|research_paper|company_announcement|market_data|media|other"
    }
  ]
}
找不到可靠来源的字段写“待补证”，绝不编造 URL。"""
    )


def save_agent_exchange(
    directory: Path,
    request: PiAgentRequest,
    result: AgentRunResult,
) -> tuple[Path, Path, Path]:
    """Atomically store one agent exchange for later audit."""

    target = Path(directory).resolve()
    target.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".exchange-", dir=target))
    input_path = temporary / "input.json"
    output_path = temporary / "output.json"
    events_path = temporary / "events.jsonl"
    try:
        _write_text(
            input_path,
            json.dumps(request.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
        )
        _write_text(output_path, json.dumps(result.as_dict(), ensure_ascii=False, indent=2) + "\n")
        _write_text(
            events_path,
            "\n".join(result.event_lines) + ("\n" if result.event_lines else ""),
        )
        final_input = target / (
            f"{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S')}-input.json"
        )
        final_output = target / final_input.name.replace("-input.json", "-output.json")
        final_events = target / final_input.name.replace("-input.json", "-events.jsonl")
        _write_text(final_input, input_path.read_text(encoding="utf-8"))
        _write_text(final_output, output_path.read_text(encoding="utf-8"))
        _write_text(final_events, events_path.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return final_input, final_output, final_events


def _valid_json_lines(stdout: str) -> tuple[str, ...]:
    lines = tuple(line.strip() for line in stdout.splitlines() if line.strip())
    valid: list[str] = []
    for line in lines:
        try:
            json.loads(line)
        except json.JSONDecodeError:
            continue
        valid.append(line)
    return tuple(valid)


def _parse_pi_events(
    event_lines: Sequence[str],
) -> tuple[str, tuple[AgentToolCall, ...]]:
    text_deltas: list[str] = []
    tool_calls: list[AgentToolCall] = []
    for line in event_lines:
        event = json.loads(line)
        event_type = event.get("type")
        if event_type == "message_update":
            inner = event.get("assistantMessageEvent") or {}
            if inner.get("type") == "text_delta" and isinstance(inner.get("delta"), str):
                text_deltas.append(inner["delta"])
        elif event_type == "tool_execution_end":
            tool_calls.append(
                AgentToolCall(
                    tool_name=str(event.get("toolName", "unknown")),
                    arguments=(
                        dict(event["args"]) if isinstance(event.get("args"), dict) else {}
                    ),
                    result=_stringify(event.get("result")),
                    is_error=bool(event.get("isError")),
                )
            )
    if not text_deltas:
        for line in event_lines:
            event = json.loads(line)
            if event.get("type") == "message_end":
                text_deltas.append(_message_text(event.get("message")))
                break
    return "".join(text_deltas).strip(), tuple(tool_calls)


def _message_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


def _stringify(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _redact_secrets(text: str, environment: Mapping[str, str]) -> str:
    redacted = text
    for name, value in environment.items():
        if value and name.upper().endswith(
            ("_API_KEY", "_API_TOKEN", "_SECRET", "_PASSWORD", "_TOKEN")
        ):
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
