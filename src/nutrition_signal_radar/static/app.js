const STATUS = {
  candidate_pool: "候选池",
  pending_decision: "待决策",
  package_drafting: "选题包完善",
  pending_confirmation: "待确认",
  selected: "已选题",
  archived: "已归档",
};

const ACTION = {
  candidate_pool: "放回候选池",
  pending_decision: "开始判断",
  package_drafting: "开始完善选题包",
  pending_confirmation: "提交人工确认",
  selected: "确认选题",
  archived: "归档 / 不做",
};

const TRACK = {
  research: "值得研究",
  watch: "传播观察",
};

const VALIDATION = {
  pending: "待校验",
  matched: "已匹配",
  not_found: "未收录",
  error: "校验失败",
  paused: "已暂停",
};

const TOPIC_DISPLAY_STAGES = [
  {
    key: "judging",
    label: "待判断",
    description: "筛选、立项与选题包完善",
    statuses: ["candidate_pool", "pending_decision", "package_drafting"],
  },
  {
    key: "confirming",
    label: "待确认",
    description: "完整选题包等待人工确认",
    statuses: ["pending_confirmation"],
  },
  {
    key: "selected",
    label: "已选题",
    description: "可继续创建研究任务书",
    statuses: ["selected"],
  },
];

const RESEARCH_DISPLAY_STAGES = [
  {
    key: "brief",
    label: "任务书",
    statuses: ["task_drafting", "pending_start"],
  },
  {
    key: "draft",
    label: "稿件",
    statuses: ["researching", "draft_review"],
  },
  {
    key: "final",
    label: "已定稿",
    statuses: ["completed"],
  },
];

const state = {
  data: null,
  activeCycleSlug: null,
  weeklyBriefs: [],
  activeBriefVersion: null,
  candidateQuery: "",
  signalQuery: "",
  candidateTrack: "research",
  signalTrack: "research",
  candidatePoolExpanded: false,
};

document.addEventListener("DOMContentLoaded", () => {
  bindNavigation();
  bindCycleControls();
  document.querySelector("#candidateSearch").addEventListener("input", (event) => {
    state.candidateQuery = event.target.value.trim().toLowerCase();
    state.candidatePoolExpanded = false;
    renderBoard();
  });
  document.querySelector("#signalSearch").addEventListener("input", (event) => {
    state.signalQuery = event.target.value.trim().toLowerCase();
    renderSignals();
  });
  document.querySelectorAll("[data-signal-track]").forEach((button) => {
    button.addEventListener("click", () => setSignalTrack(button.dataset.signalTrack));
  });
  document.querySelectorAll("[data-candidate-track]").forEach((button) => {
    button.addEventListener("click", () => setCandidateTrackFilter(button.dataset.candidateTrack));
  });
  document.querySelector("#closeDialog").addEventListener("click", () => {
    document.querySelector("#detailDialog").close();
  });
  document.querySelector("#detailDialog").addEventListener("click", (event) => {
    if (event.target.id === "detailDialog") event.target.close();
  });
  loadDashboard();
});

function bindCycleControls() {
  document.querySelector("#cycleSelect").addEventListener("change", (event) => {
    state.activeCycleSlug = event.target.value || null;
    state.candidatePoolExpanded = false;
    const detailDialog = document.querySelector("#detailDialog");
    if (detailDialog.open) detailDialog.close();
    loadDashboard();
  });
  document.querySelector("#exportCycle").addEventListener("click", exportActiveCycle);
  document.querySelector("#exportWeeklyBrief").addEventListener("click", exportWeeklyBrief);
  document.querySelector("#closeCycle").addEventListener("click", closeActiveCycle);
  document.querySelector("#newCycle").addEventListener("click", openNewCycleDialog);
  document.querySelector("#cancelCycle").addEventListener("click", () => {
    document.querySelector("#cycleDialog").close();
  });
  document.querySelector("#cycleForm").addEventListener("submit", createCycle);
  document.querySelector("#cycleDialog").addEventListener("click", (event) => {
    if (event.target.id === "cycleDialog") event.target.close();
  });
}

function bindNavigation() {
  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => switchTab(button.dataset.tab));
  });
  document.querySelectorAll("[data-go]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.setSignalTrack) setSignalTrack(button.dataset.setSignalTrack);
      switchTab(button.dataset.go);
    });
  });
}

function setSignalTrack(track) {
  state.signalTrack = track;
  document.querySelectorAll("[data-signal-track]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.signalTrack === track);
  });
  renderSignals();
}

function setCandidateTrackFilter(track) {
  state.candidateTrack = track;
  state.candidatePoolExpanded = false;
  document.querySelectorAll("[data-candidate-track]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.candidateTrack === track);
  });
  renderBoard();
}

