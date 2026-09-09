const state = {
  data: null,
  batch: null,
  selected: new Set(),
  search: "",
  filter: "all",
  stage: null,
  view: "production",
  pendingAction: null,
  config: null,
  environment: null,
  authorJobs: [],
  mothers: [],
  pipelineJobs: [],
};
let drawerCloseTimer = null;
let authorJobsTimer = null;
let authorJobsSignature = "";
let pipelineJobsSignature = "";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

function refreshIcons() {
  if (window.lucide) window.lucide.createIcons({ attrs: { "aria-hidden": "true" } });
}

function formatDate(value) {
  if (!value) return "暂无";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "操作失败");
  return payload;
}

function toast(message, error = false) {
  const element = document.createElement("div");
  element.className = `toast${error ? " error" : ""}`;
  element.textContent = message;
  $("#toast-region").append(element);
  setTimeout(() => element.remove(), 4200);
}

function currentQuestions() {
  if (!state.data) return [];
  const needle = state.search.trim().toLowerCase();
  return state.data.questions.filter((question) => {
    if (state.view === "records" && question.record_count === 0) return false;
    if (state.stage && question.stage_index !== state.stage) return false;
    if (state.filter === "pending" && question.stage_index >= 6) return false;
    if (state.filter === "passed" && !question.delivery_qc) return false;
    if (state.filter === "delivered" && !question.exported) return false;
    if (!needle) return true;
    return [question.title, question.task_id, question.languages, question.task_type]
      .join(" ").toLowerCase().includes(needle);
  });
}

function renderBatchOptions() {
  const select = $("#batch-select");
  select.innerHTML = state.data.batches.map((batch) => (
    `<option value="${escapeHtml(batch.name)}" ${batch.name === state.batch ? "selected" : ""}>` +
    `${escapeHtml(batch.name)}（${batch.question_count} 道）</option>`
  )).join("");
}

function renderSummary() {
  const summary = state.data.summary;
  $("#summary-chips").innerHTML = `
    <span class="summary-chip">题目<strong>${summary.total}</strong></span>
    <span class="summary-chip">待处理<strong>${summary.waiting}</strong></span>
    <span class="summary-chip success">质检通过<strong>${summary.qc_passed}</strong></span>
    <span class="summary-chip success">已经交付<strong>${summary.delivered}</strong></span>`;
}

function renderPipeline() {
  $("#pipeline-track").innerHTML = state.data.stages.map((stage) => `
    <button class="stage-button ${state.stage === stage.id ? "active" : ""}" data-stage="${stage.id}" aria-label="${escapeHtml(stage.label)}，待处理 ${stage.current} 道">
      <span class="stage-number">${stage.id}</span>
      <span>${escapeHtml(stage.label)}</span>
      <span class="stage-count ${stage.current ? "has-work" : ""}" title="待处理 ${stage.current} 道">${stage.current}</span>
    </button>`).join("");
}

function stageTag(question) {
  const tones = { 1: "red", 2: "amber", 3: "blue", 4: "amber", 5: "blue", 6: "green" };
  return `<span class="tag ${tones[question.stage_index]}">${escapeHtml(question.stage_label)}</span>`;
}

function renderRows() {
  const questions = currentQuestions();
  const rows = $("#question-rows");
  rows.innerHTML = questions.map((question) => {
    const checked = state.selected.has(question.id) ? "checked" : "";
    const runLabel = question.run_count ? (question.record_count ? "已完成" : "已启动") : "未启动";
    const runTone = question.run_count ? (question.record_count ? "green" : "blue") : "";
    const recordText = question.record_count ? `${question.record_count} 轮` : "暂无记录";
    const qcLabel = question.delivery_qc ? "质检通过" : (question.record_count ? "待质检" : "尚未生产");
    const qcTone = question.delivery_qc ? "green" : (question.record_count ? "amber" : "");
    return `<tr data-id="${question.id}">
      <td class="select-column"><input type="checkbox" class="row-check" aria-label="选择 ${escapeHtml(question.task_id)}" ${checked}></td>
      <td><div class="question-main"><strong>${escapeHtml(question.title)}</strong><span>${escapeHtml(question.task_id)} · ${escapeHtml(question.languages)}</span><span>${escapeHtml(question.task_type)} · ${escapeHtml(question.difficulty)}</span></div></td>
      <td>${stageTag(question)}</td>
      <td><div class="cell-stack"><span class="tag ${runTone}">${runLabel}</span><small>${question.launched_at ? formatDate(question.launched_at) : "等待运行"}</small></div></td>
      <td><div class="cell-stack"><span>${recordText}</span><small>${question.average_score === null ? "暂无评分" : `均分 ${question.average_score}`}</small></div></td>
      <td><span class="tag ${qcTone}">${qcLabel}</span></td>
      <td><div class="row-actions">
        <button class="icon-button" data-action="detail" title="查看详情" aria-label="查看详情"><i data-lucide="eye"></i></button>
        <button class="icon-button" data-action="copy" title="复制 Prompt" aria-label="复制 Prompt"><i data-lucide="copy"></i></button>
        <button class="icon-button" data-action="folder" title="打开题目目录" aria-label="打开题目目录"><i data-lucide="folder-open"></i></button>
        ${question.run_count === 0 ? `<button class="icon-button" data-action="single-pipeline" title="单题跑全流程" aria-label="单题跑全流程"><i data-lucide="play-circle"></i></button>` : ""}
      </div></td>
    </tr>`;
  }).join("");
  $("#empty-state").hidden = questions.length > 0;
  $("#result-count").textContent = `显示 ${questions.length} / ${state.data.questions.length} 道`;
  updateSelectionState();
  refreshIcons();
}

