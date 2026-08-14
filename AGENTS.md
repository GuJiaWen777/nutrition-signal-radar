# Agent Workflow

These instructions apply to the entire `nutrition-signal-radar` repository.

- Read `README.md`, `DESIGN_DRAFT.md`, `CONTEXT.md`, and `IMPLEMENTATION_PLAN.md` before implementation work.
- Keep every new configuration, source file, test, database, runtime artifact, and command inside this repository.
- This repository is independent of the historical `SentimentTracker` / Weekly Publisher codebase. Do not edit it, write into it, or reuse its runtime state unless the user gives separate explicit approval.
- Do not add any WeChat publishing, draft-box, or mass-send path.
- Do not store credentials in code, SQLite, logs, fixtures, documentation, or version control.
- Treat DeepSeek as a configurable OpenAI-compatible analysis backend, not as proof that the research workflow has public-web access. Phase 2 still requires an Agent source-retrieval smoke test.
- Default to mocked RedFox and Agent calls in tests. Run live external calls only through an explicit live-validation command when credentials are configured.
- Preserve human gates. AI scores may rank and explain but may not automatically archive, approve, start unconfirmed research, or publish.
- Treat `var/` as local runtime state and keep it out of version control.
- Implement in the order defined by `IMPLEMENTATION_PLAN.md`; do not start Phase 2 before the 0.1 exit criteria are met.
- Any future shared-module extraction, cross-project state reuse, or file edit outside this repository requires a new explicit decision.
