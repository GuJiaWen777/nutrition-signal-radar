from __future__ import annotations

import json
import re
import sqlite3
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .archive_service import export_cycle_markdown_archive
from .cycle_service import (
    MonitoringCycleError,
    close_monitoring_cycle,
    create_monitoring_cycle,
    get_monitoring_cycle,
    list_monitoring_cycles,
)
from .database import connect_database
from .demo_pipeline import DEMO_GENERATOR
from .research_workflow import (
    ResearchWorkflowError,
    confirm_research_task,
    create_research_task,
    finalize_research_draft,
    get_research_task,
    list_research_tasks,
    regenerate_offline_draft_demo,
    run_offline_draft_demo,
    save_draft_revision,
    update_research_task,
)
from .schema import initialize_schema
from .source_registry import list_priority_sites, list_source_accounts
from .topic_workflow import (
    ALLOWED_TRANSITIONS,
    PACKAGE_FIELD_LABELS,
    TopicWorkflowError,
    set_candidate_review_track,
    topic_package_missing_fields,
    transition_candidate,
    update_candidate_editorial_fields,
    update_topic_package,
)
from .weekly_service import (
    WeeklyArticleError,
    confirm_weekly_article,
    export_weekly_signal_brief,
    list_weekly_articles,
    list_weekly_signal_briefs,
    read_weekly_article,
    read_weekly_signal_brief,
    reopen_weekly_article,
)


STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/topic-article-demo.html": ("topic-article-demo.html", "text/html; charset=utf-8"),
    "/topic-article-demo.css": ("topic-article-demo.css", "text/css; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


def run_demo_server(
    database_path: Path,
    *,
    port: int = 8765,
    open_browser: bool = False,
) -> None:
    run_radar_server(
        database_path,
        port=port,
        open_browser=open_browser,
        workspace_label="Nutrition Signal Radar Demo",
        workspace_kind="demo",
        missing_database_hint="run demo import-xlsx and demo build first",
    )


def run_radar_server(
    database_path: Path,
    *,
    port: int = 8765,
    open_browser: bool = False,
    workspace_label: str = "Nutrition Signal Radar",
    workspace_kind: str = "formal",
    missing_database_hint: str = "run db init, configure sources, and sync first",
) -> None:
    """Serve either the formal or isolated demo workspace on localhost only."""

    database = database_path.resolve()
    if not database.is_file():
        raise FileNotFoundError(
            f"Radar database does not exist: {database}; {missing_database_hint}"
        )
    initialize_schema(database)
    server = create_demo_server(
        database,
        port=port,
        workspace_label=workspace_label,
        workspace_kind=workspace_kind,
    )
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"{workspace_label}: {url}")
    print("Press Ctrl+C to stop. The server only listens on 127.0.0.1.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def create_demo_server(
    database_path: Path,
    *,
    port: int = 0,
    workspace_label: str = "Nutrition Signal Radar Demo",
    workspace_kind: str = "demo",
) -> ThreadingHTTPServer:
    database = database_path.resolve()
    initialize_schema(database)
    handler = _handler_for(
        database,
        workspace_label=workspace_label,
        workspace_kind=workspace_kind,
    )
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def _handler_for(
    database_path: Path,
    *,
    workspace_label: str,
    workspace_kind: str,
) -> type[BaseHTTPRequestHandler]:
    handler_workspace_label = workspace_label
    handler_workspace_kind = workspace_kind

    class DemoHandler(BaseHTTPRequestHandler):
        server_version = "NutritionRadarDemo/0.1"
        workspace_label = handler_workspace_label
        workspace_kind = handler_workspace_kind

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path in STATIC_FILES:
                filename, content_type = STATIC_FILES[path]
                self._send_bytes((STATIC_DIR / filename).read_bytes(), content_type)
                return
            if path == "/api/health":
                self._send_json(
                    {
                        "status": "ok",
                        "database_path": str(database_path),
                        "workspace_label": self.workspace_label,
                        "workspace_kind": self.workspace_kind,
                    }
                )
                return
            if path == "/api/dashboard":
                cycle_slug = parse_qs(parsed.query).get("cycle", [None])[0]
                self._send_json(
                    _dashboard_payload(
                        database_path,
                        cycle_slug=cycle_slug,
                        workspace_label=self.workspace_label,
                        workspace_kind=self.workspace_kind,
                    )
                )
                return
            if path == "/api/research-tasks":
                cycle_slug = parse_qs(parsed.query).get("cycle", [None])[0]
                dashboard = _dashboard_payload(
                    database_path,
                    cycle_slug=cycle_slug,
                    workspace_label=self.workspace_label,
                    workspace_kind=self.workspace_kind,
                )
                self._send_json({"research_tasks": dashboard["research_tasks"]})
                return
            brief_match = re.fullmatch(r"/api/cycles/(\d+)/weekly-briefs", path)
            if brief_match:
                cycle_id = int(brief_match.group(1))
                self._send_json(
                    {
                        "cycle_id": cycle_id,
                        "briefs": list_weekly_signal_briefs(
                            database_path, cycle_id=cycle_id
                        ),
                    }
                )
                return
            brief_version_match = re.fullmatch(
                r"/api/cycles/(\d+)/weekly-briefs/(\d+)", path
            )
            if brief_version_match:
                payload = read_weekly_signal_brief(
                    database_path,
                    cycle_id=int(brief_version_match.group(1)),
                    version=int(brief_version_match.group(2)),
                )
                if payload is None:
                    self._send_json(
                        {"error": "weekly brief not found"}, HTTPStatus.NOT_FOUND
                    )
                else:
                    self._send_json(payload)
                return
            article_match = re.fullmatch(r"/api/cycles/(\d+)/weekly-articles", path)
            if article_match:
                cycle_id = int(article_match.group(1))
                self._send_json(
                    {
                        "cycle_id": cycle_id,
                        "articles": list_weekly_articles(
                            database_path, cycle_id=cycle_id
                        ),
                    }
                )
                return
            article_version_match = re.fullmatch(
                r"/api/cycles/(\d+)/weekly-articles/(\d+)", path
            )
            if article_version_match:
                payload = read_weekly_article(
                    database_path,
                    cycle_id=int(article_version_match.group(1)),
                    version=int(article_version_match.group(2)),
                )
                if payload is None:
                    self._send_json(
                        {"error": "weekly article not found"}, HTTPStatus.NOT_FOUND
                    )
                else:
                    self._send_json(payload)
                return
            match = re.fullmatch(r"/api/candidates/(\d+)", path)
            if match:
                payload = _candidate_payload(database_path, int(match.group(1)))
                if payload is None:
                    self._send_json({"error": "candidate not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._send_json(payload)
                return
            match = re.fullmatch(r"/api/candidates/(\d+)/research-task", path)
            if match:
                payload = get_research_task(
                    database_path, candidate_id=int(match.group(1))
                )
                if payload is None:
                    self._send_json(
                        {"error": "research task not found"}, HTTPStatus.NOT_FOUND
                    )
                else:
                    self._send_json({"research_task": payload})
                return
            match = re.fullmatch(r"/api/research-tasks/(\d+)", path)
            if match:
                payload = get_research_task(
                    database_path, task_id=int(match.group(1))
                )
                if payload is None:
                    self._send_json(
                        {"error": "research task not found"}, HTTPStatus.NOT_FOUND
                    )
                else:
                    self._send_json({"research_task": payload})
                return
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            try:
                if path == "/api/cycles":
                    body = self._read_json()
                    result = create_monitoring_cycle(
                        database_path,
                        slug=str(body.get("slug", "")),
                        label=str(body.get("label", "")),
                        start_date=str(body.get("start_date", "")),
                        end_date=str(body.get("end_date", "")),
                        data_cutoff=(str(body.get("data_cutoff", "")).strip() or None),
                        actor="demo_operator",
                    )
                    self._send_json({"cycle": result.__dict__}, HTTPStatus.CREATED)
                    return
                cycle_match = re.fullmatch(r"/api/cycles/(\d+)/(close|export)", path)
                if cycle_match:
                    body = self._read_json()
                    cycle_id = int(cycle_match.group(1))
                    operation = cycle_match.group(2)
                    if operation == "close":
                        result = close_monitoring_cycle(
                            database_path,
                            cycle_id,
                            reason=str(body.get("reason", "")),
                            actor="demo_operator",
                        )
                        payload = {"cycle": result.__dict__}
                    else:
                        export = export_cycle_markdown_archive(
                            database_path,
                            cycle_id,
                            actor="demo_operator",
                        )
                        payload = {
                            "export_id": export.export_id,
                            "cycle_id": export.cycle_id,
                            "cycle_slug": export.cycle_slug,
                            "version": export.version,
                            "directory": str(export.directory),
                            "manifest_path": str(export.manifest_path),
                            "files": [str(path) for path in export.files],
                        }
                    self._send_json(payload)
                    return
                brief_export_match = re.fullmatch(
                    r"/api/cycles/(\d+)/weekly-briefs", path
                )
                if brief_export_match:
                    cycle_id = int(brief_export_match.group(1))
                    cycle_connection = connect_database(database_path, readonly=True)
                    try:
                        cycle = get_monitoring_cycle(
                            cycle_connection, cycle_id=cycle_id
                        )
                    finally:
                        cycle_connection.close()
                    if cycle is None:
                        raise MonitoringCycleError("监测期次不存在")
                    brief = export_weekly_signal_brief(
                        database_path,
                        cycle_slug=str(cycle["slug"]),
                        actor="demo_operator",
                    )
                    self._send_json(brief.as_dict(), HTTPStatus.CREATED)
                    return
                article_mutation_match = re.fullmatch(
                    r"/api/cycles/(\d+)/weekly-articles/(\d+)/(confirm|reopen)", path
                )
                if article_mutation_match:
                    cycle_id = int(article_mutation_match.group(1))
                    version = int(article_mutation_match.group(2))
                    operation = article_mutation_match.group(3)
                    cycle_connection = connect_database(database_path, readonly=True)
                    try:
                        cycle = get_monitoring_cycle(
                            cycle_connection, cycle_id=cycle_id
                        )
                    finally:
                        cycle_connection.close()
                    if cycle is None:
                        raise MonitoringCycleError("监测期次不存在")
                    if operation == "confirm":
                        mutation = confirm_weekly_article(
                            database_path,
                            cycle_id=cycle_id,
                            version=version,
                            actor="demo_operator",
                        )
                    else:
                        mutation = reopen_weekly_article(
                            database_path,
                            cycle_id=cycle_id,
                            version=version,
                            actor="demo_operator",
                        )
                    self._send_json(mutation.as_dict())
                    return
            except MonitoringCycleError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            except WeeklyArticleError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, OSError) as exc:
                self._send_json({"error": f"invalid request: {exc}"}, HTTPStatus.BAD_REQUEST)
                return

            create_task_match = re.fullmatch(
                r"/api/candidates/(\d+)/research-task", path
            )
            task_match = re.fullmatch(
                r"/api/research-tasks/(\d+)/(update|confirm|run-offline-demo|regenerate-offline-demo|finalize)",
                path,
            )
            revision_match = re.fullmatch(
                r"/api/research-tasks/(\d+)/drafts/(\d+)/revise", path
            )
            if create_task_match or task_match or revision_match:
                try:
                    body = self._read_json()
                    if create_task_match:
                        candidate_id = int(create_task_match.group(1))
                        result = create_research_task(
                            database_path,
                            candidate_id,
                            actor="demo_operator",
                        )
                        task = get_research_task(
                            database_path, task_id=result.task_id
                        )
                        self._send_json(
                            {"research_task": task, "created": result.created},
                            HTTPStatus.CREATED if result.created else HTTPStatus.OK,
                        )
                        return

                    if revision_match:
                        task_id = int(revision_match.group(1))
                        draft_id = int(revision_match.group(2))
                        result = save_draft_revision(
                            database_path,
                            task_id,
                            draft_id,
                            title=str(body.get("title", "")),
                            article_body=str(body.get("article_body", "")),
                            key_judgment=str(body.get("key_judgment", "")),
                            action_implications=str(
                                body.get("action_implications", "")
                            ),
                            uncertainty_notes=str(
                                body.get("uncertainty_notes", "")
                            ),
                            revision_reason=str(body.get("revision_reason", "")),
                            actor="demo_operator",
                        )
                    else:
                        task_id = int(task_match.group(1))
                        operation = task_match.group(2)
                        if operation == "update":
                            dimensions = body.get("selection_dimensions", [])
                            if not isinstance(dimensions, list):
                                raise ResearchWorkflowError(
                                    "selection_dimensions must be a list"
                                )
                            result = update_research_task(
                                database_path,
                                task_id,
                                research_question=str(body.get("research_question", "")),
                                audience=str(body.get("audience", "")),
                                decision_use=str(body.get("decision_use", "")),
                                scope=str(body.get("scope", "")),
                                geography=str(body.get("geography", "")),
                                time_range=str(body.get("time_range", "")),
                                exclusions=str(body.get("exclusions", "")),
                                selection_dimensions=[str(value) for value in dimensions],
                                evidence_gaps=str(body.get("evidence_gaps", "")),
                                output_requirements=str(
                                    body.get("output_requirements", "")
                                ),
                                actor="demo_operator",
                            )
                        elif operation == "confirm":
                            result = confirm_research_task(
                                database_path,
                                task_id,
                                actor="demo_operator",
                            )
                        elif operation == "run-offline-demo":
                            result = run_offline_draft_demo(
                                database_path,
                                task_id,
                                actor="demo_operator",
                            )
                        elif operation == "regenerate-offline-demo":
                            result = regenerate_offline_draft_demo(
                                database_path,
                                task_id,
                                revision_reason=str(body.get("revision_reason", "")),
                                actor="demo_operator",
                            )
                        else:
                            result = finalize_research_draft(
                                database_path,
                                task_id,
                                int(body.get("draft_id", 0)),
                                review_note=str(body.get("review_note", "")),
                                actor="demo_operator",
                            )
                    task = get_research_task(database_path, task_id=result.task_id)
                    mutation = {
                        "draft_id": getattr(result, "draft_id", None),
                        "version": getattr(result, "version", None),
                        "draft_status": getattr(result, "draft_status", None),
                        "final_markdown_path": (
                            str(result.final_markdown_path)
                            if getattr(result, "final_markdown_path", None)
                            else None
                        ),
                    }
                    self._send_json(
                        {
                            "research_task": task,
                            "changed_fields": list(
                                getattr(result, "changed_fields", ())
                            ),
                            "mutation": mutation,
                        }
                    )
                    return
                except ResearchWorkflowError as exc:
                    self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                    self._send_json(
                        {"error": f"invalid request: {exc}"},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return

            match = re.fullmatch(
                r"/api/candidates/(\d+)/(transition|track|edit|package)", path
            )
            if not match:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            try:
                body = self._read_json()
                candidate_id = int(match.group(1))
                operation = match.group(2)
                if operation == "transition":
                    result = transition_candidate(
                        database_path,
                        candidate_id,
                        str(body.get("to_status", "")),
                        reason=str(body.get("reason", "")),
                        actor="demo_operator",
                    )
                    payload = {
                        "candidate_id": result.candidate_id,
                        "from_status": result.from_status,
                        "to_status": result.to_status,
                        "package_id": result.package_id,
                        "package_status": result.package_status,
                    }
                elif operation == "track":
                    result = set_candidate_review_track(
                        database_path,
                        candidate_id,
                        str(body.get("review_track", "")),
                        reason=str(body.get("reason", "")),
                        actor="demo_operator",
                    )
                    payload = {
                        "candidate_id": result.candidate_id,
                        "from_track": result.from_track,
                        "to_track": result.to_track,
                        "track_source": result.track_source,
                        "track_reason": result.track_reason,
                    }
                elif operation == "edit":
                    result = update_candidate_editorial_fields(
                        database_path,
                        candidate_id,
                        proposed_title=str(body.get("proposed_title", "")),
                        audience=str(body.get("audience", "")),
                        core_question=str(body.get("core_question", "")),
                        why_now=str(body.get("why_now", "")),
                        angle=str(body.get("angle", "")),
                        actor="demo_operator",
                    )
                    payload = {
                        "candidate_id": result.candidate_id,
                        "changed_fields": list(result.changed_fields),
                    }
                else:
                    dimensions = body.get("selection_dimensions", [])
                    if not isinstance(dimensions, list):
                        raise TopicWorkflowError("selection_dimensions must be a list")
                    result = update_topic_package(
                        database_path,
                        candidate_id,
                        title=str(body.get("title", "")),
                        audience=str(body.get("audience", "")),
                        core_question=str(body.get("core_question", "")),
                        why_now=str(body.get("why_now", "")),
                        industry_angle=str(body.get("industry_angle", "")),
                        selection_dimensions=[str(value) for value in dimensions],
                        evidence_gaps=str(body.get("evidence_gaps", "")),
                        research_directions=str(body.get("research_directions", "")),
                        risk_notes=str(body.get("risk_notes", "")),
                        actor="demo_operator",
                    )
                    payload = {
                        "candidate_id": result.candidate_id,
                        "package_id": result.package_id,
                        "changed_fields": list(result.changed_fields),
                        "missing_fields": list(result.missing_fields),
                        "missing_field_labels": [
                            PACKAGE_FIELD_LABELS[field]
                            for field in result.missing_fields
                        ],
                    }
            except (TopicWorkflowError, MonitoringCycleError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                self._send_json({"error": f"invalid request: {exc}"}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(payload)

        def _read_json(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length", "0")
            length = int(raw_length)
            if length <= 0 or length > 65_536:
                raise ValueError("JSON body must be between 1 and 65536 bytes")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload

        def _send_json(
            self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8", status)

        def _send_bytes(
            self,
            body: bytes,
            content_type: str,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            print(f"[demo] {self.address_string()} - {format % args}")

    return DemoHandler


def _dashboard_payload(
    database_path: Path,
    *,
    cycle_slug: str | None = None,
    workspace_label: str = "Nutrition Signal Radar Demo",
    workspace_kind: str = "demo",
) -> dict[str, Any]:
    cycles = list_monitoring_cycles(database_path)
    for cycle in cycles:
        relative_path = cycle.get("latest_export_path")
        cycle["latest_export_absolute_path"] = (
            str((database_path.parent / str(relative_path)).resolve())
            if relative_path
            else None
        )

    registry_accounts = [
        account.as_dict() for account in list_source_accounts(database_path)
    ]
    priority_sites = [site.as_dict() for site in list_priority_sites(database_path)]
    source_registry = {
        "accounts": registry_accounts,
        "priority_sites": priority_sites,
        "priority_site_enabled_count": sum(
            bool(site["enabled"]) for site in priority_sites
        ),
    }
    workspace_notice = (
        "当前看板只用于验证主题聚合、选题筛选与人工审核；"
        "公众号材料仅作为线索，事实性主张仍需补充一手来源。"
        if workspace_kind == "demo"
        else "来源文章仅作发现线索；行业信号与选题由规则生成并等待人工确认，"
        "事实性主张须回到一手来源补证。"
    )

    connection = connect_database(database_path, readonly=True)
    try:
        requested_missing = bool(cycle_slug) and not any(
            cycle["slug"] == cycle_slug for cycle in cycles
        )
        cycle_row = get_monitoring_cycle(
            connection,
            slug=None if requested_missing else cycle_slug,
        )
        if cycle_row is None:
            return {
                "demo": {
                    "label": "尚无监测期次",
                    "notice": workspace_notice,
                    "first_published_at": None,
                    "last_published_at": None,
                    "generator": DEMO_GENERATOR,
                    "workspace_label": workspace_label,
                    "workspace_kind": workspace_kind,
                },
                "active_cycle": None,
                "cycles": cycles,
                "requested_cycle_missing": requested_missing,
                "stats": _empty_stats(),
                "status_counts": {},
                "allowed_transitions": ALLOWED_TRANSITIONS,
                "candidates": [],
                "signals": [],
                "sources": [],
                "source_registry": source_registry,
                "research_tasks": [],
            }

        cycle_id = int(cycle_row["id"])
        active_cycle = next(
            (cycle for cycle in cycles if int(cycle["id"]) == cycle_id),
            dict(cycle_row),
        )
        stats = {
            "articles": _scalar(
                connection,
                """
                SELECT COUNT(*) FROM monitoring_cycle_articles
                WHERE cycle_id = ? AND role = 'in_period'
                """,
                (cycle_id,),
            ),
            "context_articles": _scalar(
                connection,
                """
                SELECT COUNT(*) FROM monitoring_cycle_articles
                WHERE cycle_id = ? AND role = 'context'
                """,
                (cycle_id,),
            ),
            "accounts": _scalar(
                connection,
                """
                SELECT COUNT(DISTINCT sa.account_id)
                FROM monitoring_cycle_articles mca
                JOIN source_articles sa ON sa.id = mca.article_id
                WHERE mca.cycle_id = ? AND mca.role = 'in_period'
                """,
                (cycle_id,),
            ),
            "clusters": _scalar(
                connection,
                "SELECT COUNT(*) FROM topic_clusters WHERE cycle_id = ?",
                (cycle_id,),
            ),
            "signals": _scalar(
                connection,
                """
                SELECT COUNT(*) FROM industry_signals s
                JOIN topic_clusters tc ON tc.id = s.cluster_id
                WHERE tc.cycle_id = ?
                """,
                (cycle_id,),
            ),
            "candidates": _scalar(
                connection,
                """
                SELECT COUNT(*) FROM topic_candidates c
                JOIN industry_signals s ON s.id = c.signal_id
                JOIN topic_clusters tc ON tc.id = s.cluster_id
                WHERE tc.cycle_id = ?
                """,
                (cycle_id,),
            ),
            "research_queue": _scalar(
                connection,
                """
                SELECT COUNT(*) FROM topic_candidates c
                JOIN industry_signals s ON s.id = c.signal_id
                JOIN topic_clusters tc ON tc.id = s.cluster_id
                WHERE tc.cycle_id = ? AND c.review_track = 'research'
                  AND c.status != 'archived'
                """,
                (cycle_id,),
            ),
            "watch_queue": _scalar(
                connection,
                """
                SELECT COUNT(*) FROM topic_candidates c
                JOIN industry_signals s ON s.id = c.signal_id
                JOIN topic_clusters tc ON tc.id = s.cluster_id
                WHERE tc.cycle_id = ? AND c.review_track = 'watch'
                  AND c.status != 'archived'
                """,
                (cycle_id,),
            ),
            "pending_decision": _scalar(
                connection,
                """
                SELECT COUNT(*) FROM topic_candidates c
                JOIN industry_signals s ON s.id = c.signal_id
                JOIN topic_clusters tc ON tc.id = s.cluster_id
                WHERE tc.cycle_id = ? AND c.status = 'pending_decision'
                """,
                (cycle_id,),
            ),
            "selected": _scalar(
                connection,
                """
                SELECT COUNT(*) FROM topic_candidates c
                JOIN industry_signals s ON s.id = c.signal_id
                JOIN topic_clusters tc ON tc.id = s.cluster_id
                WHERE tc.cycle_id = ? AND c.status = 'selected'
                """,
                (cycle_id,),
            ),
        }
        date_row = connection.execute(
            """
            SELECT MIN(sa.published_at) AS first_at, MAX(sa.published_at) AS last_at
            FROM monitoring_cycle_articles mca
            JOIN source_articles sa ON sa.id = mca.article_id
            WHERE mca.cycle_id = ? AND mca.role = 'in_period'
            """,
            (cycle_id,),
        ).fetchone()
        status_counts = {
            str(row["status"]): int(row["count"])
            for row in connection.execute(
                """
                SELECT c.status, COUNT(*) AS count
                FROM topic_candidates c
                JOIN industry_signals s ON s.id = c.signal_id
                JOIN topic_clusters tc ON tc.id = s.cluster_id
                WHERE tc.cycle_id = ?
                GROUP BY c.status
                """,
                (cycle_id,),
            )
        }
        candidates = [
            _candidate_summary(row)
            for row in connection.execute(
                """
                SELECT c.id, c.proposed_title, c.audience, c.why_now, c.score,
                       c.score_reason, c.risk_flags_json, c.status, c.archived_reason,
                       c.review_track, c.track_source, c.track_reason, c.editor_modified,
                       p.title AS package_title, p.status AS package_status,
                       s.article_count, s.independent_source_count, s.trend_state,
                       s.evidence_status, tc.source_count, tc.last_seen_at,
                       mc.id AS cycle_id, mc.slug AS cycle_slug,
                       mc.status AS cycle_status
                FROM topic_candidates c
                JOIN industry_signals s ON s.id = c.signal_id
                JOIN topic_clusters tc ON tc.id = s.cluster_id
                JOIN monitoring_cycles mc ON mc.id = tc.cycle_id
                LEFT JOIN topic_packages p ON p.candidate_id = c.id
                WHERE tc.cycle_id = ?
                ORDER BY c.score DESC, c.id
                LIMIT 300
                """,
                (cycle_id,),
            )
        ]
        signals = [
            _signal_summary(row)
            for row in connection.execute(
                """
                WITH ranked_candidates AS (
                    SELECT c.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY c.signal_id ORDER BY c.score DESC, c.id
                           ) AS row_number
                    FROM topic_candidates c
                )
                SELECT s.id, s.signal_title, s.underlying_event, s.trend_state,
                       s.article_count, s.independent_source_count,
                       s.evidence_status, s.consensus, s.data_gaps,
                       tc.source_count, tc.last_seen_at,
                       c.score AS top_score, c.id AS candidate_id,
                       c.review_track, c.track_source, c.track_reason,
                       mc.status AS cycle_status
                FROM industry_signals s
                JOIN topic_clusters tc ON tc.id = s.cluster_id
                JOIN monitoring_cycles mc ON mc.id = tc.cycle_id
                LEFT JOIN ranked_candidates c
                    ON c.signal_id = s.id AND c.row_number = 1
                WHERE tc.cycle_id = ?
                ORDER BY c.score DESC, s.id
                LIMIT 200
                """,
                (cycle_id,),
            )
        ]
        sources = [
            dict(row)
            for row in connection.execute(
                """
                SELECT a.id, a.name, a.segment, a.enabled, a.sync_health,
                       COUNT(s.id) AS article_count,
                       MIN(s.published_at) AS first_published_at,
                       MAX(s.published_at) AS last_published_at
                FROM source_accounts a
                JOIN source_articles s ON s.account_id = a.id
                JOIN monitoring_cycle_articles mca ON mca.article_id = s.id
                WHERE mca.cycle_id = ? AND mca.role = 'in_period'
                GROUP BY a.id
                ORDER BY article_count DESC, a.name
                """,
                (cycle_id,),
            )
        ]
        research_tasks = list_research_tasks(database_path, cycle_id=cycle_id)
        return {
            "demo": {
                "label": str(active_cycle.get("label") or active_cycle.get("slug")),
                "notice": workspace_notice,
                "first_published_at": date_row["first_at"],
                "last_published_at": date_row["last_at"],
                "generator": DEMO_GENERATOR,
                "workspace_label": workspace_label,
                "workspace_kind": workspace_kind,
            },
            "active_cycle": active_cycle,
            "cycles": cycles,
            "requested_cycle_missing": requested_missing,
            "stats": stats,
            "status_counts": status_counts,
            "allowed_transitions": ALLOWED_TRANSITIONS,
            "candidates": candidates,
            "signals": signals,
            "sources": sources,
            "source_registry": source_registry,
            "research_tasks": research_tasks,
        }
    finally:
        connection.close()


def _candidate_payload(database_path: Path, candidate_id: int) -> dict[str, Any] | None:
    connection = connect_database(database_path, readonly=True)
    try:
        row = connection.execute(
            """
            SELECT c.*, s.signal_title, s.underlying_event, s.consensus,
                   s.disagreements, s.evidence_status AS signal_evidence_status,
                   s.affected_actors_json, s.business_implications, s.data_gaps,
                   s.article_count, s.independent_source_count, s.trend_state,
                   s.cluster_id, tc.source_count, tc.first_seen_at, tc.last_seen_at,
                   tc.canonical_name, mc.id AS cycle_id, mc.slug AS cycle_slug,
                   mc.label AS cycle_label, mc.status AS cycle_status
            FROM topic_candidates c
            JOIN industry_signals s ON s.id = c.signal_id
            JOIN topic_clusters tc ON tc.id = s.cluster_id
            JOIN monitoring_cycles mc ON mc.id = tc.cycle_id
            WHERE c.id = ?
            """,
            (candidate_id,),
        ).fetchone()
        if row is None:
            return None
        candidate = dict(row)
        for key in (
            "score_breakdown_json",
            "risk_flags_json",
            "affected_actors_json",
        ):
            candidate[key.removesuffix("_json")] = _json_value(candidate.pop(key), [] if key != "score_breakdown_json" else {})

        package_row = connection.execute(
            "SELECT * FROM topic_packages WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        package = dict(package_row) if package_row is not None else None
        if package is not None:
            for key, default in (
                ("selection_dimensions_json", []),
                ("evidence_json", []),
            ):
                package[key.removesuffix("_json")] = _json_value(package.pop(key), default)
            missing_fields = topic_package_missing_fields(package)
            package["missing_fields"] = list(missing_fields)
            package["missing_field_labels"] = [
                PACKAGE_FIELD_LABELS[field] for field in missing_fields
            ]
            package["can_submit"] = not missing_fields

        articles = [
            dict(article)
            for article in connection.execute(
                """
                SELECT a.id, a.title, a.url, a.published_at, a.read_count,
                       a.like_count, a.share_count, a.recommend_count,
                       sa.name AS account_name, ca.similarity, ca.is_primary_evidence
                FROM cluster_articles ca
                JOIN source_articles a ON a.id = ca.article_id
                JOIN source_accounts sa ON sa.id = a.account_id
                WHERE ca.cluster_id = ?
                ORDER BY ca.is_primary_evidence DESC, a.read_count DESC, a.id
                """,
                (candidate["cluster_id"],),
            )
        ]
        events = []
        for event in connection.execute(
            """
            SELECT event_type, actor, from_status, to_status, reason,
                   payload_json, created_at
            FROM topic_events WHERE candidate_id = ? ORDER BY id DESC LIMIT 30
            """,
            (candidate_id,),
        ):
            payload = dict(event)
            payload["payload"] = _json_value(payload.pop("payload_json"), {})
            events.append(payload)
        return {
            "candidate": candidate,
            "package": package,
            "research_task": get_research_task(
                database_path, candidate_id=candidate_id
            ),
            "article_demo_url": _article_demo_url(
                candidate,
                package_title=package["title"] if package else None,
            ),
            "articles": articles,
            "events": events,
            "allowed_transitions": (
                list(ALLOWED_TRANSITIONS.get(candidate["status"], ()))
                if candidate["cycle_status"] == "open"
                else []
            ),
        }
    finally:
        connection.close()


def _candidate_summary(row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    payload["risk_flags"] = _json_value(payload.pop("risk_flags_json"), [])
    payload["article_demo_url"] = _article_demo_url(
        payload,
        package_title=payload.get("package_title"),
    )
    return payload


def _article_demo_url(
    candidate: Mapping[str, Any] | dict[str, Any],
    *,
    package_title: str | None = None,
) -> str | None:
    if candidate.get("status") != "selected":
        return None
    title = " ".join(
        value
        for value in (
            str(package_title or "").strip(),
            str(candidate.get("proposed_title") or "").strip(),
        )
        if value
    )
    if "注册检验" in title and "工艺验证批次" in title:
        return "/topic-article-demo.html"
    return None


def _signal_summary(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _json_value(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _empty_stats() -> dict[str, int]:
    return {
        "articles": 0,
        "context_articles": 0,
        "accounts": 0,
        "clusters": 0,
        "signals": 0,
        "candidates": 0,
        "research_queue": 0,
        "watch_queue": 0,
        "pending_decision": 0,
        "selected": 0,
    }


def _scalar(
    connection: sqlite3.Connection,
    query: str,
    parameters: tuple[object, ...] = (),
) -> int:
    return int(connection.execute(query, parameters).fetchone()[0])