function renderFiles() {
  const workbookFiles = state.data.batch.workbooks.map((file) => ({ ...file, type: "workbook" }));
  const trajectoryFiles = state.data.batch.trajectories.map((file) => ({ ...file, type: "trajectory" }));
  const files = [...workbookFiles, ...trajectoryFiles].sort((a, b) => b.modified_at.localeCompare(a.modified_at));
  $("#file-list").innerHTML = files.length ? files.map((file) => `
    <div class="file-row">
      <span class="file-icon"><i data-lucide="${file.type === "workbook" ? "file-spreadsheet" : "file-json"}"></i></span>
      <div class="file-info"><strong>${escapeHtml(file.name)}</strong><span>${formatBytes(file.size)} · ${formatDate(file.modified_at)}</span></div>
      <button class="button secondary" data-file-path="${escapeHtml(file.path)}"><i data-lucide="copy"></i>复制路径</button>
    </div>`).join("") : `<div class="empty-state"><strong>还没有交付文件</strong><span>交付质检通过后即可导出。</span></div>`;
  refreshIcons();
}

function renderView() {
  const exportsView = state.view === "exports";
  const settingsView = state.view === "settings";
  const authorView = state.view === "author";
  const nonProductionView = exportsView || settingsView || authorView;
  $(".batch-toolbar").hidden = settingsView || authorView;
  $(".pipeline-band").hidden = nonProductionView;
  $(".list-controls").hidden = nonProductionView;
  $(".table-panel").hidden = nonProductionView;
  $("#export-view").hidden = !exportsView;
  $("#settings-view").hidden = !settingsView;
  $("#author-view").hidden = !authorView;
  $("#pipeline-jobs-panel").hidden = nonProductionView;
  const titles = {
    author: ["生成题目", "填写批次信息和关键词，生成可复制的标准出题命令。"],
    production: ["生产批次", "从题目质检到 Excel 交付，状态直接来自本地生产库。"],
    records: ["交付记录", "查看已经生成评分记录的题目及交付质检状态。"],
    exports: ["导出中心", "集中查看当前批次的工作簿和原始 JSONL 轨迹。"],
    settings: ["运行配置", "修改下一次 Claude Code 启动使用的中转地址、模型和提交人。"],
  };
  $("#page-title").textContent = titles[state.view][0];
  $("#page-subtitle").textContent = titles[state.view][1];
  $$(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === state.view));
  if (exportsView) renderFiles();
  else if (settingsView) renderSettings();
  else if (!authorView) renderRows();
}

function renderSettings() {
  if (!state.config) return;
  $("#config-base-url").value = state.config.base_url || "";
  $("#config-model").value = state.config.model || "";
  $("#config-submitter").value = state.config.submitter || "";
  $("#config-api-key").value = "";
  $("#config-key-hint").textContent = state.config.api_key_hint || "";
  $("#config-docker-image").value = state.config.docker_image || "claude-cli:latest";
  $("#config-docker-command").value = state.config.docker_command || "claude";
  $("#config-qc-concurrency").value = state.config.qc_concurrency || 2;
  $("#config-model-concurrency").value = state.config.model_concurrency || 2;
  $("#config-codex-concurrency").value = state.config.codex_concurrency || 2;
}

function renderEnvironment() {
  const container = $("#environment-status");
  if (!container || !state.environment) return;
  const status = state.environment;
  const summary = "<div class=\"environment-summary " + (status.ok ? "ok" : "failed") + "\"><span class=\"status-dot\"></span><strong>" + (status.ok ? "环境可用" : "环境不可用") + "</strong><small>检测于 " + escapeHtml(formatDate(status.checked_at)) + "</small></div>";
  const checks = (status.checks || []).map((check) =>
    "<div class=\"environment-check " + (check.ok ? "ok" : "failed") + "\"><i data-lucide=\"" + (check.ok ? "check-circle-2" : "x-circle") + "\"></i><div><strong>" + escapeHtml(check.name) + "</strong><span>" + escapeHtml(check.detail) + "</span></div></div>"
  ).join("");
  const messages = (status.repair_messages || []).map((message) =>
    "<p class=\"environment-message\">" + escapeHtml(message) + "</p>"
  ).join("");
  container.innerHTML = summary + "<div class=\"environment-checks\">" + checks + "</div>" + messages;
  refreshIcons();
}

async function loadEnvironment(repair = false) {
  const button = $("#environment-repair");
  if (button) button.disabled = true;
  try {
    state.environment = await api(repair ? "/api/actions/environment-repair" : "/api/environment", repair ? { method: "POST", body: "{}" } : {});
    renderEnvironment();
    if (!state.environment.ok) toast("运行环境不可用，请先修复", true);
    else if (repair) toast("运行环境已通过检测");
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (button) button.disabled = false;
  }
}

function pipelineStatus(status) {
  return ({
    queued: "排队中",
    running: "执行中",
    qc_running: "题目质检中",
    qc_passed: "题目质检通过",
    model_running: "模型跑题中",
    model_completed: "模型跑题完成",
    producing: "交付生产中",
    produced: "交付已生产",
    finalizing: "交付质检与导出中",
    completed: "已完成",
    failed: "失败",
    interrupted: "已中断",
  })[status] || status;
}

