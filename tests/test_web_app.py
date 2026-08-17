import json
import threading
from pathlib import Path
from urllib.request import Request, urlopen

from nutrition_signal_radar.agent_gateway import AgentRunResult
from nutrition_signal_radar.article_contract import ArticleBatch, NormalizedArticle
from nutrition_signal_radar.database import connect_database
from nutrition_signal_radar.demo_pipeline import build_demo_topics
from nutrition_signal_radar.ingest_service import import_article_batch
from nutrition_signal_radar.topic_workflow import transition_candidate
from nutrition_signal_radar.web_app import create_demo_server
from nutrition_signal_radar.weekly_service import generate_weekly_article


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "var" / "demo" / "radar.sqlite3"
    articles = tuple(
        NormalizedArticle(
            external_id=f"article-{index}",
            account_name=f"公众号{index}",
            title="同一事件的多来源报道",
            published_at=f"2026-03-2{index}T09:00:00+08:00",
            url=f"https://example.com/{index}",
            content_snapshot="相同正文",
            content_hash="same-content",
        )
        for index in (1, 2)
    )
    import_article_batch(
        path,
        ArticleBatch(
            source_kind="xlsx",
            source_path="D:/source.xlsx",
            source_fingerprint="fingerprint",
            source_rows=2,
            articles=articles,
        ),
    )
    build_demo_topics(path)
    return path


def test_local_demo_serves_concise_wechat_article_preview(tmp_path: Path) -> None:
    path = _database(tmp_path)
    server = create_demo_server(path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(
            f"http://127.0.0.1:{server.server_port}/topic-article-demo.html",
            timeout=5,
        ) as response:
            html = response.read().decode("utf-8")
        assert response.status == 200
        assert "注册检验必须使用工艺验证批次吗" in html
        assert "排版与内容结构 Demo" in html
        assert "本页面不可直接发布" in html
        assert "一手来源待补证" in html
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_static_workspace_groups_detailed_states_into_simple_display_stages() -> None:
    static_root = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "nutrition_signal_radar"
        / "static"
    )
    script = (static_root / "app.js").read_text(encoding="utf-8")
    index = (static_root / "index.html").read_text(encoding="utf-8")

    assert "const TOPIC_DISPLAY_STAGES" in script
    assert 'label: "待判断"' in script
    assert 'label: "待确认"' in script
    assert 'label: "已选题"' in script
    assert 'statuses: ["candidate_pool", "pending_decision", "package_drafting"]' in script
    assert "const RESEARCH_DISPLAY_STAGES" in script
    assert 'label: "任务书"' in script
    assert 'label: "稿件"' in script
    assert 'label: "已定稿"' in script
    assert "TOPIC_DISPLAY_STAGES.map" in script
    assert "researchDisplayStage(task.status)" in script
    assert "primaryTopicTransition" in script
    assert '<summary>更多操作</summary>' in script
    assert "值得研究 / 传播观察”只用于筛选" in index
    assert 'data-tab="weekly"' in index
    assert 'id="weekly"' in index
    assert "renderWeeklyBrief" in script
    assert "exportWeeklyBrief" in script