function switchTab(tabId) {
  document.querySelectorAll(".tab").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.tab === tabId);
  });
  document.querySelectorAll(".panel").forEach((panel) => {
    panel.classList.toggle("is-active", panel.id === tabId);
  });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function loadDashboard() {
  try {
    const query = state.activeCycleSlug
      ? `?cycle=${encodeURIComponent(state.activeCycleSlug)}`
      : "";
    const response = await fetch(`/api/dashboard${query}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    state.activeCycleSlug = state.data.active_cycle?.slug || null;
    renderAll();
  } catch (error) {
    showToast(`无法读取 Demo：${error.message}`, true);
  }
}

function renderAll() {
  const { demo } = state.data;
  document.querySelector("#demoLabel").textContent = demo.label;
  document.querySelector("#demoDescription").textContent = demo.notice;
  const formalWorkspace = demo.workspace_kind === "formal";
  document.querySelector(".demo-pill").textContent = formalWorkspace
    ? "LOCAL WORKBENCH"
    : "LOCAL DEMO";
  document.title = `营养健康行业信号雷达 · ${formalWorkspace ? "本地工作台" : "Demo"}`;
  renderCycleControls();
  renderStats();
  renderTopSignals();
  renderSignals();
  renderBoard();
  renderResearchTasks();
  renderSources();
  renderWeeklyBrief();
}

function renderCycleControls() {
  const cycles = state.data.cycles || [];
  const active = state.data.active_cycle;
  const select = document.querySelector("#cycleSelect");
  select.innerHTML = cycles.length
    ? cycles.map((cycle) => `<option value="${escapeAttribute(cycle.slug)}"${cycle.slug === active?.slug ? " selected" : ""}>${escapeHtml(cycle.label)} · ${cycle.status === "open" ? "进行中" : "已关闭"}</option>`).join("")
    : '<option value="">尚无期次</option>';
  select.disabled = !cycles.length;

  const status = document.querySelector("#cycleStatus");
  status.textContent = active ? (active.status === "open" ? "进行中" : "已关闭 · 只读") : "尚无期次";
  status.classList.toggle("is-closed", active?.status === "closed");
  document.querySelector("#cycleWorkspace").classList.toggle("is-closed", active?.status === "closed");
  document.querySelector("#cycleDates").textContent = active
    ? `${formatDate(active.start_date)} — ${formatDate(active.end_date)}${active.data_cutoff ? ` · 截止 ${formatDateTime(active.data_cutoff)}` : ""}`
    : "先新建一个监测期次";

  const latestExport = active?.latest_export_absolute_path || active?.latest_export_path;
  document.querySelector("#cycleExportPath").innerHTML = latestExport
    ? `最近归档：<code>${escapeHtml(latestExport)}</code>`
    : (active ? "本期尚未导出选题归档" : "");

  const hasOpenCycle = cycles.some((cycle) => cycle.status === "open");
  document.querySelector("#exportCycle").disabled = !active;
  document.querySelector("#closeCycle").disabled = !active || active.status !== "open";
  document.querySelector("#newCycle").disabled = hasOpenCycle;
  document.querySelector("#newCycle").title = hasOpenCycle ? "请先关闭当前进行中的期次" : "创建一个空白期次";
}

function openNewCycleDialog() {
  const cycles = state.data?.cycles || [];
  if (cycles.some((cycle) => cycle.status === "open")) {
    showToast("请先关闭当前进行中的期次", true);
    return;
  }
  const form = document.querySelector("#cycleForm");
  form.reset();
  const latestEnd = cycles
    .map((cycle) => cycle.end_date)
    .filter(Boolean)
    .sort()
    .at(-1);
  const start = latestEnd ? addDays(latestEnd, 1) : toIsoDate(new Date());
  const end = addDays(start, 6);
  const week = isoWeek(start);
  form.elements.slug.value = `${week.year}-w${String(week.week).padStart(2, "0")}`;
  form.elements.label.value = `${week.year} 年第 ${week.week} 周`;
  form.elements.start_date.value = start;
  form.elements.end_date.value = end;
  document.querySelector("#cycleDialog").showModal();
}

async function createCycle(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submitButton = form.querySelector('button[type="submit"]');
  submitButton.disabled = true;
  const values = Object.fromEntries(new FormData(form).entries());
  if (!values.data_cutoff) delete values.data_cutoff;
  try {
    const response = await fetch("/api/cycles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    state.activeCycleSlug = payload.cycle.slug;
    document.querySelector("#cycleDialog").close();
    showToast(`已创建空白期次“${payload.cycle.label}”；导入数据后才会生成本期信号`);
    await loadDashboard();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    submitButton.disabled = false;
  }
}

async function closeActiveCycle() {
  const active = state.data?.active_cycle;
  if (!active || active.status !== "open") return;
  if (!window.confirm(`关闭“${active.label}”？关闭后本期信号和选题将只读，第一版不提供重新打开。`)) return;
  try {
    const response = await fetch(`/api/cycles/${active.id}/close`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: "人工关闭本期" }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    showToast(`已关闭“${active.label}”，历史内容现在只读`);
    await loadDashboard();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function exportActiveCycle() {
  const active = state.data?.active_cycle;
  if (!active) return;
  const button = document.querySelector("#exportCycle");
  button.disabled = true;
  try {
    const response = await fetch(`/api/cycles/${active.id}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    showToast(`已生成第 ${payload.version} 版选题归档：${payload.files?.length || 0} 个 Markdown / 清单文件`);
    await loadDashboard();
  } catch (error) {
    showToast(error.message, true);
    button.disabled = false;
  }
}

function renderStats() {
  const stats = state.data.stats;
  const items = [
    [stats.articles, "去重文章"],
    [stats.clusters, "主题簇"],
    [stats.signals, "行业信号"],
    [stats.research_queue, "值得研究"],
    [stats.watch_queue, "传播观察"],
    [stats.selected, "人工已选题"],
  ];
  document.querySelector("#statsGrid").innerHTML = items.map(([value, label]) => `
    <article class="stat-card"><strong>${number(value)}</strong><span>${escapeHtml(label)}</span></article>
  `).join("");
}

function renderTopSignals() {
  renderTopSignalList(
    "#researchSignals",
    state.data.signals.filter((signal) => signal.review_track === "research").slice(0, 4),
  );
  renderTopSignalList(
    "#watchSignals",
    state.data.signals.filter((signal) => signal.review_track === "watch").slice(0, 4),
  );
}

function renderTopSignalList(selector, signals) {
  const root = document.querySelector(selector);
  root.innerHTML = signals.length ? signals.map((signal) => `
    <div class="top-item" data-candidate-id="${signal.candidate_id}">
      <span class="score-orb">${Math.round(signal.top_score || 0)}</span>
      <div><strong>${escapeHtml(signal.signal_title)}</strong><small>${signal.article_count} 篇 · ${signal.source_count} 个公众号 · 独立来源估算 ${signal.independent_source_count}</small></div>
      <span class="chevron">›</span>
    </div>
  `).join("") : '<div class="empty-list">当前没有规则归入此队列的信号</div>';
  bindCandidateOpeners(root);
}

function renderSignals() {
  if (!state.data) return;
  const query = state.signalQuery;
  const signals = state.data.signals.filter((signal) => {
    const haystack = `${signal.signal_title} ${signal.underlying_event} ${signal.data_gaps}`.toLowerCase();
    const matchesTrack = state.signalTrack === "all" || signal.review_track === state.signalTrack;
    return matchesTrack && (!query || haystack.includes(query));
  });
  document.querySelector("#signalCount").textContent = `${signals.length} 个${state.signalTrack === "all" ? "" : TRACK[state.signalTrack]}信号`;
  document.querySelector("#signalGrid").innerHTML = signals.map((signal) => `
    <article class="signal-card">
      <div class="card-meta">
        <span class="badge ${signal.review_track === "research" ? "teal" : "watch"}">${TRACK[signal.review_track] || signal.review_track}</span>
        <span class="badge teal">${trendLabel(signal.trend_state)}</span>
        <span class="badge gold">待补证</span>
        <span class="badge">规则聚类</span>
      </div>
      <h3>${escapeHtml(signal.signal_title)}</h3>
      <p>${escapeHtml(signal.underlying_event)}</p>
      <p class="track-note">${escapeHtml(signal.track_reason)}</p>
      <footer>
        <span>${signal.article_count} 篇 · ${signal.source_count} 个号 · 独立来源估算 ${signal.independent_source_count}</span>
        <button class="secondary-button" data-candidate-id="${signal.candidate_id}">查看选题</button>
      </footer>
    </article>
  `).join("");
  bindCandidateOpeners(document.querySelector("#signalGrid"));
}

function renderBoard() {
  if (!state.data) return;
  const query = state.candidateQuery;
  const candidates = state.data.candidates.filter((candidate) => {
    const haystack = `${candidate.proposed_title} ${candidate.package_title || ""} ${candidate.audience} ${(candidate.risk_flags || []).join(" ")}`.toLowerCase();
    const matchesTrack = state.candidateTrack === "all" || candidate.review_track === state.candidateTrack;
    return matchesTrack && (!query || haystack.includes(query));
  });
  const active = candidates.filter((candidate) => candidate.status !== "archived");
  const archived = candidates.filter((candidate) => candidate.status === "archived");
  const candidatePoolTotal = active.filter((candidate) => candidate.status === "candidate_pool").length;
  const trackLabel = state.candidateTrack === "all" ? "全部队列" : TRACK[state.candidateTrack];
  const poolDisplayNote = !query && candidatePoolTotal > 10
    ? ` · 新候选先显示 10/${candidatePoolTotal}`
    : "";
  document.querySelector("#candidateCount").textContent = `${trackLabel} · ${active.length} 个在看板中${poolDisplayNote}`;
  document.querySelector("#archiveCount").textContent = `(${archived.length})`;
  document.querySelector("#topicBoard").innerHTML = TOPIC_DISPLAY_STAGES.map((stage) => {
    const cards = active.filter((candidate) => stage.statuses.includes(candidate.status));
    const isJudgingStage = stage.key === "judging";
    const inProgressCards = isJudgingStage
      ? cards.filter((candidate) => candidate.status !== "candidate_pool")
      : [];
    const candidatePoolCards = isJudgingStage
      ? cards.filter((candidate) => candidate.status === "candidate_pool")
      : [];
    const canToggleCandidatePool = isJudgingStage && !query && candidatePoolCards.length > 10;
    const visibleCards = isJudgingStage
      ? [
        ...inProgressCards,
        ...(canToggleCandidatePool && !state.candidatePoolExpanded
          ? candidatePoolCards.slice(0, 10)
          : candidatePoolCards),
      ]
      : cards;
    const toggle = canToggleCandidatePool
      ? `<button class="board-limit-toggle" type="button" data-candidate-pool-toggle aria-expanded="${state.candidatePoolExpanded}">${state.candidatePoolExpanded ? "收起新候选，只显示前 10 个" : `显示全部新候选（${candidatePoolCards.length}）`}</button>`
      : "";
    return `
      <section class="board-column">
        <header class="column-header">
          <div><span class="column-title">${stage.label}</span><small class="column-description">${stage.description}</small></div>
          <span class="column-count">${cards.length}</span>
        </header>
        <div class="column-cards">
          ${visibleCards.length ? visibleCards.map(topicCard).join("") : '<div class="empty-column">暂无卡片</div>'}
        </div>
        ${toggle}
      </section>
    `;
  }).join("");
  document.querySelector("#archiveList").innerHTML = archived.length ? archived.map((candidate) => `
    <div class="archive-item" data-candidate-id="${candidate.id}"><span>${escapeHtml(candidate.proposed_title)}</span><span>${escapeHtml(candidate.archived_reason || "未记录原因")}</span></div>
  `).join("") : '<div class="empty-column">暂无归档选题</div>';
  bindCandidateOpeners(document.querySelector("#topicBoard"));
  bindCandidateOpeners(document.querySelector("#archiveList"));
  const candidatePoolToggle = document.querySelector("[data-candidate-pool-toggle]");
  if (candidatePoolToggle) {
    candidatePoolToggle.addEventListener("click", () => {
      state.candidatePoolExpanded = !state.candidatePoolExpanded;
      renderBoard();
    });
  }
}

function topicCard(candidate) {
  const firstRisk = candidate.risk_flags?.[0] || "待人工判断";
  const displayTitle = candidate.package_title || candidate.proposed_title;
  return `
    <article class="topic-card" data-candidate-id="${candidate.id}">
      <div class="score-line"><span class="score-label">规则辅助分 ${Number(candidate.score || 0).toFixed(1)}</span><span class="badge ${candidate.review_track === "research" ? "teal" : "watch"}">${TRACK[candidate.review_track]}</span></div>
      <h3>${escapeHtml(displayTitle)}</h3>
      <p>${escapeHtml(candidate.audience)}</p>
      <div class="source-label">${candidate.source_count} 个号 / 独立估算 ${candidate.independent_source_count}</div>
      <div class="risk-line">⚑ ${escapeHtml(firstRisk)}</div>
      <div class="topic-next-step">下一步：${escapeHtml(topicNextStep(candidate.status))}</div>
      ${candidate.article_demo_url ? `
        <div class="card-action-row">
          <span>已选题结构样稿</span>
          <a class="card-demo-link" href="${escapeAttribute(candidate.article_demo_url)}" target="_blank" rel="noreferrer" data-article-demo>查看文章 Demo</a>
        </div>
      ` : ""}
    </article>
  `;
}

function renderSources() {
  const registry = state.data.source_registry || {};
  const accounts = registry.accounts || [];
  document.querySelector("#registryRows").innerHTML = accounts.length
    ? accounts.map((source) => `
      <tr>
        <td>
          <strong>${escapeHtml(source.name)}</strong>
          ${source.redfox_name && source.redfox_name !== source.name ? `<small>${escapeHtml(source.redfox_name)}</small>` : ""}
        </td>
        <td><span class="badge ${source.enabled ? "teal" : ""}">${source.enabled ? "已启用" : "未启用"}</span></td>
        <td>${escapeHtml(VALIDATION[source.validation_status] || source.validation_status)}</td>
        <td>${escapeHtml(source.redfox_library || "quality")}</td>
        <td>${Number(source.priority)}</td>
        <td>${escapeHtml(source.sync_health || "—")}${source.last_synced_at ? ` · ${formatDate(source.last_synced_at)}` : ""}</td>
      </tr>
    `).join("")
    : '<tr><td colspan="6" class="empty-cell">尚未配置监测账号；可在 CLI 中用 sources configure-account 建立提案。</td></tr>';
  const prioritySites = registry.priority_sites || [];
  document.querySelector("#prioritySiteSummary").textContent = prioritySites.length
    ? `重点网站清单：${prioritySites.length} 条（已启用 ${registry.priority_site_enabled_count || 0}）；只影响研究检索优先级，不限制公开互联网范围。`
    : "重点网站清单尚未导入；可在 CLI 中用 sources seed-priority-sites 建立初始草案。";

  document.querySelector("#sourceRows").innerHTML = state.data.sources.map((source) => `
    <tr>
      <td><strong>${escapeHtml(source.name)}</strong></td>
      <td>${number(source.article_count)}</td>
      <td>${formatDate(source.first_published_at)} — ${formatDate(source.last_published_at)}</td>
      <td><span class="badge teal">${escapeHtml(source.sync_health === "healthy" ? "已导入" : source.sync_health)}</span></td>
    </tr>
  `).join("");
}

async function renderWeeklyBrief() {
  const active = state.data?.active_cycle;
  const versionList = document.querySelector("#weeklyVersionList");
  const meta = document.querySelector("#weeklyMeta");
  if (!active) {
    versionList.innerHTML = '<div class="empty-column">请先选择一个监测期次。</div>';
    document.querySelector("#weeklyVersionCount").textContent = "";
    document.querySelector("#weeklyContent").innerHTML = "";
    document.querySelector("#weeklyContentMeta").textContent = "";
    meta.textContent = "尚无期次";
    return;
  }
  meta.textContent = `${active.label} · ${active.status === "open" ? "进行中" : "已关闭"}`;
  try {
    const response = await fetch(`/api/cycles/${active.id}/weekly-briefs`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    state.weeklyBriefs = payload.briefs || [];
  } catch (error) {
    state.weeklyBriefs = [];
    showToast(`无法读取本周信号快报：${error.message}`, true);
  }
  document.querySelector("#weeklyVersionCount").textContent =
    state.weeklyBriefs.length
      ? `${state.weeklyBriefs.length} 个版本`
      : "尚无版本";
  versionList.innerHTML = state.weeklyBriefs.length
    ? state.weeklyBriefs.map((brief) => {
        const selected = brief.version === state.activeBriefVersion ? " is-active" : "";
        return `
          <button class="brief-version-chip${selected}" data-brief-version="${brief.version}">
            v${String(brief.version).padStart(3, "0")}
            <small>${formatDate(brief.created_at)} · ${brief.file_count} 个文件</small>
          </button>
        `;
      }).join("")
    : '<div class="empty-column">本期尚未导出信号快报；点击右上角按钮生成 v001。</div>';
  versionList.querySelectorAll("[data-brief-version]").forEach((button) => {
    button.addEventListener("click", () => viewWeeklyBrief(Number(button.dataset.briefVersion)));
  });
  if (!state.weeklyBriefs.some((brief) => brief.version === state.activeBriefVersion)) {
    state.activeBriefVersion = state.weeklyBriefs[0]?.version || null;
  }
  if (state.activeBriefVersion) {
    await viewWeeklyBrief(state.activeBriefVersion);
  } else {
    document.querySelector("#weeklyContent").innerHTML =
      '<div class="empty-column">快报内容将在这里展示。</div>';
    document.querySelector("#weeklyContentMeta").textContent = "";
  }
}

async function viewWeeklyBrief(version) {
  const active = state.data?.active_cycle;
  if (!active) return;
  state.activeBriefVersion = version;
  document.querySelectorAll("[data-brief-version]").forEach((button) => {
    button.classList.toggle("is-active", Number(button.dataset.briefVersion) === version);
  });
  try {
    const response = await fetch(`/api/cycles/${active.id}/weekly-briefs/${version}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const brief = await response.json();
    document.querySelector("#weeklyContentMeta").textContent =
      `v${String(brief.version).padStart(3, "0")} · ${formatDateTime(brief.created_at)} · 待人工审阅`;
    document.querySelector("#weeklyContent").innerHTML = brief.markdown
      ? renderBriefMarkdown(brief.markdown)
      : '<div class="empty-column">该版本缺少可读内容。</div>';
  } catch (error) {
    showToast(`无法打开快报 v${version}：${error.message}`, true);
  }
}

async function exportWeeklyBrief() {
  const active = state.data?.active_cycle;
  if (!active) {
    showToast("请先选择一个监测期次", true);
    return;
  }
  const button = document.querySelector("#exportWeeklyBrief");
  button.disabled = true;
  try {
    const response = await fetch(`/api/cycles/${active.id}/weekly-briefs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    showToast(`已导出快报 v${String(payload.version).padStart(3, "0")}，等待人工审阅`);
    await renderWeeklyBrief();
  } catch (error) {
    showToast(`导出失败：${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

function renderBriefMarkdown(markdown) {
  const lines = String(markdown || "").split(/\r?\n/);
  const html = [];
  let bulletBuffer = [];
  const flushBullets = () => {
    if (bulletBuffer.length) {
      html.push(`<ul>${bulletBuffer.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("")}</ul>`);
      bulletBuffer = [];
    }
  };
  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    if (/^- /.test(line)) {
      bulletBuffer.push(line.replace(/^- /, ""));
      continue;
    }
    flushBullets();
    if (/^#{1,4} /.test(line)) {
      const level = Math.min(line.match(/^#+/)[0].length + 1, 5);
      html.push(`<h${level}>${inlineMarkdown(line.replace(/^#+\s*/, ""))}</h${level}>`);
    } else if (/^> /.test(line)) {
      html.push(`<blockquote>${inlineMarkdown(line.replace(/^>\s*/, ""))}</blockquote>`);
    } else if (/^---+$/.test(line)) {
      html.push("<hr>");
    } else if (line.trim()) {
      html.push(`<p>${inlineMarkdown(line.trim())}</p>`);
    }
  }
  flushBullets();
  return html.join("");
}

function inlineMarkdown(text) {
  let html = escapeHtml(text);
  html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return html;
}

function renderResearchTasks() {
  const root = document.querySelector("#researchTaskList");
  const tasks = state.data.research_tasks || [];
  if (!tasks.length) {
    root.innerHTML = `
      <div class="research-empty-state">
        <strong>本期还没有研究任务</strong>
        <span>从已确认的选题详情中先生成并人工确认研究任务书；系统不会自动启动任何研究。</span>
      </div>
    `;
    return;
  }
  root.innerHTML = tasks.map((task) => {
    const stage = researchDisplayStage(task.status);
    const nextStep = researchProgressHint(task.status);
    const draftInfo = task.latest_draft_version
      ? `稿件 V${task.latest_draft_version} · ${draftStatus(task.latest_draft_status)}`
      : "尚无稿件";
    return `
      <article class="research-task-card" data-research-task-id="${task.id}">
        <div class="research-card-meta">
          <span class="status-chip">${stage}</span>
          <span class="badge ${task.status === "completed" ? "teal" : "gold"}">${escapeHtml(nextStep)}</span>
        </div>
        <h3>${escapeHtml(task.topic_title || "未命名选题")}</h3>
        <p>任务 #${task.id} · ${escapeHtml(task.cycle_label || task.cycle_slug || "未知期次")} · ${Number(task.draft_count || 0)} 个版本</p>
        <footer><span>${escapeHtml(draftInfo)}</span><button class="secondary-button" type="button">查看任务与稿件</button></footer>
      </article>
    `;
  }).join("");
  bindResearchTaskOpeners(root);
}

function bindResearchTaskOpeners(root) {
  root.querySelectorAll("[data-research-task-id]").forEach((element) => {
    element.addEventListener("click", () => {
      openResearchTask(Number(element.dataset.researchTaskId));
    });
  });
}

function bindCandidateOpeners(root) {
  root.querySelectorAll("[data-candidate-id]").forEach((element) => {
    element.addEventListener("click", (event) => {
      if (event.target.closest("[data-article-demo]")) return;
      openCandidate(Number(element.dataset.candidateId));
    });
  });
}

async function openCandidate(candidateId) {
  const dialog = document.querySelector("#detailDialog");
  const content = document.querySelector("#detailContent");
  content.innerHTML = '<p class="muted">正在读取选题详情…</p>';
  if (!dialog.open) dialog.showModal();
  try {
    const response = await fetch(`/api/candidates/${candidateId}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const detail = await response.json();
    content.innerHTML = detailHtml(detail);
    content.querySelectorAll("[data-transition]").forEach((button) => {
      button.addEventListener("click", () => transitionCandidate(candidateId, button.dataset.transition));
    });
    content.querySelectorAll("[data-track-action]").forEach((button) => {
      button.addEventListener("click", () => changeCandidateTrack(candidateId, button.dataset.trackAction));
    });
    const editorialForm = content.querySelector("#editorialForm");
    if (editorialForm) {
      editorialForm.addEventListener("submit", (event) => saveCandidateEdit(event, candidateId));
    }
    const packageForm = content.querySelector("#packageForm");
    if (packageForm) {
      packageForm.addEventListener("submit", (event) => saveTopicPackage(event, candidateId));
    }
    const createResearchButton = content.querySelector("[data-create-research-task]");
    if (createResearchButton) {
      createResearchButton.addEventListener("click", () => createResearchTask(candidateId));
    }
    const researchTaskForm = content.querySelector("#researchTaskForm");
    if (researchTaskForm) {
      researchTaskForm.addEventListener("submit", (event) => saveResearchTask(event, candidateId));
    }
    const runOfflineDemoButton = content.querySelector("[data-run-offline-demo]");
    if (runOfflineDemoButton) {
      runOfflineDemoButton.addEventListener("click", () => runOfflineDraftDemo(Number(runOfflineDemoButton.dataset.runOfflineDemo)));
    }
    const openResearchTaskButton = content.querySelector("[data-open-research-task]");
    if (openResearchTaskButton) {
      openResearchTaskButton.addEventListener("click", () => openResearchTask(Number(openResearchTaskButton.dataset.openResearchTask)));
    }
  } catch (error) {
    content.innerHTML = `<p>读取失败：${escapeHtml(error.message)}</p>`;
  }
}

async function openResearchTask(taskId) {
  const dialog = document.querySelector("#detailDialog");
  const content = document.querySelector("#detailContent");
  content.innerHTML = '<p class="muted">正在读取研究任务与稿件…</p>';
  if (!dialog.open) dialog.showModal();
  try {
    const response = await fetch(`/api/research-tasks/${taskId}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    const task = payload.research_task;
    content.innerHTML = researchTaskDetailHtml(task);
    const runButton = content.querySelector("[data-run-offline-demo]");
    if (runButton) {
      runButton.addEventListener("click", () => runOfflineDraftDemo(taskId));
    }
    const regenerateButton = content.querySelector("[data-regenerate-offline-demo]");
    if (regenerateButton) {
      regenerateButton.addEventListener("click", () => regenerateOfflineDraftDemo(taskId));
    }
    const revisionForm = content.querySelector("#draftRevisionForm");
    if (revisionForm) {
      revisionForm.addEventListener("submit", (event) => saveDraftRevision(event, taskId));
    }
    const finalizeButton = content.querySelector("[data-finalize-draft]");
    if (finalizeButton) {
      finalizeButton.addEventListener("click", () => finalizeResearchDraft(taskId, Number(finalizeButton.dataset.finalizeDraft)));
    }
  } catch (error) {
    content.innerHTML = `<p>读取失败：${escapeHtml(error.message)}</p>`;
  }
}

function detailHtml(detail) {
  const candidate = detail.candidate;
  const packageData = detail.package;
  const risks = candidate.risk_flags || [];
  const isCycleOpen = candidate.cycle_status === "open";
  const canEditCandidate = isCycleOpen && ["candidate_pool", "pending_decision"].includes(candidate.status);
  const canChangeTrack = isCycleOpen && !["selected", "archived"].includes(candidate.status);
  const otherTrack = candidate.review_track === "research" ? "watch" : "research";
  const displayTitle = packageData?.title || candidate.proposed_title;
  const allowedTransitions = detail.allowed_transitions || [];
  const primaryTransition = primaryTopicTransition(candidate.status, allowedTransitions);
  const secondaryTransitions = allowedTransitions.filter((status) => status !== primaryTransition);
  const secondaryActions = [
    canChangeTrack
      ? `<button class="secondary-button" data-track-action="${otherTrack}">移到${TRACK[otherTrack]}</button>`
      : "",
    ...secondaryTransitions.map((status) => `<button class="${status === "archived" ? "danger-button" : "secondary-button"}" data-transition="${status}">${actionLabel(status, candidate.status)}</button>`),
  ].filter(Boolean).join("");
  return `
    <span class="detail-kicker">TOPIC CANDIDATE · #${candidate.id}</span>
    <h2>${escapeHtml(displayTitle)}</h2>
    <div class="chip-row">
      <span class="status-chip">${topicDisplayStage(candidate.status)}</span>
      <span class="status-chip ${candidate.review_track === "watch" ? "is-watch" : ""}">${TRACK[candidate.review_track]} · ${candidate.track_source === "manual" ? "人工调整" : "规则建议"}</span>
      <span class="status-chip ${isCycleOpen ? "" : "is-closed"}">${escapeHtml(candidate.cycle_label || candidate.cycle_slug || "未知期次")} · ${isCycleOpen ? "进行中" : "已关闭，只读"}</span>
    </div>
    <p class="detail-summary">${escapeHtml(candidate.why_now)}</p>

    <section class="detail-section">
      <h3>判断信息</h3>
      <div class="detail-grid">
        ${field("审阅队列", `${TRACK[candidate.review_track]}（${candidate.track_source === "manual" ? "人工" : "规则"}）`)}
        ${field("分队理由", candidate.track_reason)}
        ${field("下一步", topicNextStep(candidate.status))}
        ${field("目标读者", candidate.audience)}
        ${field("辅助评分", `${Number(candidate.score || 0).toFixed(1)} / 100（只在队列内排序）`)}
        ${field("核心问题", candidate.core_question)}
        ${field("差异化角度", candidate.angle)}
      </div>
      <p class="muted">${escapeHtml(candidate.score_reason)}</p>
      ${scoreBreakdown(candidate.score_breakdown)}
    </section>

    ${canEditCandidate ? `
      <section class="detail-section">
        <details class="editor-box">
          <summary>人工编辑选题内容</summary>
          <form id="editorialForm" class="editor-form">
            ${formField("建议题目", "proposed_title", candidate.proposed_title, "input")}
            ${formField("目标读者", "audience", candidate.audience, "input")}
            ${formField("核心问题", "core_question", candidate.core_question, "textarea")}
            ${formField("为什么现在", "why_now", candidate.why_now, "textarea")}
            ${formField("差异化角度", "angle", candidate.angle, "textarea")}
            <p class="muted">人工编辑会留痕，并在之后重跑 Demo 时保留；不会自动确认选题。</p>
            <button class="primary-button" type="submit">保存人工修改</button>
          </form>
        </details>
      </section>
    ` : ""}

    <section class="detail-section">
      <h3>行业信号与证据状态</h3>
      <div class="detail-grid">
        ${field("规则识别的底层事件", candidate.underlying_event)}
        ${field("样本情况", `${candidate.article_count} 篇文章 / ${candidate.source_count} 个公众号 / 独立来源估算 ${candidate.independent_source_count}`)}
        ${field("一致信息", candidate.consensus)}
        ${field("分歧", candidate.disagreements)}
      </div>
      <p><strong>证据缺口：</strong>${escapeHtml(candidate.data_gaps)}</p>
    </section>

    <section class="detail-section">
      <h3>风险提示</h3>
      <ul class="risk-list">${risks.map((risk) => `<li class="badge gold">${escapeHtml(risk)}</li>`).join("")}</ul>
    </section>

    <section class="detail-section">
      <h3>选题包</h3>
      ${topicPackageHtml(packageData, candidate.status, candidate.cycle_status)}
    </section>

    <section class="detail-section" id="researchHandoff">
      <h3>下一步：研究任务书</h3>
      ${researchHandoffHtml(detail)}
    </section>

    <section class="detail-section">
      <h3>来源文章</h3>
      <div class="source-list">${detail.articles.map(sourceItem).join("")}</div>
    </section>

    <div class="action-row">
      ${primaryTransition ? `<button class="primary-button" data-transition="${primaryTransition}">${actionLabel(primaryTransition, candidate.status)}</button>` : ""}
      ${secondaryActions ? `<details class="more-actions"><summary>更多操作</summary><div class="more-action-list">${secondaryActions}</div></details>` : ""}
      ${allowedTransitions.length || secondaryActions ? "" : terminalMessage(candidate.status, candidate.cycle_status)}
    </div>
  `;
}

function researchHandoffHtml(detail) {
  const candidate = detail.candidate;
  const task = detail.research_task;
  const articleDemo = detail.article_demo_url
    ? `
      <div class="article-demo-callout">
        <div>
          <strong>文章结构 Demo</strong>
          <span>仅展示微信公众号文章的篇幅与排版，不是深度研究产出，事实仍待补证。</span>
        </div>
        <a class="secondary-button button-link" href="${escapeAttribute(detail.article_demo_url)}" target="_blank" rel="noreferrer" data-article-demo>查看文章 Demo</a>
      </div>
    `
    : "";

  if (candidate.status !== "selected") {
    return `
      <p class="muted">选题包经人工确认进入“已选题”后，才可生成独立研究任务书。</p>
      ${articleDemo}
    `;
  }

  if (!task) {
    return `
      ${articleDemo}
      <div class="research-gate-card">
        <div>
          <span class="eyebrow">HUMAN GATE</span>
          <strong>先生成可编辑任务书，再决定是否启动研究</strong>
          <p>系统只从已确认选题包预填范围；不会调用 DeepSeek、联网研究或微信公众号接口。</p>
        </div>
        <button class="primary-button" type="button" data-create-research-task>生成研究任务书</button>
      </div>
    `;
  }

  const dimensions = task.selection_dimensions || [];
  const evidence = task.known_evidence || [];
  const statusLabel = researchDisplayStage(task.status);
  const statusHint = researchProgressHint(task.status);
  if (task.editable) {
    return `
      ${articleDemo}
      <div class="package-status-line">
        <span class="status-chip">${statusLabel}</span>
        <span class="muted">任务 #${task.id} · ${statusHint}</span>
      </div>
      <div class="readiness-box ${task.can_confirm ? "is-ready" : ""}">
        <strong>${task.can_confirm ? "可以提交人工确认" : "任务书尚未完整"}</strong>
        <span>${task.can_confirm ? "确认后只进入“待启动”，不会自动研究。" : `还缺：${escapeHtml((task.missing_field_labels || []).join("、") || "待检查字段")}`}</span>
      </div>
      <form id="researchTaskForm" class="editor-form research-task-form" data-research-task-id="${task.id}">
        ${formField("核心研究问题", "research_question", task.research_question, "textarea")}
        ${formField("目标读者", "audience", task.audience, "input")}
        ${formField("决策用途", "decision_use", task.decision_use, "textarea")}
        ${formField("研究范围", "scope", task.scope, "textarea")}
        <div class="research-form-grid">
          ${formField("地域范围", "geography", task.geography, "input")}
          ${formField("时间范围", "time_range", task.time_range, "input")}
        </div>
        ${formField("判断维度（每行一项）", "selection_dimensions_text", dimensions.join("\n"), "textarea")}
        ${formField("明确排除项", "exclusions", task.exclusions, "textarea")}
        ${formField("待补证主张", "evidence_gaps", task.evidence_gaps, "textarea")}
        ${formField("稿件要求", "output_requirements", task.output_requirements, "textarea")}
        ${researchEvidenceHtml(evidence)}
        <div class="research-form-actions">
          <button class="secondary-button" type="submit" data-research-action="save">保存任务书草稿</button>
          <button class="primary-button" type="submit" data-research-action="confirm">保存并人工确认</button>
        </div>
      </form>
    `;
  }

  const taskFields = `
    <div class="detail-grid package-summary-grid">
      ${field("核心研究问题", task.research_question)}
      ${field("目标读者", task.audience)}
      ${field("决策用途", task.decision_use)}
      ${field("研究范围", task.scope)}
      ${field("地域范围", task.geography)}
      ${field("时间范围", task.time_range)}
      ${field("明确排除项", task.exclusions)}
      ${field("待补证主张", task.evidence_gaps)}
      ${field("稿件要求", task.output_requirements)}
    </div>
    <ul class="dimension-list research-dimensions">${dimensions.map((item) => `<li class="badge teal">${escapeHtml(item)}</li>`).join("")}</ul>
    ${researchEvidenceHtml(evidence)}
  `;
  if (task.status === "pending_start") {
    return `
      ${articleDemo}
      <div class="package-status-line"><span class="status-chip">${statusLabel}</span><span class="muted">任务 #${task.id} · ${statusHint}</span></div>
      <div class="readiness-box is-ready"><strong>任务书已人工确认</strong><span>可以由你手动生成一份离线文章 Demo；它不会联网，也不会把发现线索当成已确认事实。</span></div>
      ${taskFields}
      <div class="research-action-bar"><button class="primary-button" type="button" data-run-offline-demo="${task.id}">启动离线文章 Demo（不联网）</button></div>
    `;
  }
  const draftAction = ["draft_review", "completed"].includes(task.status)
    ? `<button class="secondary-button" type="button" data-open-research-task="${task.id}">查看研究与稿件</button>`
    : "";
  return `
    ${articleDemo}
    <div class="package-status-line">
      <span class="status-chip">${statusLabel}</span>
      <span class="muted">任务 #${task.id} · ${statusHint}</span>
    </div>
    <div class="readiness-box is-ready">
      <strong>${task.status === "completed" ? "最终文章已人工确认" : "稿件已生成，等待人工审阅"}</strong>
      <span>${task.status === "completed" ? "已保存本地 Markdown；确认不等于授权发布。" : "可直接查看、退回生成新版本或人工编辑另存版本。"}</span>
    </div>
    ${taskFields}
    <div class="research-action-bar">${draftAction}</div>
  `;
}

function researchTaskDetailHtml(task) {
  const latest = task.latest_draft;
  const dimensions = task.selection_dimensions || [];
  const taskSummary = `
    <div class="detail-grid package-summary-grid">
      ${field("核心研究问题", task.research_question)}
      ${field("目标读者", task.audience)}
      ${field("决策用途", task.decision_use)}
      ${field("研究范围", task.scope)}
      ${field("地域范围", task.geography)}
      ${field("时间范围", task.time_range)}
      ${field("待补证主张", task.evidence_gaps)}
      ${field("稿件要求", task.output_requirements)}
    </div>
    <ul class="dimension-list research-dimensions">${dimensions.map((item) => `<li class="badge teal">${escapeHtml(item)}</li>`).join("")}</ul>
  `;
  const header = `
    <span class="detail-kicker">RESEARCH TASK · #${task.id}</span>
    <h2>${escapeHtml(task.topic_title || task.research_question || "研究任务")}</h2>
    <div class="chip-row">
      <span class="status-chip">${researchDisplayStage(task.status)}</span>
      <span class="status-chip">${task.start_mode === "manual" ? "人工启动" : "自动启动"}</span>
      <span class="status-chip ${task.cycle_status === "closed" ? "is-closed" : ""}">${escapeHtml(task.cycle_label || task.cycle_slug || "未知期次")}</span>
    </div>
    <p class="detail-stage-hint">下一步：${escapeHtml(researchProgressHint(task.status))}</p>
    <div class="research-demo-callout">
      <strong>离线文章 Demo 边界</strong>
      <span>本界面不会联网、不会调用 DeepSeek，也不会发布公众号。它只能验证“任务书 → 可审阅文章结构 → 人工定稿 Markdown”的操作链；事实仍须人工补证。</span>
    </div>
  `;

  if (task.status === "pending_start") {
    return `
      ${header}
      <section class="detail-section">
        <h3>任务书已确认，等待人工启动</h3>
        <div class="readiness-box is-ready"><strong>可以生成离线文章 Demo</strong><span>点击后只使用当前任务书与公众号发现线索，生成 V1 待审稿；不进行任何互联网检索。</span></div>
        ${taskSummary}
        ${researchEvidenceHtml(task.known_evidence || [])}
        <div class="research-action-bar"><button class="primary-button" type="button" data-run-offline-demo="${task.id}">启动离线文章 Demo（不联网）</button></div>
      </section>
    `;
  }

  if (!latest) {
    const message = task.status === "task_drafting"
      ? "研究任务书仍待人工确认，请先从选题详情补充并确认。"
      : "当前没有可查看的稿件。";
    return `${header}<section class="detail-section"><p class="muted">${message}</p>${taskSummary}</section>`;
  }

  const canRevise = task.status === "draft_review" && latest.status === "review";
  const finalPath = latest.final_markdown_absolute_path;
  return `
    ${header}
    <section class="detail-section">
      <div class="package-status-line">
        <span class="status-chip">V${latest.version} · ${draftStatus(latest.status)}</span>
        <span class="muted">${escapeHtml(draftGeneratorLabel(latest.generated_by))} · ${formatDateTime(latest.generated_at)}</span>
      </div>
      ${latest.is_offline_demo ? '<div class="readiness-box"><strong>这是一份离线 Demo 初稿</strong><span>它没有获取公开网页或新增一手来源；所有公众号线索仍为“待补证”。</span></div>' : ""}
      <article class="article-preview">
        <header><span>文章预览</span><strong>${escapeHtml(latest.title)}</strong></header>
        <div class="article-preview-body">${articlePreviewHtml(latest.article_body)}</div>
      </article>
      <div class="research-insight-grid">
        ${field("核心判断", latest.key_judgment)}
        ${field("行动启示", latest.action_implications)}
        ${field("不确定性", latest.uncertainty_notes)}
      </div>
      ${researchDraftSourcesHtml(latest.sources || [])}
      ${finalPath ? `<p class="final-markdown-path"><strong>最终 Markdown 已保存：</strong><code>${escapeHtml(finalPath)}</code></p>` : ""}
    </section>
    ${canRevise ? `
      <section class="detail-section">
        <h3>人工审阅与版本处理</h3>
        <p class="muted">退回或手动编辑都会新建版本，当前 V${latest.version} 永不覆盖。最终确认会生成本地 Markdown，不会触发发布。</p>
        <div class="research-action-bar">
          <button class="secondary-button" type="button" data-regenerate-offline-demo>退回并重生成 Demo V${Number(latest.version) + 1}</button>
          <button class="primary-button" type="button" data-finalize-draft="${latest.id}">人工确认最终 Markdown</button>
        </div>
        <details class="editor-box draft-editor-box">
          <summary>编辑当前正文并另存为 V${Number(latest.version) + 1}</summary>
          <form id="draftRevisionForm" class="editor-form" data-draft-id="${latest.id}">
            ${formField("文章标题", "title", latest.title, "input")}
            ${formField("核心判断", "key_judgment", latest.key_judgment, "textarea")}
            ${formField("行动启示", "action_implications", latest.action_implications, "textarea")}
            ${formField("不确定性与待补证说明", "uncertainty_notes", latest.uncertainty_notes, "textarea")}
            <label class="form-field"><span>文章正文（Markdown）</span><textarea name="article_body" rows="18" required>${escapeHtml(latest.article_body)}</textarea></label>
            ${formField("本次修订原因（必填，保留在版本记录中）", "revision_reason", "", "textarea")}
            <button class="primary-button" type="submit">保存为 V${Number(latest.version) + 1}</button>
          </form>
        </details>
      </section>
    ` : ""}
    <section class="detail-section">
      <h3>任务书范围</h3>
      ${taskSummary}
    </section>
    <section class="detail-section">
      <h3>版本记录</h3>
      ${draftHistoryHtml(task.drafts || [])}
    </section>
  `;
}

function researchDraftSourcesHtml(sources) {
  return `
    <div class="package-evidence research-evidence">
      <h4>稿件来源与证据状态 <span class="muted">${sources.length} 条</span></h4>
      <div class="source-list">
        ${sources.length ? sources.map((source) => {
          const title = escapeHtml(source.title || "未命名来源");
          const link = source.url_or_reference
            ? `<a href="${escapeAttribute(source.url_or_reference)}" target="_blank" rel="noreferrer">${title}</a>`
            : `<strong>${title}</strong>`;
          const status = source.evidence_status === "verified" ? "已核实" : "待补证";
          return `<div class="source-item"><div>${link}<small>${escapeHtml(source.source_name || "未知来源")} · ${escapeHtml(source.source_note || "")}</small></div><span class="badge ${status === "已核实" ? "teal" : "gold"}">${status}</span></div>`;
        }).join("") : '<div class="empty-column">尚无来源记录</div>'}
      </div>
    </div>
  `;
}

function draftHistoryHtml(drafts) {
  if (!drafts.length) return '<p class="muted">尚无稿件版本。</p>';
  return `<div class="draft-history">${drafts.map((draft, index) => `
    <details class="draft-history-item"${index === 0 ? " open" : ""}>
      <summary>V${draft.version} · ${draftStatus(draft.status)} · ${escapeHtml(draftGeneratorLabel(draft.generated_by))}</summary>
      <p class="muted">${escapeHtml(draft.change_summary || "无变更说明")}${draft.revision_reason ? ` · 原因：${escapeHtml(draft.revision_reason)}` : ""}</p>
      <div class="draft-history-preview">${articlePreviewHtml(draft.article_body)}</div>
    </details>
  `).join("")}</div>`;
}

function articlePreviewHtml(markdown) {
  const blocks = String(markdown || "").trim().split(/\n\s*\n/).filter(Boolean);
  return blocks.map((rawBlock) => {
    const block = String(rawBlock).trim();
    if (block.startsWith("# ")) return `<h1>${articleInlineHtml(block.slice(2))}</h1>`;
    if (block.startsWith("## ")) return `<h2>${articleInlineHtml(block.slice(3))}</h2>`;
    if (block.startsWith("> ")) return `<blockquote>${articleInlineHtml(block.replace(/^>\s?/gm, "")).replace(/\n/g, "<br>")}</blockquote>`;
    const lines = block.split("\n");
    if (lines.every((line) => /^-\s+/.test(line))) {
      return `<ul>${lines.map((line) => `<li>${articleInlineHtml(line.replace(/^-\s+/, ""))}</li>`).join("")}</ul>`;
    }
    if (lines.every((line) => /^\d+\.\s+/.test(line))) {
      return `<ol>${lines.map((line) => `<li>${articleInlineHtml(line.replace(/^\d+\.\s+/, ""))}</li>`).join("")}</ol>`;
    }
    return `<p>${articleInlineHtml(block).replace(/\n/g, "<br>")}</p>`;
  }).join("");
}

function articleInlineHtml(value) {
  return escapeHtml(value).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
}

function researchEvidenceHtml(evidence) {
  return `
    <div class="package-evidence research-evidence">
      <h4>随任务书带入的公众号发现线索 <span class="muted">${evidence.length} 条，不等于一手证据</span></h4>
      <div class="source-list">
        ${evidence.length ? evidence.map((item) => {
          const title = escapeHtml(item.title || "未命名来源");
          const link = item.url
            ? `<a href="${escapeAttribute(item.url)}" target="_blank" rel="noreferrer">${title}</a>`
            : `<strong>${title}</strong>`;
          return `<div class="source-item"><div>${link}<small>${escapeHtml(item.account_name || "未知公众号")} · ${formatDate(item.published_at)}</small></div><span class="badge gold">发现源</span></div>`;
        }).join("") : '<div class="empty-column">尚无发现线索</div>'}
      </div>
    </div>
  `;
}

function topicPackageHtml(packageData, candidateStatus, cycleStatus) {
  if (!packageData) {
    return '<p class="muted">进入“选题包完善”后才创建。创建不等于确认，也不会自动启动研究。</p>';
  }
  const dimensions = packageData.selection_dimensions || [];
  const evidence = packageData.evidence || [];
  const missingLabels = packageData.missing_field_labels || [];
  const editable = cycleStatus === "open" && candidateStatus === "package_drafting" && packageData.status === "drafting";
  let readiness;
  if (packageData.status === "pending_confirmation") {
    readiness = packageData.can_submit
      ? '<div class="readiness-box is-ready"><strong>已提交人工确认</strong><span>字段已锁定；如需修改，请先退回“选题包完善”。</span></div>'
      : `<div class="readiness-box"><strong>提交内容不完整</strong><span>请退回选题包完善并补充：${escapeHtml(missingLabels.join("、") || "待检查字段")}</span></div>`;
  } else if (packageData.status === "confirmed") {
    readiness = '<div class="readiness-box is-ready"><strong>选题包已人工确认</strong><span>本版本不会自动创建或启动深度研究任务。</span></div>';
  } else {
    readiness = packageData.can_submit
      ? '<div class="readiness-box is-ready"><strong>可以提交人工确认</strong><span>字段已完整；提交后如需修改，先退回“选题包完善”。</span></div>'
      : `<div class="readiness-box"><strong>尚未达到提交条件</strong><span>还缺：${escapeHtml(missingLabels.join("、") || "待检查字段")}</span></div>`;
  }

  if (editable) {
    return `
      <div class="package-status-line">
        <span class="status-chip">${packageStatus(packageData.status)}</span>
        <span class="muted">保存草稿不会提交确认</span>
      </div>
      ${readiness}
      <form id="packageForm" class="editor-form package-form">
        ${formField("最终选题标题", "title", packageData.title, "input")}
        ${formField("目标读者", "audience", packageData.audience, "input")}
        ${formField("核心问题", "core_question", packageData.core_question, "textarea")}
        ${formField("为什么现在", "why_now", packageData.why_now, "textarea")}
        ${formField("差异化行业判断角度", "industry_angle", packageData.industry_angle, "textarea")}
        ${formField("选题判断维度（每行一项）", "selection_dimensions_text", dimensions.join("\n"), "textarea")}
        ${formField("待补证事实", "evidence_gaps", packageData.evidence_gaps, "textarea", false)}
        ${formField("研究方向", "research_directions", packageData.research_directions, "textarea", false)}
        ${formField("风险提示", "risk_notes", packageData.risk_notes, "textarea", false)}
        <button class="primary-button" type="submit">保存选题包草稿</button>
      </form>
      ${packageEvidenceHtml(evidence)}
    `;
  }

  return `
    <div class="package-status-line">
      <span class="status-chip">${packageStatus(packageData.status)}</span>
      ${packageData.editor_modified ? '<span class="muted">已人工编辑</span>' : '<span class="muted">规则生成草稿</span>'}
    </div>
    ${readiness}
    <div class="detail-grid package-summary-grid">
      ${field("最终选题标题", packageData.title)}
      ${field("目标读者", packageData.audience)}
      ${field("核心问题", packageData.core_question)}
      ${field("为什么现在", packageData.why_now)}
      ${field("差异化角度", packageData.industry_angle)}
      ${field("待补证事实", packageData.evidence_gaps)}
      ${field("研究方向", packageData.research_directions)}
      ${field("风险提示", packageData.risk_notes)}
    </div>
    <ul class="dimension-list">${dimensions.map((item) => `<li class="badge teal">${escapeHtml(item)}</li>`).join("")}</ul>
    ${candidateStatus === "pending_confirmation" ? '<p class="gate-note">当前等待人工确认。如需修改，请点击“退回选题包完善”。</p>' : ""}
    ${candidateStatus === "selected" ? '<p class="gate-note">选题包已确认。本版本不会自动创建或启动深度研究任务。</p>' : ""}
    ${packageEvidenceHtml(evidence)}
  `;
}

function packageEvidenceHtml(evidence) {
  return `
    <div class="package-evidence">
      <h4>已有公众号线索 <span class="muted">${evidence.length} 条，均为发现源</span></h4>
      <div class="source-list">
        ${evidence.length ? evidence.map((item) => {
          const title = escapeHtml(item.title || "未命名来源");
          const link = item.url
            ? `<a href="${escapeAttribute(item.url)}" target="_blank" rel="noreferrer">${title}</a>`
            : `<strong>${title}</strong>`;
          return `<div class="source-item"><div>${link}<small>${escapeHtml(item.account_name || "未知公众号")} · ${formatDate(item.published_at)}</small></div><span class="badge gold">待补证</span></div>`;
        }).join("") : '<div class="empty-column">尚无公众号线索，不能提交确认</div>'}
      </div>
    </div>
  `;
}

function terminalMessage(status, cycleStatus) {
  if (cycleStatus === "closed") {
    return status === "selected"
      ? '<span class="muted">该期信号与选题包已只读；下游研究任务书拥有独立人工门禁。</span>'
      : '<span class="muted">该监测期次已关闭，信号、选题与选题包均为只读。</span>';
  }
  if (status === "selected") {
    return '<span class="muted">选题包已人工确认；不会自动创建或启动研究任务。</span>';
  }
  if (status === "archived") {
    return '<span class="muted">该选题已归档并保留记录。</span>';
  }
  return '<span class="muted">当前为终态。</span>';
}

function primaryTopicTransition(fromStatus, allowedTransitions) {
  const nextStatus = {
    candidate_pool: "pending_decision",
    pending_decision: "package_drafting",
    package_drafting: "pending_confirmation",
    pending_confirmation: "selected",
  }[fromStatus];
  return nextStatus && allowedTransitions.includes(nextStatus) ? nextStatus : null;
}

function actionLabel(toStatus, fromStatus) {
  const directionalLabels = {
    "candidate_pool:pending_decision": "开始判断",
    "pending_decision:candidate_pool": "放回候选池",
    "pending_decision:package_drafting": "开始完善选题包",
    "package_drafting:pending_decision": "暂回待判断",
    "package_drafting:pending_confirmation": "提交人工确认",
    "pending_confirmation:package_drafting": "退回选题包完善",
    "pending_confirmation:selected": "确认选题",
  };
  const directionalKey = `${fromStatus}:${toStatus}`;
  if (directionalLabels[directionalKey]) return directionalLabels[directionalKey];
  return ACTION[toStatus] || toStatus;
}

function field(label, value) {
  return `<div class="detail-field"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value || "—")}</strong></div>`;
}

function scoreBreakdown(breakdown = {}) {
  const items = [
    ["账号覆盖", breakdown.source_breadth],
    ["独立来源", breakdown.evidence_independence],
    ["弱传播信号", breakdown.engagement],
    ["时效", breakdown.recency],
    ["决策相关", breakdown.decision_relevance],
  ];
  return `
    <div class="score-breakdown">
      ${items.map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${Number(value || 0).toFixed(1)}</strong></div>`).join("")}
    </div>
  `;
}

function formField(label, name, value, kind, required = true) {
  const requiredAttribute = required ? " required" : "";
  const control = kind === "textarea"
    ? `<textarea name="${name}" rows="3"${requiredAttribute}>${escapeHtml(value)}</textarea>`
    : `<input name="${name}" value="${escapeAttribute(value)}"${requiredAttribute}>`;
  return `<label class="form-field"><span>${escapeHtml(label)}</span>${control}</label>`;
}

function sourceItem(article) {
  const link = article.url ? `<a href="${escapeAttribute(article.url)}" target="_blank" rel="noreferrer">${escapeHtml(article.title)}</a>` : `<strong>${escapeHtml(article.title)}</strong>`;
  return `
    <div class="source-item">
      <div>${link}<small>${escapeHtml(article.account_name)} · ${formatDate(article.published_at)}${article.is_primary_evidence ? " · 代表文章" : ""}</small></div>
      <span class="source-metrics">阅读 ${number(article.read_count)} · 分享 ${number(article.share_count)}</span>
    </div>
  `;
}

async function transitionCandidate(candidateId, toStatus) {
  let reason = "";
  if (toStatus === "archived") {
    reason = window.prompt("请填写归档原因（必填）", "证据较弱") || "";
    if (!reason.trim()) return;
  }
  if (toStatus === "selected") {
    const confirmed = window.confirm("确认这个选题包？确认后本版本不会自动启动深度研究。");
    if (!confirmed) return;
  }
  try {
    const response = await fetch(`/api/candidates/${candidateId}/transition`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ to_status: toStatus, reason }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    showToast(`已由人工推进：${STATUS[payload.from_status]} → ${STATUS[payload.to_status]}`);
    await loadDashboard();
    await openCandidate(candidateId);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function changeCandidateTrack(candidateId, reviewTrack) {
  try {
    const response = await fetch(`/api/candidates/${candidateId}/track`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ review_track: reviewTrack, reason: "人工调整审阅队列" }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    showToast(`已人工移到“${TRACK[payload.to_track]}”，不会自动改变选题状态`);
    await loadDashboard();
    await openCandidate(candidateId);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function saveCandidateEdit(event, candidateId) {
  event.preventDefault();
  const form = event.currentTarget;
  const submitButton = form.querySelector('button[type="submit"]');
  submitButton.disabled = true;
  const values = Object.fromEntries(new FormData(form).entries());
  try {
    const response = await fetch(`/api/candidates/${candidateId}/edit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    const changed = payload.changed_fields?.length || 0;
    showToast(changed ? `已保存 ${changed} 项人工修改` : "内容没有变化");
    await loadDashboard();
    await openCandidate(candidateId);
  } catch (error) {
    submitButton.disabled = false;
    showToast(error.message, true);
  }
}

async function saveTopicPackage(event, candidateId) {
  event.preventDefault();
  const form = event.currentTarget;
  const submitButton = form.querySelector('button[type="submit"]');
  submitButton.disabled = true;
  const formValues = Object.fromEntries(new FormData(form).entries());
  const selectionDimensions = String(formValues.selection_dimensions_text || "")
    .split(/\r?\n|[，,；;]/)
    .map((value) => value.trim())
    .filter(Boolean);
  delete formValues.selection_dimensions_text;
  const payloadBody = { ...formValues, selection_dimensions: selectionDimensions };
  try {
    const response = await fetch(`/api/candidates/${candidateId}/package`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payloadBody),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    const changed = payload.changed_fields?.length || 0;
    const missing = payload.missing_field_labels || [];
    showToast(
      missing.length
        ? `草稿已保存；提交前还需补充：${missing.join("、")}`
        : (changed ? `选题包已保存，可提交人工确认` : "选题包没有变化，可提交人工确认"),
      false,
    );
    await loadDashboard();
    await openCandidate(candidateId);
  } catch (error) {
    submitButton.disabled = false;
    showToast(error.message, true);
  }
}

async function createResearchTask(candidateId) {
  try {
    const response = await fetch(`/api/candidates/${candidateId}/research-task`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    showToast(payload.created ? "研究任务书已生成，请人工检查范围" : "该选题已有研究任务书");
    await loadDashboard();
    await openCandidate(candidateId);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function saveResearchTask(event, candidateId) {
  event.preventDefault();
  const form = event.currentTarget;
  const action = event.submitter?.dataset.researchAction || "save";
  const buttons = form.querySelectorAll("button[type='submit']");
  buttons.forEach((button) => { button.disabled = true; });
  const formValues = Object.fromEntries(new FormData(form).entries());
  const selectionDimensions = String(formValues.selection_dimensions_text || "")
    .split(/\r?\n|[，,；;]/)
    .map((value) => value.trim())
    .filter(Boolean);
  delete formValues.selection_dimensions_text;
  const taskId = Number(form.dataset.researchTaskId);
  if (!taskId) {
    showToast("无法识别研究任务，请重新打开选题", true);
    buttons.forEach((button) => { button.disabled = false; });
    return;
  }
  try {
    let response = await fetch(`/api/research-tasks/${taskId}/update`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...formValues, selection_dimensions: selectionDimensions }),
    });
    let payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    if (action === "confirm") {
      response = await fetch(`/api/research-tasks/${taskId}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      showToast("研究任务书已人工确认，当前停在“待启动”");
    } else {
      const changed = payload.changed_fields?.length || 0;
      showToast(changed ? `已保存 ${changed} 项任务书修改` : "任务书没有变化");
    }
    await loadDashboard();
    await openCandidate(candidateId);
  } catch (error) {
    buttons.forEach((button) => { button.disabled = false; });
    showToast(error.message, true);
  }
}

async function runOfflineDraftDemo(taskId) {
  const confirmed = window.confirm(
    "启动离线文章 Demo？它只会根据当前任务书和公众号发现线索生成 V1 结构稿，不联网、不调用 DeepSeek，所有事实仍为待补证。",
  );
  if (!confirmed) return;
  try {
    const response = await fetch(`/api/research-tasks/${taskId}/run-offline-demo`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    showToast(`已生成离线 Demo 稿 V${payload.mutation?.version || 1}，请人工审阅`);
    await loadDashboard();
    await openResearchTask(taskId);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function regenerateOfflineDraftDemo(taskId) {
  const reason = window.prompt("请填写退回 / 重生成原因（会保留在版本记录中）", "补充更清楚的行业判断与行动框架") || "";
  if (!reason.trim()) return;
  try {
    const response = await fetch(`/api/research-tasks/${taskId}/regenerate-offline-demo`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ revision_reason: reason }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    showToast(`已保留旧稿并生成 Demo V${payload.mutation?.version || "新版本"}`);
    await loadDashboard();
    await openResearchTask(taskId);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function saveDraftRevision(event, taskId) {
  event.preventDefault();
  const form = event.currentTarget;
  const submitButton = form.querySelector('button[type="submit"]');
  const draftId = Number(form.dataset.draftId);
  if (!draftId) {
    showToast("无法识别当前稿件版本，请重新打开任务", true);
    return;
  }
  submitButton.disabled = true;
  const values = Object.fromEntries(new FormData(form).entries());
  try {
    const response = await fetch(`/api/research-tasks/${taskId}/drafts/${draftId}/revise`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    showToast(`已另存为 V${payload.mutation?.version || "新版本"}；旧稿保持不变`);
    await loadDashboard();
    await openResearchTask(taskId);
  } catch (error) {
    submitButton.disabled = false;
    showToast(error.message, true);
  }
}

async function finalizeResearchDraft(taskId, draftId) {
  const firstConfirmed = window.confirm(
    "确认将当前稿件保存为最终 Markdown？这不会发布到公众号；请先确保未核实内容仍保留“待补证”标记。",
  );
  if (!firstConfirmed) return;
  const reviewNote = window.prompt(
    "请输入人工事实与编辑审阅说明（会写入最终 Markdown）",
    "人工审阅完成；未补齐的一手来源继续保留待补证标记。",
  ) || "";
  if (!reviewNote.trim()) return;
  try {
    const response = await fetch(`/api/research-tasks/${taskId}/finalize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft_id: draftId, review_note: reviewNote }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    showToast("已人工确认并保存最终 Markdown；没有执行任何发布动作");
    await loadDashboard();
    await openResearchTask(taskId);
  } catch (error) {
    showToast(error.message, true);
  }
}

function trendLabel(value) {
  return ({ new: "新出现", rising: "升温", steady: "持续", falling: "回落", dormant: "沉寂" })[value] || value;
}

function packageStatus(value) {
  return ({ drafting: "完善中", pending_confirmation: "待人工确认", confirmed: "已人工确认" })[value] || value;
}

function topicDisplayStage(value) {
  return TOPIC_DISPLAY_STAGES.find((stage) => stage.statuses.includes(value))?.label
    || STATUS[value]
    || value;
}

function topicNextStep(value) {
  return ({
    candidate_pool: "判断是否列入选题",
    pending_decision: "开始完善选题包",
    package_drafting: "补齐并提交选题包",
    pending_confirmation: "人工确认选题包",
    selected: "可创建研究任务书",
    archived: "已归档留痕",
  })[value] || "查看详情";
}

function researchDisplayStage(value) {
  return RESEARCH_DISPLAY_STAGES.find((stage) => stage.statuses.includes(value))?.label
    || (value === "cancelled" ? "已取消" : value);
}

function researchProgressHint(value) {
  return ({
    task_drafting: "补全并人工确认",
    pending_start: "已确认，等待人工启动",
    researching: "正在生成稿件",
    draft_review: "等待人工审阅",
    completed: "最终 Markdown 已保存",
    cancelled: "已取消并保留记录",
  })[value] || "查看任务详情";
}

function draftStatus(value) {
  return ({ review: "待人工审阅", superseded: "已保留旧版", final: "最终文章" })[value] || value || "尚无稿件";
}

function draftGeneratorLabel(value) {
  return ({
    offline_article_demo_v1: "离线 Demo 生成",
    manual_editor: "人工编辑",
  })[value] || value || "未知生成方式";
}

function formatDate(value) {
  if (!value) return "—";
  const text = String(value);
  return text.slice(0, 10);
}

function formatDateTime(value) {
  if (!value) return "—";
  return String(value).replace("T", " ").slice(0, 16);
}

function addDays(isoDate, days) {
  const date = new Date(`${isoDate}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return toIsoDate(date);
}

function toIsoDate(date) {
  return new Date(date).toISOString().slice(0, 10);
}

function isoWeek(isoDate) {
  const date = new Date(`${isoDate}T00:00:00Z`);
  const day = date.getUTCDay() || 7;
  date.setUTCDate(date.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  const week = Math.ceil((((date - yearStart) / 86400000) + 1) / 7);
  return { year: date.getUTCFullYear(), week };
}

function number(value) {
  return new Intl.NumberFormat("zh-CN").format(Number(value || 0));
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

function escapeAttribute(value) {
  return escapeHtml(value).replace(/`/g, "&#96;");
}

let toastTimer;
function showToast(message, isError = false) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.classList.toggle("is-error", isError);
  toast.classList.add("is-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 3200);
}