function pipelineItemLabel(item) {
  if (item.status !== "model_running") return pipelineStatus(item.status);
  return ({
    starting: "模型容器启动中",
    healthy: "模型运行正常",
    idle: "模型运行中，暂时无新轨迹",
    unavailable: "模型容器状态异常",
    stalled: "模型运行已停滞",
  })[item.health_status] || "模型运行中，等待健康确认";
}

function scrollJobLogsToLatest() {
  document.querySelectorAll(".author-job-output").forEach((output) => {
    output.scrollTop = output.scrollHeight;
  });
}

function renderPipelineJobs() {
  const list = $("#pipeline-job-list");
  if (!list) return;
  if (!state.pipelineJobs.length) {
    list.innerHTML = '<div class="empty-state"><strong>暂无自动流水线任务</strong><span>点击“ 一键全流程 ”后，状态和日志会保存在这里。</span></div>';
    return;
  }
  list.innerHTML = state.pipelineJobs.map((job) => `
    <article class="pipeline-job status-${escapeHtml(job.status)}">
      <div class="pipeline-job-head"><div><strong>#${job.id} · ${escapeHtml(job.batch_name)}</strong><span>${job.question_count} 题 · Docker ${escapeHtml(job.docker_image)}</span></div><div class="job-actions">${job.can_retry ? `<button class="button secondary compact" type="button" data-pipeline-retry="${job.id}"><i data-lucide="rotate-ccw"></i>从失败处重试</button>` : ""}<span class="job-status">${escapeHtml(pipelineStatus(job.status))}</span></div></div>
      <div class="pipeline-job-meta"><span>质检并发 ${job.qc_concurrency}</span><span>模型并发 ${job.model_concurrency}</span><span>交付并发 ${job.codex_concurrency}</span>${job.retry_of_job_id ? `<span>重试自 #${job.retry_of_job_id}</span>` : ""}<span>${escapeHtml(formatDate(job.created_at))}</span></div>
      <div class="pipeline-item-grid">${(job.items || []).map((item) => `<span class="pipeline-item status-${escapeHtml(item.status.replaceAll("_", "-"))} health-${escapeHtml(item.health_status || "unknown")}"${item.error ? ` title="${escapeHtml(item.error)}"` : ""}><span>第 ${item.question_no} 题：${escapeHtml(pipelineItemLabel(item))}</span>${item.status === "model_running" && item.health_detail ? `<small>${escapeHtml(item.health_detail)} · 最近活动 ${escapeHtml(formatDate(item.activity_at || item.heartbeat_at))}</small>` : ""}</span>`).join("")}</div>
      ${job.can_retry ? `<div class="job-actions"><button class="button secondary compact" type="button" data-author-retry="${job.id}"><i data-lucide="rotate-ccw"></i>从失败处重试</button></div>` : ""}
      ${job.error ? `<div class="author-job-error">${escapeHtml(job.error)}</div>` : ""}
      <pre class="author-job-output">${escapeHtml(job.output || job.last_message || "等待流水线启动...")}</pre>
    </article>`).join("");
  scrollJobLogsToLatest();
  refreshIcons();
}

function retryPipelineJob(jobId) {
  openModal("从失败处重试", `将保留任务 #${jobId} 的日志和原始轨迹，跳过已有有效结果，从首个失败阶段继续。`, "开始重试", async () => {
    closeModal();
    const button = document.querySelector(`[data-pipeline-retry="${jobId}"]`);
    if (button) button.disabled = true;
    try {
      const result = await api("/api/actions/auto-pipeline-retry", {
        method: "POST", body: JSON.stringify({ job_id: jobId }),
      });
      toast(result.message || "重试任务已启动");
      await loadPipelineJobs();
    } catch (error) {
      toast(error.message, true);
      if (button) button.disabled = false;
    }
  });
}

let pipelineJobsTimer = null;
async function loadPipelineJobs() {
  if (document.hidden) return;
  try {
    const result = await api("/api/pipeline-jobs");
    const seenBatches = new Set();
    state.pipelineJobs = (result.jobs || []).filter((job) => {
      if (seenBatches.has(job.batch_name)) return false;
      seenBatches.add(job.batch_name);
      return true;
    }).map((job) => ({ ...job, output: (job.output || "").slice(-30000) }));
    const signature = JSON.stringify(state.pipelineJobs.map((job) => ({
      id: job.id, status: job.status, outputLength: job.output_length,
      lastMessage: job.last_message, error: job.error,
      items: (job.items || []).map((item) => [
        item.id, item.status, item.heartbeat_at, item.activity_at,
        item.health_status, item.health_detail, item.error,
      ]),
    })));
    if (signature !== pipelineJobsSignature) {
      pipelineJobsSignature = signature;
      renderPipelineJobs();
    }
    const active = state.pipelineJobs.some((job) => job.status === "queued" || job.status === "running");
    if (active && !pipelineJobsTimer) pipelineJobsTimer = setInterval(loadPipelineJobs, 6000);
    if (!active && pipelineJobsTimer) { clearInterval(pipelineJobsTimer); pipelineJobsTimer = null; }
  } catch (error) { toast(error.message, true); }
}