def test_research_task_api_stops_at_human_confirmed_pending_start(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)
    connection = connect_database(path, readonly=True)
    try:
        candidate_id = int(
            connection.execute("SELECT id FROM topic_candidates").fetchone()[0]
        )
    finally:
        connection.close()
    transition_candidate(path, candidate_id, "pending_decision")
    transition_candidate(path, candidate_id, "package_drafting")
    transition_candidate(path, candidate_id, "pending_confirmation")
    transition_candidate(path, candidate_id, "selected")

    server = create_demo_server(path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        create_request = Request(
            f"{base_url}/api/candidates/{candidate_id}/research-task",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(create_request, timeout=5) as response:
            created = json.loads(response.read().decode("utf-8"))
            assert response.status == 201
        task = created["research_task"]
        task_id = int(task["id"])

        update_request = Request(
            f"{base_url}/api/research-tasks/{task_id}/update",
            data=json.dumps(
                {
                    "research_question": task["research_question"],
                    "audience": task["audience"],
                    "decision_use": "支持企业决策者判断行动窗口。",
                    "scope": task["scope"],
                    "geography": task["geography"],
                    "time_range": task["time_range"],
                    "exclusions": task["exclusions"],
                    "selection_dimensions": task["selection_dimensions"],
                    "evidence_gaps": task["evidence_gaps"],
                    "output_requirements": task["output_requirements"],
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(update_request, timeout=5) as response:
            updated = json.loads(response.read().decode("utf-8"))

        confirm_request = Request(
            f"{base_url}/api/research-tasks/{task_id}/confirm",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(confirm_request, timeout=5) as response:
            confirmed = json.loads(response.read().decode("utf-8"))
        with urlopen(f"{base_url}/api/candidates/{candidate_id}", timeout=5) as response:
            detail = json.loads(response.read().decode("utf-8"))

        assert created["created"] is True
        assert task["status"] == "task_drafting"
        assert "decision_use" in updated["changed_fields"]
        assert confirmed["research_task"]["status"] == "pending_start"
        assert confirmed["research_task"]["start_mode"] == "manual"
        assert detail["research_task"]["id"] == task_id
        assert detail["research_task"]["editable"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_offline_article_demo_api_creates_review_draft_after_manual_start(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)
    connection = connect_database(path, readonly=True)
    try:
        candidate_id = int(
            connection.execute("SELECT id FROM topic_candidates").fetchone()[0]
        )
    finally:
        connection.close()
    transition_candidate(path, candidate_id, "pending_decision")
    transition_candidate(path, candidate_id, "package_drafting")
    transition_candidate(path, candidate_id, "pending_confirmation")
    transition_candidate(path, candidate_id, "selected")

    server = create_demo_server(path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        create_request = Request(
            f"{base_url}/api/candidates/{candidate_id}/research-task",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(create_request, timeout=5) as response:
            task = json.loads(response.read().decode("utf-8"))["research_task"]
        task_id = int(task["id"])
        update_request = Request(
            f"{base_url}/api/research-tasks/{task_id}/update",
            data=json.dumps(
                {
                    "research_question": task["research_question"],
                    "audience": task["audience"],
                    "decision_use": "支持企业决策者判断行动窗口。",
                    "scope": task["scope"],
                    "geography": task["geography"],
                    "time_range": task["time_range"],
                    "exclusions": task["exclusions"],
                    "selection_dimensions": task["selection_dimensions"],
                    "evidence_gaps": task["evidence_gaps"],
                    "output_requirements": task["output_requirements"],
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(update_request, timeout=5):
            pass
        confirm_request = Request(
            f"{base_url}/api/research-tasks/{task_id}/confirm",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(confirm_request, timeout=5):
            pass
        run_request = Request(
            f"{base_url}/api/research-tasks/{task_id}/run-offline-demo",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(run_request, timeout=5) as response:
            generated = json.loads(response.read().decode("utf-8"))
        with urlopen(f"{base_url}/api/dashboard", timeout=5) as response:
            dashboard = json.loads(response.read().decode("utf-8"))

        task_after = generated["research_task"]
        assert task_after["status"] == "draft_review"
        assert task_after["latest_draft"]["version"] == 1
        assert task_after["latest_draft"]["is_offline_demo"] is True
        assert task_after["latest_draft"]["sources"][0]["evidence_status"] == "needs_evidence"
        assert generated["mutation"]["version"] == 1
        assert dashboard["research_tasks"][0]["id"] == task_id
        assert dashboard["research_tasks"][0]["latest_draft_status"] == "review"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_local_demo_serves_dashboard_detail_and_manual_transition(tmp_path: Path) -> None:
    path = _database(tmp_path)
    server = create_demo_server(path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base_url}/", timeout=5) as response:
            html = response.read().decode("utf-8")
        with urlopen(f"{base_url}/api/dashboard", timeout=5) as response:
            dashboard = json.loads(response.read().decode("utf-8"))

        candidate_id = dashboard["candidates"][0]["id"]
        request = Request(
            f"{base_url}/api/candidates/{candidate_id}/transition",
            data=json.dumps({"to_status": "pending_decision"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            transition = json.loads(response.read().decode("utf-8"))

        track_request = Request(
            f"{base_url}/api/candidates/{candidate_id}/track",
            data=json.dumps(
                {"review_track": "research", "reason": "人工判断值得研究"}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(track_request, timeout=5) as response:
            track = json.loads(response.read().decode("utf-8"))

        edit_request = Request(
            f"{base_url}/api/candidates/{candidate_id}/edit",
            data=json.dumps(
                {
                    "proposed_title": "人工编辑后的选题",
                    "audience": "企业决策者",
                    "core_question": "底层事实是什么？",
                    "why_now": "近期多号关注。",
                    "angle": "区分证据和传播。",
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(edit_request, timeout=5) as response:
            edit = json.loads(response.read().decode("utf-8"))

        drafting_request = Request(
            f"{base_url}/api/candidates/{candidate_id}/transition",
            data=json.dumps({"to_status": "package_drafting"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(drafting_request, timeout=5) as response:
            drafting = json.loads(response.read().decode("utf-8"))

        package_request = Request(
            f"{base_url}/api/candidates/{candidate_id}/package",
            data=json.dumps(
                {
                    "title": "最终确认题目",
                    "audience": "品牌方与企业决策者",
                    "core_question": "这次变化对产品立项意味着什么？",
                    "why_now": "近期出现集中讨论。",
                    "industry_angle": "区分传播和事实增量。",
                    "selection_dimensions": ["监管与合规", "业务含义"],
                    "evidence_gaps": "补充监管原文。",
                    "research_directions": "核实事件与影响范围。",
                    "risk_notes": "公众号材料仅作发现源。",
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(package_request, timeout=5) as response:
            package_edit = json.loads(response.read().decode("utf-8"))

        submit_request = Request(
            f"{base_url}/api/candidates/{candidate_id}/transition",
            data=json.dumps({"to_status": "pending_confirmation"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(submit_request, timeout=5) as response:
            submitted = json.loads(response.read().decode("utf-8"))
        with urlopen(f"{base_url}/api/candidates/{candidate_id}", timeout=5) as response:
            detail = json.loads(response.read().decode("utf-8"))

        cycle_id = int(dashboard["active_cycle"]["id"])
        export_request = Request(
            f"{base_url}/api/cycles/{cycle_id}/export",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(export_request, timeout=5) as response:
            exported = json.loads(response.read().decode("utf-8"))
        close_request = Request(
            f"{base_url}/api/cycles/{cycle_id}/close",
            data=json.dumps({"reason": "测试关闭"}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(close_request, timeout=5) as response:
            closed = json.loads(response.read().decode("utf-8"))
        with urlopen(f"{base_url}/api/candidates/{candidate_id}", timeout=5) as response:
            closed_detail = json.loads(response.read().decode("utf-8"))

        create_request = Request(
            f"{base_url}/api/cycles",
            data=json.dumps(
                {
                    "slug": "2026-w13",
                    "label": "2026 年第 13 周",
                    "start_date": "2026-03-23",
                    "end_date": "2026-03-29",
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(create_request, timeout=5) as response:
            created = json.loads(response.read().decode("utf-8"))
        with urlopen(f"{base_url}/api/dashboard?cycle=2026-w13", timeout=5) as response:
            next_dashboard = json.loads(response.read().decode("utf-8"))

        assert "营养健康行业信号雷达" in html
        assert dashboard["stats"]["articles"] == 2
        assert dashboard["stats"]["signals"] == 1
        assert dashboard["stats"]["watch_queue"] == 1
        assert dashboard["active_cycle"]["status"] == "open"
        assert len(dashboard["cycles"]) == 1
        assert transition["to_status"] == "pending_decision"
        assert track["to_track"] == "research"
        assert "proposed_title" in edit["changed_fields"]
        assert drafting["package_status"] == "drafting"
        assert package_edit["missing_fields"] == []
        assert submitted["to_status"] == "pending_confirmation"
        assert detail["candidate"]["status"] == "pending_confirmation"
        assert detail["candidate"]["review_track"] == "research"
        assert detail["candidate"]["proposed_title"] == "人工编辑后的选题"
        assert detail["package"]["title"] == "最终确认题目"
        assert detail["package"]["can_submit"] is True
        assert detail["package"]["status"] == "pending_confirmation"
        assert exported["version"] == 1
        assert Path(exported["manifest_path"]).is_file()
        assert closed["cycle"]["status"] == "closed"
        assert closed_detail["candidate"]["cycle_status"] == "closed"
        assert closed_detail["allowed_transitions"] == []
        assert created["cycle"]["slug"] == "2026-w13"
        assert next_dashboard["active_cycle"]["slug"] == "2026-w13"
        assert next_dashboard["stats"]["articles"] == 0
        assert next_dashboard["candidates"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_formal_workspace_exposes_source_registry_and_workspace_kind(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)
    server = create_demo_server(
        path,
        port=0,
        workspace_label="Nutrition Signal Radar",
        workspace_kind="formal",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base_url}/api/health", timeout=5) as response:
            health = json.loads(response.read().decode("utf-8"))
        with urlopen(f"{base_url}/api/dashboard", timeout=5) as response:
            dashboard = json.loads(response.read().decode("utf-8"))

        assert health["workspace_kind"] == "formal"
        assert health["workspace_label"] == "Nutrition Signal Radar"
        assert dashboard["demo"]["workspace_kind"] == "formal"
        assert "事实性主张须回到一手来源补证" in dashboard["demo"]["notice"]
        registry = dashboard["source_registry"]
        assert registry["priority_site_enabled_count"] == 0
        assert {account["name"] for account in registry["accounts"]} == {"公众号1", "公众号2"}
        assert all(
            set(account) >= {"name", "enabled", "validation_status", "sync_health"}
            for account in registry["accounts"]
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_weekly_brief_can_be_exported_listed_and_read_from_the_board(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)
    server = create_demo_server(path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base_url}/api/dashboard", timeout=5) as response:
            dashboard = json.loads(response.read().decode("utf-8"))
        cycle_id = int(dashboard["active_cycle"]["id"])
        with urlopen(
            f"{base_url}/api/cycles/{cycle_id}/weekly-briefs", timeout=5
        ) as response:
            initial = json.loads(response.read().decode("utf-8"))
        assert initial["briefs"] == []

        export_request = Request(
            f"{base_url}/api/cycles/{cycle_id}/weekly-briefs",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(export_request, timeout=5) as response:
            exported = json.loads(response.read().decode("utf-8"))
            assert response.status == 201
        assert exported["version"] == 1

        with urlopen(
            f"{base_url}/api/cycles/{cycle_id}/weekly-briefs", timeout=5
        ) as response:
            listed = json.loads(response.read().decode("utf-8"))
        assert len(listed["briefs"]) == 1
        assert listed["briefs"][0]["version"] == 1

        with urlopen(
            f"{base_url}/api/cycles/{cycle_id}/weekly-briefs/1", timeout=5
        ) as response:
            read = json.loads(response.read().decode("utf-8"))
        assert "本周信号快报" in read["markdown"]
        assert read["exists"] is True
        assert Path(read["markdown_absolute_path"]).is_file()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _ArticleRunner:
    def run(self, request: object) -> AgentRunResult:
        payload = {
            "title": "营养保健行业周报|监管趋严、原料创新",
            "summary": "本期聚焦监管整治与原料创新。",
            "article_body": (
                "## 一、监管趋严\n\n7月7日，市场监管总局通报相关案件查办情况。"
            ),
            "editor_notes": "尚未核实的数据已从正文移除。",
            "sources": [
                {
                    "claim": "监管总局公布案件数据",
                    "title": "监管总局发布会实录",
                    "url": "https://example.com/regulator",
                    "publisher": "市场监管总局",
                    "source_type": "regulator",
                }
            ],
        }
        return AgentRunResult(
            status="completed",
            final_text=json.dumps(payload, ensure_ascii=False),
            tool_calls=(),
            event_lines=(),
            exit_code=0,
            stderr="",
        )


def test_weekly_article_can_be_listed_read_confirmed_and_reopened_from_the_board(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)
    article = generate_weekly_article(path, _ArticleRunner())
    server = create_demo_server(path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(
            f"{base_url}/api/cycles/{article.cycle_id}/weekly-articles", timeout=5
        ) as response:
            listed = json.loads(response.read().decode("utf-8"))
        assert len(listed["articles"]) == 1
        assert listed["articles"][0]["status"] == "draft_for_human_review"

        with urlopen(
            f"{base_url}/api/cycles/{article.cycle_id}/weekly-articles/1", timeout=5
        ) as response:
            read = json.loads(response.read().decode("utf-8"))
        assert read["title"].startswith("营养保健行业周报")
        assert "待补证" not in read["markdown"]
        assert Path(read["markdown_absolute_path"]).is_file()

        confirm_request = Request(
            f"{base_url}/api/cycles/{article.cycle_id}/weekly-articles/1/confirm",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(confirm_request, timeout=5) as response:
            confirmed = json.loads(response.read().decode("utf-8"))
        assert confirmed["status"] == "final"

        with urlopen(
            f"{base_url}/api/cycles/{article.cycle_id}/weekly-articles/1", timeout=5
        ) as response:
            read = json.loads(response.read().decode("utf-8"))
        assert read["status"] == "final"
        assert read["confirmed_at"] is not None

        reopen_request = Request(
            f"{base_url}/api/cycles/{article.cycle_id}/weekly-articles/1/reopen",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(reopen_request, timeout=5) as response:
            reopened = json.loads(response.read().decode("utf-8"))
        assert reopened["status"] == "draft_for_human_review"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
