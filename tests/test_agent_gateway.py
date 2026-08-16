import json
import sys
from pathlib import Path

from nutrition_signal_radar.agent_gateway import (
    AgentGatewayError,
    PiAgentRequest,
    PiAgentRunner,
    build_pi_research_prompt,
    build_pi_system_prompt,
    save_agent_exchange,
)
from nutrition_signal_radar.cli import main


def _fake_pi_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake_pi.py"
    events = [
        {"type": "session", "version": 3, "id": "fixture", "timestamp": "t", "cwd": "."},
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_start", "message": {"role": "assistant", "content": []}},
        {
            "type": "message_update",
            "usage": {},
            "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "核心判断："},
        },
        {
            "type": "message_update",
            "usage": {},
            "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "证据已核实。"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "c1",
            "toolName": "web_search",
            "args": {"query": "保健食品原料目录"},
            "result": '{"results":[{"url":"https://www.samr.gov.cn/example/1"}]}',
            "isError": False,
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "c2",
            "toolName": "web_read",
            "args": {"url": "https://www.samr.gov.cn/example/1"},
            "result": "监管页面正文",
            "isError": False,
        },
        {"type": "message_end", "message": {"role": "assistant", "content": []}},
        {"type": "agent_end", "messages": []},
    ]
    script.write_text(
        "import json, sys\n"
        "sys.stdout.reconfigure(encoding='utf-8')\n"
        "events = " + repr(events) + "\n"
        "for event in events:\n"
        "    print(json.dumps(event, ensure_ascii=False))\n",
        encoding="utf-8",
    )
    return script


def test_build_pi_research_prompt_carries_evidence_contract() -> None:
    prompt = build_pi_research_prompt(
        PiAgentRequest(
            research_question="谁发布了新版原料目录？",
            known_evidence=("公众号线索 A",),
        )
    )
    system_prompt = build_pi_system_prompt()

    assert "谁发布了新版原料目录？" in prompt
    assert "公众号线索 A" in prompt
    assert "web_search 与 fetch_content" in prompt
    assert "不要用项目名推断题目" in prompt
    assert "绝不编造 URL" in system_prompt
    assert "待补证" in system_prompt
    assert "监管原文" in system_prompt


def test_pi_runner_parses_json_events_and_extracts_urls(tmp_path: Path) -> None:
    script = _fake_pi_script(tmp_path)
    runner = PiAgentRunner(
        pi_command=[sys.executable, str(script)],
        provider="google",
        timeout_seconds=30,
    )

    result = runner.run(
        PiAgentRequest(
            research_question="测试题",
            audience="企业决策者",
        )
    )

    assert result.status == "completed"
    assert result.final_text == "核心判断：证据已核实。"
    assert [call.tool_name for call in result.tool_calls] == ["web_search", "web_read"]
    assert result.urls == ("https://www.samr.gov.cn/example/1",)
    assert result.exit_code == 0


def test_pi_runner_reports_missing_executable(tmp_path: Path) -> None:
    runner = PiAgentRunner(pi_command=["definitely-not-pi-xyz"], timeout_seconds=10)

    try:
        runner.run(PiAgentRequest(research_question="测试题"))
    except AgentGatewayError as exc:
        assert "pi executable not found" in str(exc)
    else:
        raise AssertionError("expected AgentGatewayError")


def test_save_agent_exchange_stores_input_output_and_events(tmp_path: Path) -> None:
    script = _fake_pi_script(tmp_path)
    result = PiAgentRunner(
        pi_command=[sys.executable, str(script)], timeout_seconds=30
    ).run(PiAgentRequest(research_question="测试题"))
    request = PiAgentRequest(research_question="测试题")

    input_path, output_path, events_path = save_agent_exchange(
        tmp_path / "raw" / "agent", request, result
    )

    assert input_path.is_file()
    assert output_path.is_file()
    assert events_path.is_file()
    assert json.loads(input_path.read_text(encoding="utf-8"))["research_question"] == "测试题"
    assert json.loads(output_path.read_text(encoding="utf-8"))["status"] == "completed"
    assert events_path.read_text(encoding="utf-8").count("\n") >= 3


def test_research_smoke_cli_requires_live_acknowledgement(tmp_path: Path, capsys) -> None:
    result = main(["--project-root", str(tmp_path), "research", "smoke-test-pi", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert result == 2
    assert payload["status"] == "protected"
    assert "--live" in payload["message"]