async function startAutoPipeline() {
  const button = $("#auto-pipeline-button");
  if (!state.batch) return;
  const numbers = state.selected.size ? selectedNumbers() : [];
  const scope = numbers.length ? `第 ${numbers.join("、")} 题` : "全部题目";
  openModal("一键全流程", `将对批次 ${state.batch} 的${scope}执行 Codex 题目质检、Claude 并行跑题、Codex 交付生产、交付质检和 Excel 导出。`, "开始执行", async () => {
    closeModal();
    const original = button.innerHTML;
    button.disabled = true;
    button.textContent = "启动中...";
    try {
      const payload = { batch: state.batch };
      if (numbers.length) payload.numbers = numbers;
      const result = await api("/api/actions/auto-pipeline", { method: "POST", body: JSON.stringify(payload) });
      toast(result.message || "自动流水线已启动");
      await loadPipelineJobs();
    } catch (error) { toast(error.message, true); }
    finally { button.innerHTML = original; button.disabled = false; refreshIcons(); }
  });
}

function authorJobStatus(job) {
  const labels = { queued: "排队中", running: "执行中", completed: "已完成", failed: "失败", interrupted: "已中断" };
  return labels[job.status] || job.status;
}

function renderAuthorJobs() {
  const list = $("#author-job-list");
  if (!list) return;
  if (!state.authorJobs.length) {
    list.innerHTML = '<div class="empty-state"><strong>暂无 Codex 出题任务</strong><span>提交任务后，过程日志会显示在这里。</span></div>';
    return;
  }
  list.innerHTML = state.authorJobs.map((job) => `
    <article class="author-job status-${escapeHtml(job.status)}">
      <div class="author-job-head"><div><strong>#${job.id} · ${escapeHtml(job.batch_name)}</strong><span>${job.question_count} 题 · 创建于 ${escapeHtml(formatDate(job.created_at))}</span></div><span class="job-status">${escapeHtml(authorJobStatus(job))}</span></div>
      <div class="author-job-meta"><span>${job.author_mode === "derived" ? `派生 · ${escapeHtml(job.task_type || "非 0-1")}` : "0-1 新建母题"}</span><span>开始：${escapeHtml(formatDate(job.started_at))}</span><span>结束：${escapeHtml(formatDate(job.finished_at))}</span><span>PID：${escapeHtml(job.pid || "-")}</span></div>
      ${job.error ? `<div class="author-job-error">${escapeHtml(job.error)}</div>` : ""}
      <pre class="author-job-output">${escapeHtml(job.output || job.last_message || "等待 Codex CLI 启动...")}</pre>
    </article>`).join("");
  scrollJobLogsToLatest();
  refreshIcons();
}

function startSinglePipeline(question, button) {
  openModal("单题跑全流程", `将只对 ${question.task_id} 执行题目质检、Claude 跑题、交付生产、交付质检和 Excel 导出。`, "开始执行", async () => {
    closeModal();
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<i data-lucide="loader-circle"></i>';
    refreshIcons();
    try {
      const result = await api("/api/actions/auto-pipeline", {
        method: "POST",
        body: JSON.stringify({ batch: state.batch, numbers: [question.question_no] }),
      });
      toast(result.message || "单题流水线已启动");
      await loadPipelineJobs();
      await loadDashboard();
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.innerHTML = original;
      button.disabled = false;
      refreshIcons();
    }
  });
}

function selectedAuthorMode() {
  return document.querySelector('input[name="author-mode"]:checked')?.value || "0-1";
}

function renderMotherLibrary() {
  const select = $("#author-mother");
  if (!select) return;
  const selected = select.value;
  select.innerHTML = '<option value="">请选择已通过基础检查的母项目</option>' + state.mothers.map((mother) =>
    `<option value="${mother.id}">${escapeHtml(mother.title)} · ${escapeHtml(mother.source_task_id)} · 已用 ${mother.use_count} 次</option>`
  ).join("");
  select.value = state.mothers.some((mother) => String(mother.id) === selected)
    ? selected
    : (state.mothers[0] ? String(state.mothers[0].id) : "");
  const mother = state.mothers.find((item) => String(item.id) === select.value);
  const taskType = $("#author-task-type");
  if (mother && !mother.bugfix_ready && mother.iteration_ready) taskType.value = "Feature 迭代";
  else if (mother && mother.bugfix_ready) taskType.value = "Bug 修复";
  $("#mother-summary").textContent = mother
    ? `代码路径：${mother.workspace_path}\nGit 地址：${mother.repo_url || "尚未登记"}\n初始快照：${mother.initial_snapshot || "尚未登记"}\n已派生使用：${mother.use_count} 次 · Bug 修复${mother.bugfix_ready ? "可用" : "不可用"} · Feature 迭代${mother.iteration_ready ? "可用" : "不可用"}`
    : "选择母库项目后显示代码路径、Git 地址、初始快照和使用次数。";
}

function renderAuthorMode() {
  const derived = selectedAuthorMode() === "derived";
  $("#derived-author-fields").hidden = !derived;
  $("#author-common-fields").hidden = derived;
  $("#author-notes-field").hidden = derived;
  $("#author-business").required = !derived;
  renderMotherLibrary();
  renderAuthorCommand();
}

function renderMotherModeAndCommand() {
  renderMotherLibrary();
  renderAuthorMode();
  renderAuthorCommand();
}

function retryAuthorJob(jobId) {
  openModal("从失败处重试", `将继续执行出题任务 #${jobId}。若批次已部分创建，将在现有批次基础上补齐。`, "开始重试", async () => {
    closeModal();
    const button = document.querySelector(`[data-author-retry="${jobId}"]`);
    if (button) button.disabled = true;
    try {
      const result = await api("/api/actions/codex-author-retry", {
        method: "POST", body: JSON.stringify({ job_id: jobId }),
      });
      toast(result.message || "重试出题任务已启动");
      await loadAuthorJobs();
    } catch (error) {
      toast(error.message, true);
      if (button) button.disabled = false;
    }
  });
}

async function loadAuthorJobs() {
  if (document.hidden) return;
  try {
    const result = await api("/api/author-jobs");
    state.authorJobs = (result.jobs || []).slice(0, 20).map((job) => ({
      ...job, output: (job.output || "").slice(-30000),
    }));
    const signature = JSON.stringify(state.authorJobs.map((job) => [
      job.id, job.status, job.output_length, job.last_message, job.error, job.can_retry,
    ]));
    if (signature !== authorJobsSignature) {
      authorJobsSignature = signature;
      renderAuthorJobs();
    }
    const active = state.authorJobs.some((job) => job.status === "queued" || job.status === "running");
    if (active && !authorJobsTimer) {
      authorJobsTimer = setInterval(loadAuthorJobs, 6000);
    } else if (!active && authorJobsTimer) {
      clearInterval(authorJobsTimer);
      authorJobsTimer = null;
    }
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadMothers() {
  try {
    const result = await api("/api/mother-library");
    state.mothers = result.mothers || [];
    renderMotherLibrary();
    renderAuthorMode();
    renderAuthorCommand();
  } catch (error) { toast(error.message, true); }
}

async function startCodexAuthorJob() {
  const button = $("#codex-author-button");
  const body = {
    batch: $("#author-batch").value.trim(),
    count: Number($("#author-count").value),
    business: $("#author-business").value.trim(),
    technology: $("#author-technology").value.trim(),
    notes: $("#author-notes").value.trim(),
    mode: selectedAuthorMode(),
    task_type: $("#author-task-type").value,
    mother_id: $("#author-mother").value ? Number($("#author-mother").value) : null,
    derived_notes: $("#author-derived-notes")?.value.trim() || "",
    defect_tolerance: $("#author-defect-tolerance")?.value.trim() || "",
  };
  button.disabled = true;
  const original = button.innerHTML;
  button.textContent = "提交中...";
  try {
    const result = await api("/api/actions/codex-author", { method: "POST", body: JSON.stringify(body) });
    toast(result.message || "Codex 出题任务已启动");
    await loadAuthorJobs();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.innerHTML = original;
    button.disabled = false;
    refreshIcons();
  }
}

function render() {
  renderBatchOptions();
  renderSummary();
  renderPipeline();
  renderView();
  $("#last-updated").textContent = `更新于 ${new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date())}`;
  refreshIcons();
}

async function loadDashboard(batch = state.batch) {
  $("#refresh-button").disabled = true;
  try {
    const query = batch ? `?batch=${encodeURIComponent(batch)}` : "";
    state.data = await api(`/api/dashboard${query}`);
    state.batch = state.data.batch?.name || null;
    state.selected.clear();
    render();
  } catch (error) {
    toast(error.message, true);
  } finally {
    $("#refresh-button").disabled = false;
  }
}

async function loadConfig() {
  try {
    state.config = await api("/api/config");
    if (state.view === "settings") renderSettings();
  } catch (error) {
    toast(error.message, true);
  }
}

function updateSelectionState() {
  const selectionMode = state.stage === 2;
  if (!selectionMode) state.selected.clear();
  $("#selection-tools").hidden = !selectionMode;
  $("#question-table").classList.toggle("selection-enabled", selectionMode);
  const visible = currentQuestions();
  const visibleIds = visible.map((question) => question.id);
  const checkedVisible = visibleIds.filter((id) => state.selected.has(id));
  const selectAll = $("#select-all");
  selectAll.checked = visibleIds.length > 0 && checkedVisible.length === visibleIds.length;
  selectAll.indeterminate = checkedVisible.length > 0 && checkedVisible.length < visibleIds.length;
  $("#selected-count").textContent = `已选 ${state.selected.size} 道`;
  const selectedQuestions = state.data.questions.filter((question) => state.selected.has(question.id));
  const launchButton = $("#launch-button");
  launchButton.hidden = state.stage !== 2;
  launchButton.disabled = selectedQuestions.length === 0 || selectedQuestions.some((question) => !question.can_launch);
}

async function copyText(value, successMessage = "已复制", announce = true) {
  try {
    await navigator.clipboard.writeText(value);
    if (announce) toast(successMessage);
  } catch {
    const input = document.createElement("textarea");
    input.value = value;
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.append(input);
    input.select();
    document.execCommand("copy");
    input.remove();
    if (announce) toast(successMessage);
  }
}

function selectedNumbers(useAll = false) {
  const chosen = useAll || state.selected.size === 0
    ? state.data.questions
    : state.data.questions.filter((question) => state.selected.has(question.id));
  return chosen.map((question) => question.question_no);
}

function workflowPrompt() {
  const batch = state.batch;
  const numbers = selectedNumbers();
  const scope = state.selected.size ? `第 ${numbers.join("、")} 题` : "全部题目";
  const stage = state.stage || Math.min(...state.data.questions.map((question) => question.stage_index));
  if (stage <= 1) {
    return `使用 $cc-usr-question-qc 质检批次 ${batch} 的${scope}。只检查批次内和跨批次是否存在重复题、近似题或换名套模板。不要读取任何 Claude Code 轨迹、回复或产物。`;
  }
  if (stage === 2) {
    return `使用 $cc-usr-claude-runner 启动批次 ${batch} 的${scope}。只启动已经通过题目质检的记录，不要分析轨迹或评分。`;
  }
  if (stage === 3) {
    return `使用 $cc-usr-delivery-producer 处理批次 ${batch} 的${scope}。自动定位对应的 Claude Code 会话，提取每轮 SessionID、PromptID 和当前对话轮次排序，读取轨迹、回复、代码、diff 与验证结果，严格按照 项目规范.md 的五维标准逐轮评分并写入 production.sqlite3。不要修改目标模型的代码或轨迹。`;
  }
  if (stage === 4) {
    return `使用 $cc-usr-delivery-qc 质检批次 ${batch} 的${scope}交付记录。逐项检查 28 个提交字段，包括当前对话轮次排序；遇到任何错误、警告、缺失字段或证据冲突都要立即根据 SQLite、对应会话和实际产物修正，并在同一任务中反复复检直到零错误零警告。只有全部符合规范后才将审核备注写为“质检通过”。不要导出 Excel。`;
  }
  return `使用 $cc-usr-excel-exporter 导出批次 ${batch} 的${scope}。只导出已经通过交付质检的记录，按照 A:AB 28 列生成全新 Excel，同时复制对应原始 JSONL 轨迹，Excel 的“轨迹文件”列保持空白。`;
}

function authorPrompt() {
  const batch = $("#author-batch").value.trim() || "<填写批次名>";
  const count = $("#author-count").value || "10";
  const business = $("#author-business").value.trim().replace(/\s+/g, " ");
  const technology = $("#author-technology").value.trim().replace(/\s+/g, " ");
  const notes = $("#author-notes").value.trim().replace(/\s+/g, " ");
  const mode = selectedAuthorMode();
  if (mode === "derived") {
    const mother = state.mothers.find((item) => String(item.id) === $("#author-mother").value);
    const taskType = $("#author-task-type").value;
    const derivedNotes = $("#author-derived-notes")?.value.trim().replace(/\s+/g, " ") || "";
    const tolerance = $("#author-defect-tolerance")?.value.trim().replace(/\s+/g, " ") || "";
    return `使用 $cc-usr-question-author 基于母库生成派生题目。\n批次名：${batch}\n题目数量：${count}\n题型：${taskType}\n母库项目：${mother ? `${mother.title}（ID ${mother.id}，代码路径 ${mother.workspace_path}，Git ${mother.repo_url || "待登记"}，已用 ${mother.use_count} 次）` : "系统自动选择符合规范的母库项目"}\n派生方向：${derivedNotes || "围绕母项目已有业务设计真实的后续工作"}\n可接受的小瑕疵：${tolerance || "允许不影响构建和主要流程的小问题，并记录为可迭代方向"}\n出题要求：根据母项目代码、已登记快照和《项目规范.md》自动生成，不需要额外填写关键词。\n严格遵守项目规范和母库引用规则，保留母题关系并完成独立快照与质检，不要启动目标模型。`;
  }
  const requirements = [
    business && `业务关键词：${business}`,
    technology && `技术关键词：${technology}`,
    notes && `补充要求：${notes}`,
  ].filter(Boolean).join("；") || "<填写出题关键词>";
  return `使用 $cc-usr-question-author 创建批次。\n批次名：${batch}\n题目数量：${count}\n出题要求：${requirements}\n严格遵守 项目规范.md。创建完成后运行出题机械质检，不要启动目标模型。`;
}

function renderAuthorCommand() {
  $("#author-command").value = authorPrompt();
  $("#author-status").hidden = true;
}

function openModal(title, description, confirmLabel, action) {
  $("#modal-title").textContent = title;
  $("#modal-description").textContent = description;
  $("#modal-confirm").textContent = confirmLabel;
  state.pendingAction = action;
  $("#modal-backdrop").hidden = false;
  $("#modal-confirm").focus();
}

function closeModal() {
  $("#modal-backdrop").hidden = true;
  state.pendingAction = null;
}

async function executeAction(path, body, busyButton, successMessage) {
  const original = busyButton?.innerHTML;
  if (busyButton) {
    busyButton.disabled = true;
    busyButton.textContent = "处理中…";
  }
  try {
    const result = await api(path, { method: "POST", body: JSON.stringify(body) });
    toast(successMessage || result.message || "操作完成");
    await loadDashboard(state.batch);
    return result;
  } catch (error) {
    toast(error.message, true);
    throw error;
  } finally {
    if (busyButton && original) {
      busyButton.innerHTML = original;
      busyButton.disabled = false;
      refreshIcons();
    }
  }
}

async function openQuestionDetail(id) {
  const drawer = $("#detail-drawer");
  if (drawerCloseTimer) clearTimeout(drawerCloseTimer);
  $("#drawer-body").innerHTML = `<div class="empty-state"><strong>正在读取题目…</strong></div>`;
  $("#drawer-backdrop").hidden = false;
  drawer.removeAttribute("inert");
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  try {
    const detail = await api(`/api/questions/${id}`);
    $("#drawer-task-id").textContent = detail.task_id;
    $("#drawer-title").textContent = detail.title;
    const runs = detail.runs.length ? detail.runs.map((run) => `
      <div class="record-block"><div class="detail-grid">
        <div class="detail-item"><span>SessionID</span><strong>${escapeHtml(run.session_id || "等待写入")}</strong></div>
        <div class="detail-item"><span>Harness 版本</span><strong>${escapeHtml(run.harness_version || "暂无")}</strong></div>
        <div class="detail-item"><span>运行批次</span><strong>${escapeHtml(run.batch_run_id)}</strong></div>
        <div class="detail-item"><span>启动时间</span><strong>${escapeHtml(formatDate(run.launched_at))}</strong></div>
      </div></div>`).join("") : `<p class="prompt-box">尚未启动 Claude Code。</p>`;
    const scoreLabels = [["delivery", "交付"], ["instruction", "指令"], ["planning", "规划"], ["reasoning", "推理"], ["execution", "执行"]];
    const records = detail.records.length ? detail.records.map((record) => `
      <div class="record-block">
        <div class="record-head"><strong>第 ${record.turn_no} 轮 · ${escapeHtml(record.record_id)}</strong><span class="tag ${record.delivery_qc_passed ? "green" : "amber"}">${record.delivery_qc_passed ? "质检通过" : "待质检"}</span></div>
        <div class="score-grid">${scoreLabels.map(([key, label]) => `<div class="score-box"><span>${label}</span><strong>${record[`${key}_score`]}</strong></div>`).join("")}</div>
        <div class="description-list">${scoreLabels.map(([key, label]) => `<details><summary>${label}描述</summary><p>${escapeHtml(record[`${key}_description`])}</p></details>`).join("")}</div>
      </div>`).join("") : `<p class="prompt-box">尚未生成交付记录。</p>`;
    $("#drawer-body").innerHTML = `
      <section class="drawer-section"><h3>题目信息</h3><div class="detail-grid">
        <div class="detail-item"><span>任务类型</span><strong>${escapeHtml(detail.task_type)}</strong></div>
        <div class="detail-item"><span>任务难度</span><strong>${escapeHtml(detail.difficulty)}</strong></div>
        <div class="detail-item"><span>语言 / 框架</span><strong>${escapeHtml(detail.languages)}</strong></div>
        <div class="detail-item"><span>复现等级</span><strong>${escapeHtml(detail.reproducibility)}</strong></div>
      </div><div class="link-row">
        <a class="button secondary" href="${escapeHtml(detail.repo_url)}" target="_blank" rel="noreferrer"><i data-lucide="github"></i>仓库</a>
        <a class="button secondary" href="${escapeHtml(detail.initial_snapshot)}" target="_blank" rel="noreferrer"><i data-lucide="git-commit-horizontal"></i>初始快照</a>
        <button class="button secondary" data-detail-copy="prompt"><i data-lucide="copy"></i>复制 Prompt</button>
      </div></section>
      <section class="drawer-section"><h3>User Prompt</h3><p class="prompt-box" id="detail-prompt"></p></section>
      <section class="drawer-section"><h3>模型运行</h3>${runs}</section>
      <section class="drawer-section"><h3>交付评分</h3>${records}</section>`;
    $("#detail-prompt").textContent = detail.prompt;
    $("[data-detail-copy='prompt']").addEventListener("click", () => copyText(detail.prompt, "Prompt 已复制"));
    refreshIcons();
  } catch (error) {
    $("#drawer-body").innerHTML = `<div class="empty-state"><strong>详情读取失败</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
}

function closeDrawer() {
  $("#detail-drawer").classList.remove("open");
  $("#detail-drawer").setAttribute("aria-hidden", "true");
  drawerCloseTimer = setTimeout(() => {
    $("#drawer-backdrop").hidden = true;
    $("#detail-drawer").setAttribute("inert", "");
    drawerCloseTimer = null;
  }, 180);
}

$("#refresh-button").addEventListener("click", () => loadDashboard());
$("#batch-select").addEventListener("change", (event) => {
  state.stage = null;
  loadDashboard(event.target.value);
});
$("#search-input").addEventListener("input", (event) => { state.search = event.target.value; renderRows(); });
$("#select-all").addEventListener("change", (event) => {
  currentQuestions().forEach((question) => event.target.checked ? state.selected.add(question.id) : state.selected.delete(question.id));
  renderRows();
});
$("#filter-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-filter]");
  if (!button) return;
  state.filter = button.dataset.filter;
  $$(".filter-tab").forEach((item) => item.classList.toggle("active", item === button));
  renderRows();
});
$("#pipeline-track").addEventListener("click", (event) => {
  const button = event.target.closest("[data-stage]");
  if (!button) return;
  const selectedStage = Number(button.dataset.stage);
  state.stage = state.stage === selectedStage ? null : selectedStage;
  if (state.stage !== 2) state.selected.clear();
  renderPipeline();
  renderRows();
});
$("#question-rows").addEventListener("change", (event) => {
  if (!event.target.classList.contains("row-check")) return;
  const id = Number(event.target.closest("tr").dataset.id);
  event.target.checked ? state.selected.add(id) : state.selected.delete(id);
  updateSelectionState();
});
$("#question-rows").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const id = Number(button.closest("tr").dataset.id);
  const question = state.data.questions.find((item) => item.id === id);
  if (button.dataset.action === "detail") openQuestionDetail(id);
  if (button.dataset.action === "copy") {
    const detail = await api(`/api/questions/${id}`);
    copyText(detail.prompt, "Prompt 已复制");
  }
  if (button.dataset.action === "folder") {
    executeAction("/api/actions/open", { kind: "question", id }, button, `已打开 ${question.task_id} 目录`).catch(() => {});
  }
  if (button.dataset.action === "single-pipeline") startSinglePipeline(question, button);
});
$("#open-batch").addEventListener("click", () => executeAction("/api/actions/open", { kind: "batch", id: state.batch }, $("#open-batch"), "批次目录已打开").catch(() => {}));
$("#qc-check").addEventListener("click", async () => {
  try {
    const result = await executeAction("/api/actions/qc-check", { batch: state.batch }, $("#qc-check"), "交付记录复检通过");
    if (result.output) console.info(result.output);
  } catch {}
});
$("#workflow-button").addEventListener("click", () => copyText(workflowPrompt(), "当前阶段指令已复制"));
$("#launch-button").addEventListener("click", () => {
  const numbers = selectedNumbers();
  const terminal = state.config?.terminal || "终端";
  openModal("启动 Claude Code", `将为第 ${numbers.join("、")} 题分别打开新的 ${terminal} 会话。`, "确认启动", async () => {
    closeModal();
    await executeAction("/api/actions/launch", { batch: state.batch, numbers }, $("#launch-button"), "Claude Code 会话已启动");
  });
});
$("#author-form").addEventListener("input", renderAuthorCommand);
document.querySelectorAll('input[name="author-mode"]').forEach((input) => input.addEventListener("change", renderAuthorMode));
$("#author-mother").addEventListener("change", renderMotherModeAndCommand);
$("#author-task-type").addEventListener("change", renderAuthorCommand);
$("#author-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await copyText(authorPrompt(), "出题命令已复制", false);
  const status = $("#author-status");
  status.querySelector("span").textContent = "出题命令已复制";
  status.hidden = false;
  refreshIcons();
});
$("#codex-author-button").addEventListener("click", startCodexAuthorJob);
$("#author-jobs-refresh").addEventListener("click", loadAuthorJobs);
$("#author-job-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-author-retry]");
  if (button) retryAuthorJob(Number(button.dataset.authorRetry));
});
$("#export-button").addEventListener("click", () => {
  const numbers = state.selected.size ? selectedNumbers() : [];
  const scope = numbers.length ? `第 ${numbers.join("、")} 题` : "整个批次";
  openModal("导出交付文件", `将导出${scope}中已经通过交付质检的完整记录，并复制对应原始 JSONL。现有文件不会被覆盖。`, "确认导出", async () => {
    closeModal();
    await executeAction("/api/actions/export", { batch: state.batch, numbers }, $("#export-button"), "Excel 和轨迹文件已生成");
  });
});
$(".nav-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-view]");
  if (!button) return;
  state.view = button.dataset.view;
  state.stage = null;
  if (state.view === "settings" && !state.config) loadConfig();
  renderView();
});
$("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#save-config");
  const original = button.innerHTML;
  button.disabled = true;
  button.textContent = "保存中…";
  try {
    const result = await api("/api/config", {
      method: "POST",
      body: JSON.stringify({
        base_url: $("#config-base-url").value,
        model: $("#config-model").value,
        api_key: $("#config-api-key").value,
        submitter: $("#config-submitter").value,
        docker_image: $("#config-docker-image").value,
        docker_command: $("#config-docker-command").value,
        qc_concurrency: Number($("#config-qc-concurrency").value),
        model_concurrency: Number($("#config-model-concurrency").value),
        codex_concurrency: Number($("#config-codex-concurrency").value),
      }),
    });
    state.config = result.config;
    renderSettings();
    toast("运行配置已保存");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.innerHTML = original;
    button.disabled = false;
    refreshIcons();
  }
});
$("#environment-repair").addEventListener("click", () => loadEnvironment(true));
$("#toggle-api-key").addEventListener("click", () => {
  const input = $("#config-api-key");
  const showing = input.type === "text";
  input.type = showing ? "password" : "text";
  $("#toggle-api-key").title = showing ? "显示 API Key" : "隐藏 API Key";
  $("#toggle-api-key").setAttribute("aria-label", showing ? "显示 API Key" : "隐藏 API Key");
  $("#toggle-api-key").innerHTML = `<i data-lucide="${showing ? "eye" : "eye-off"}"></i>`;
  refreshIcons();
});
$("#file-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-file-path]");
  if (button) copyText(button.dataset.filePath, "文件路径已复制");
});
$("#auto-pipeline-button").addEventListener("click", startAutoPipeline);
$("#pipeline-jobs-refresh").addEventListener("click", loadPipelineJobs);
$("#pipeline-job-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-pipeline-retry]");
  if (button) retryPipelineJob(Number(button.dataset.pipelineRetry));
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    loadPipelineJobs();
    loadAuthorJobs();
  }
});
$("#drawer-close").addEventListener("click", closeDrawer);
$("#drawer-backdrop").addEventListener("click", closeDrawer);
$("#modal-cancel").addEventListener("click", closeModal);
$("#modal-confirm").addEventListener("click", async () => {
  const action = state.pendingAction;
  if (action) await action().catch(() => {});
});
$("#modal-backdrop").addEventListener("click", (event) => { if (event.target === $("#modal-backdrop")) closeModal(); });
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeDrawer();
    closeModal();
  }
});

refreshIcons();
renderAuthorCommand();
loadDashboard();
loadConfig();
loadEnvironment();
loadAuthorJobs();
loadMothers();
loadPipelineJobs();
