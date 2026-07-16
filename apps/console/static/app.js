(function () {
  const BASE_PATH = String(window.__BASE_PATH__ || "").replace(/\/$/, "");
  const apiUrl = (path) => `${BASE_PATH}${path.startsWith("/") ? path : `/${path}`}`;

  const ACTIVE_STATUSES = new Set(["queued", "running", "stopping"]);
  const DONE_STATUSES = new Set(["completed", "stopped"]);
  const FAILED_STATUSES = new Set(["failed", "partial"]);

  const state = {
    tasks: [],
    selectedTaskId: null,
    taskFilter: "all",
    taskSearch: "",
    templates: [],
    lastHealth: null,
    lastLogLines: [],
    autoScroll: true,
    refreshingTasks: false,
    refreshingDetail: false,
    refreshingHealth: false,
  };

  const taskListEl = document.getElementById("taskList");
  const taskListMetaEl = document.getElementById("taskListMeta");
  const taskFiltersEl = document.getElementById("taskFilters");
  const detailTitleEl = document.getElementById("detailTitle");
  const detailMetaEl = document.getElementById("detailMeta");
  const detailSummaryEl = document.getElementById("detailSummary");
  const consoleOutputEl = document.getElementById("consoleOutput");
  const consoleMetaEl = document.getElementById("consoleMeta");
  const formEl = document.getElementById("taskForm");
  const settingsFormEl = document.getElementById("settingsForm");
  const formMessageEl = document.getElementById("formMessage");
  const settingsMessageEl = document.getElementById("settingsMessage");
  const stopBtnEl = document.getElementById("stopBtn");
  const refreshBtnEl = document.getElementById("refreshBtn");
  const healthRefreshBtnEl = document.getElementById("healthRefreshBtn");
  const toggleSettingsBtnEl = document.getElementById("toggleSettingsBtn");
  const toggleSettingsHeadBtnEl = document.getElementById("toggleSettingsHeadBtn");
  const toggleAdvancedBtnEl = document.getElementById("toggleAdvancedBtn");
  const toggleMailBtnEl = document.getElementById("toggleMailBtn");
  const advancedFieldsEl = document.getElementById("advancedFields");
  const healthGridEl = document.getElementById("healthGrid");
  const healthMetaEl = document.getElementById("healthMeta");
  const overviewRunningEl = document.getElementById("overviewRunning");
  const overviewRunningHintEl = document.getElementById("overviewRunningHint");
  const overviewPoolTotalEl = document.getElementById("overviewPoolTotal");
  const overviewPoolHintEl = document.getElementById("overviewPoolHint");
  const overviewHealthEl = document.getElementById("overviewHealth");
  const overviewHealthHintEl = document.getElementById("overviewHealthHint");
  const taskSearchInputEl = document.getElementById("taskSearchInput");
  const cleanupDoneBtnEl = document.getElementById("cleanupDoneBtn");
  const cleanupFailedBtnEl = document.getElementById("cleanupFailedBtn");
  const cleanupAllTerminalBtnEl = document.getElementById("cleanupAllTerminalBtn");
  const preflightBtnEl = document.getElementById("preflightBtn");
  const preflightBoxEl = document.getElementById("preflightBox");
  const autoScrollToggleEl = document.getElementById("autoScrollToggle");
  const downloadLogBtnEl = document.getElementById("downloadLogBtn");
  const templateSelectEl = document.getElementById("templateSelect");
  const applyTemplateBtnEl = document.getElementById("applyTemplateBtn");
  const saveTemplateBtnEl = document.getElementById("saveTemplateBtn");
  const deleteTemplateBtnEl = document.getElementById("deleteTemplateBtn");

  function boolish(value) {
    return value === true || value === 1 || value === "1" || value === "true";
  }

  function escapeHtml(value) {
    return String(value || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
  }

  function setSettingsOpen(open) {
    settingsFormEl.classList.toggle("hidden", !open);
    const label = open ? "收起配置" : "展开配置";
    const topLabel = open ? "收起设置" : "系统设置";
    if (toggleSettingsHeadBtnEl) toggleSettingsHeadBtnEl.textContent = label;
    if (toggleSettingsBtnEl) toggleSettingsBtnEl.textContent = topLabel;
  }

  function setDefaults() {
    const defaults = window.__DEFAULTS__ || {};
    formEl.elements.name.value = `grok-task-${Date.now()}`;
    formEl.elements.count.value = defaults.run?.count || 50;
    const concurrent = Number(defaults.max_concurrent_tasks || 1);
    const concurrentCap = Number(defaults.max_concurrent_tasks_cap || 8);
    if (settingsFormEl.elements.max_concurrent_tasks) {
      settingsFormEl.elements.max_concurrent_tasks.min = 1;
      settingsFormEl.elements.max_concurrent_tasks.max = concurrentCap;
      settingsFormEl.elements.max_concurrent_tasks.value = concurrent;
    }
    const concurrentValueEl = document.getElementById("maxConcurrentValue");
    if (concurrentValueEl) {
      concurrentValueEl.textContent = String(concurrent);
    }
    settingsFormEl.elements.proxy.value = defaults.proxy || "";
    settingsFormEl.elements.browser_proxy.value = defaults.browser_proxy || "";
    settingsFormEl.elements.temp_mail_api_base.value = defaults.temp_mail_api_base || "";
    settingsFormEl.elements.temp_mail_admin_password.value = defaults.temp_mail_admin_password || "";
    settingsFormEl.elements.temp_mail_domain.value = defaults.temp_mail_domain || "";
    settingsFormEl.elements.temp_mail_site_password.value = defaults.temp_mail_site_password || "";
    settingsFormEl.elements.api_endpoint.value = defaults.api?.endpoint || "";
    if (settingsFormEl.elements.api_import_endpoint) {
      settingsFormEl.elements.api_import_endpoint.value = defaults.api?.import_endpoint || "";
    }
    if (settingsFormEl.elements.api_admin_username) {
      settingsFormEl.elements.api_admin_username.value = defaults.api?.admin_username || "admin";
    }
    if (settingsFormEl.elements.api_admin_password) {
      settingsFormEl.elements.api_admin_password.value = "";
      const configured = boolish(defaults.api?.admin_password_configured || defaults.api?.admin_password);
      settingsFormEl.elements.api_admin_password.placeholder = configured
        ? "已配置（留空保存则沿用）"
        : "未配置";
    }
    settingsFormEl.elements.api_token.value = defaults.api?.token || "";
    settingsFormEl.elements.api_append.checked = defaults.api?.append !== false;
    formEl.elements.api_append.checked = false;
  }

  function statusClass(status) {
    return `status-pill status-${status || "unknown"}`;
  }

  function healthClass(ok) {
    return ok ? "health-pill health-ok" : "health-pill health-bad";
  }

  function progressPercents(task) {
    const target = Math.max(1, Number(task.target_count || 1));
    const success = Math.max(0, Number(task.completed_count || 0));
    const failed = Math.max(0, Number(task.failed_count || 0));
    const pushed = Math.max(0, Number(task.pushed_count || 0));
    const successPct = Math.min(100, Math.round((success / target) * 100));
    const failedPct = Math.min(100 - successPct, Math.round((failed / target) * 100));
    const pushedPct = Math.min(100, Math.round((pushed / target) * 100));
    return { successPct, failedPct, pushedPct, success, failed, pushed, target };
  }

  function filterTasks(tasks) {
    const q = String(state.taskSearch || "").trim().toLowerCase();
    return (tasks || []).filter((task) => {
      const status = String(task.status || "");
      if (state.taskFilter === "active" && !ACTIVE_STATUSES.has(status)) return false;
      if (state.taskFilter === "done" && !DONE_STATUSES.has(status)) return false;
      if (state.taskFilter === "failed" && !FAILED_STATUSES.has(status)) return false;
      if (!q) return true;
      const hay = `${task.id} ${task.name || ""} ${task.last_error || ""} ${task.error_summary || ""}`.toLowerCase();
      return hay.includes(q);
    });
  }

  function pushGapOf(task) {
    if (task.push_gap != null) return Math.max(0, Number(task.push_gap) || 0);
    const completed = Math.max(0, Number(task.completed_count || 0));
    const pushed = Math.max(0, Number(task.pushed_count || 0));
    return Math.max(0, completed - pushed);
  }

  function errorSummaryOf(task) {
    const raw = task.error_summary || task.last_error || "";
    return String(raw || "").trim();
  }

  const ERROR_TYPE_LABELS = {
    mail: "邮箱",
    proxy: "代理",
    captcha: "验证码",
    xai: "注册",
    import: "入池",
    other: "其他",
    unknown: "未知",
  };

  function errorTypeLabel(type) {
    return ERROR_TYPE_LABELS[type] || type || "未知";
  }

  function errorCountsOf(task) {
    return task.error_counts || {};
  }

  function topErrorTypeOf(task) {
    return String(task.top_error_type || "").trim();
  }

  function renderErrorTypePills(task, { compact = false } = {}) {
    const counts = errorCountsOf(task);
    const top = topErrorTypeOf(task);
    const entries = Object.entries(counts)
      .filter(([, n]) => Number(n) > 0)
      .sort((a, b) => Number(b[1]) - Number(a[1]));
    if (!entries.length) return "";
    const shown = compact ? entries.slice(0, 3) : entries;
    return `
      <div class="error-type-pills">
        ${shown.map(([type, n]) => `
          <span class="error-type-pill ${type} ${type === top ? "top" : ""}">${escapeHtml(errorTypeLabel(type))} ${Number(n)}</span>
        `).join("")}
      </div>
    `;
  }

  function renderTemplates() {
    if (!templateSelectEl) return;
    const current = templateSelectEl.value;
    const options = ['<option value="">不使用模板</option>']
      .concat((state.templates || []).map((tpl) => {
        const id = escapeHtml(tpl.id || tpl.name || "");
        const name = escapeHtml(tpl.name || "未命名模板");
        const count = tpl.count != null ? ` · ${tpl.count}次` : "";
        return `<option value="${id}">${name}${count}</option>`;
      }));
    templateSelectEl.innerHTML = options.join("");
    if (current && [...templateSelectEl.options].some((o) => o.value === current)) {
      templateSelectEl.value = current;
    }
  }

  async function refreshTemplates() {
    const data = await fetchJson("/api/templates");
    state.templates = data.templates || [];
    renderTemplates();
  }

  function findTemplate(idOrName) {
    const key = String(idOrName || "").trim();
    if (!key) return null;
    return (state.templates || []).find((tpl) => String(tpl.id || "") === key || String(tpl.name || "") === key) || null;
  }

  function applyTemplateToForm(tpl) {
    if (!tpl) return;
    if (tpl.name) {
      // Keep a fresh batch-ish name, but preserve template prefix.
      const base = String(tpl.name).replace(/-\d+$/, "");
      formEl.elements.name.value = `${base}-${Date.now().toString().slice(-6)}`;
    }
    if (tpl.count != null) formEl.elements.count.value = tpl.count;
    const fields = [
      "proxy",
      "browser_proxy",
      "temp_mail_api_base",
      "temp_mail_admin_password",
      "temp_mail_domain",
      "temp_mail_site_password",
      "api_endpoint",
      "api_import_endpoint",
      "api_admin_username",
      "api_admin_password",
      "api_token",
    ];
    // Ensure advanced fields visible if template has overrides.
    let hasOverride = false;
    fields.forEach((key) => {
      if (!formEl.elements[key]) return;
      const value = tpl[key];
      if (value != null && String(value).trim() !== "") {
        formEl.elements[key].value = value;
        hasOverride = true;
      }
    });
    if (formEl.elements.api_append) {
      if (tpl.api_append === true || tpl.api_append === false) {
        formEl.elements.api_append.checked = !!tpl.api_append;
        hasOverride = true;
      }
    }
    if (hasOverride && advancedFieldsEl.classList.contains("hidden")) {
      advancedFieldsEl.classList.remove("hidden");
      toggleAdvancedBtnEl.textContent = "收起高级设置";
    }
  }

  function buildTemplatePayloadFromForm() {
    const name = window.prompt("模板名称", formEl.elements.name.value.trim() || `模板-${Date.now().toString().slice(-4)}`);
    if (!name) return null;
    const payload = buildTaskPayload();
    return {
      name: name.trim(),
      count: payload.count,
      proxy: payload.proxy || "",
      browser_proxy: payload.browser_proxy || "",
      temp_mail_api_base: payload.temp_mail_api_base || "",
      temp_mail_admin_password: payload.temp_mail_admin_password || "",
      temp_mail_domain: payload.temp_mail_domain || "",
      temp_mail_site_password: payload.temp_mail_site_password || "",
      api_endpoint: payload.api_endpoint || "",
      api_import_endpoint: payload.api_import_endpoint || "",
      api_admin_username: payload.api_admin_username || "",
      api_admin_password: payload.api_admin_password || "",
      api_token: payload.api_token || "",
      api_append: formEl.elements.api_append.checked ? true : null,
      notes: "",
    };
  }

  function renderOverview() {
    const tasks = state.tasks || [];
    const running = tasks.filter((t) => t.status === "running").length;
    const queued = tasks.filter((t) => t.status === "queued").length;
    const stopping = tasks.filter((t) => t.status === "stopping").length;
    if (overviewRunningEl) overviewRunningEl.textContent = String(running + queued + stopping);
    if (overviewRunningHintEl) {
      overviewRunningHintEl.textContent = `running ${running} · queued ${queued} · stopping ${stopping}`;
    }

    const pool = (state.lastHealth && state.lastHealth.pool) || {};
    const providers = pool.providers || {};
    if (overviewPoolTotalEl) {
      overviewPoolTotalEl.textContent = pool.total != null ? String(pool.total) : "-";
    }
    if (overviewPoolHintEl) {
      const bits = [];
      if (providers.grok_web != null) bits.push(`web ${providers.grok_web}`);
      if (providers.grok_build != null) bits.push(`build ${providers.grok_build}`);
      if (providers.grok_console != null) bits.push(`console ${providers.grok_console}`);
      overviewPoolHintEl.textContent = bits.length ? bits.join(" · ") : "web / build / console";
    }

    const items = (state.lastHealth && state.lastHealth.items) || [];
    if (overviewHealthEl) {
      if (!items.length) {
        overviewHealthEl.textContent = "-";
      } else {
        const okCount = items.filter((item) => item.ok).length;
        overviewHealthEl.textContent = `${okCount}/${items.length}`;
      }
    }
    if (overviewHealthHintEl) {
      overviewHealthHintEl.textContent = state.lastHealth?.checked_at
        ? `检测于 ${state.lastHealth.checked_at}`
        : "等待检测";
    }
  }

  function renderHealth(data) {
    state.lastHealth = data || null;
    const items = data.items || [];
    const pool = data.pool || {};
    const poolBits = [];
    if (pool.total != null) poolBits.push(`号池 ${pool.total}`);
    const providers = pool.providers || {};
    if (providers.grok_web != null) poolBits.push(`web ${providers.grok_web}`);
    if (providers.grok_build != null) poolBits.push(`build ${providers.grok_build}`);
    if (providers.grok_console != null) poolBits.push(`console ${providers.grok_console}`);
    healthMetaEl.textContent = [
      `最近检测时间 ${data.checked_at || "-"}`,
      poolBits.length ? poolBits.join(" · ") : "",
    ].filter(Boolean).join(" | ");

    if (!items.length) {
      healthGridEl.innerHTML = '<div class="empty">暂无健康检查结果</div>';
      renderOverview();
      return;
    }

    healthGridEl.innerHTML = items.map((item) => {
      const isPool = item.key === "pool"
        || String(item.label || "").toLowerCase().includes("pool")
        || String(item.label || "").includes("号池");
      const extra = isPool && pool.import_summary
        ? `<div class="health-detail">import: ${escapeHtml(pool.import_summary)}</div>`
        : "";
      return `
      <div class="health-card ${isPool ? "health-card-pool" : ""}">
        <div class="task-row">
          <strong>${escapeHtml(item.label)}</strong>
          <span class="${healthClass(item.ok)}">${item.ok ? "正常" : "异常"}</span>
        </div>
        <div class="health-summary">${escapeHtml(item.summary || "-")}</div>
        <div class="health-target">${escapeHtml(item.target || "-")}</div>
        <div class="health-detail">${escapeHtml(item.detail || "-")}</div>
        ${extra}
      </div>`;
    }).join("");
    renderOverview();
  }

  function renderTaskList() {
    const filtered = filterTasks(state.tasks);
    if (taskListMetaEl) {
      taskListMetaEl.textContent = `显示 ${filtered.length} / ${state.tasks.length} 个任务`;
    }
    if (!state.tasks.length) {
      taskListEl.innerHTML = '<div class="empty">暂无任务</div>';
      renderOverview();
      return;
    }
    if (!filtered.length) {
      taskListEl.innerHTML = '<div class="empty">当前筛选下没有任务</div>';
      renderOverview();
      return;
    }

    taskListEl.innerHTML = filtered.map((task) => {
      const p = progressPercents(task);
      const selected = task.id === state.selectedTaskId ? "selected" : "";
      const gap = pushGapOf(task);
      const err = errorSummaryOf(task);
      const cardClass = [
        "task-card",
        selected,
        gap > 0 ? "has-gap" : "",
        err ? "has-error" : "",
      ].filter(Boolean).join(" ");
      const gapLine = gap > 0
        ? `<div class="task-gap-line">成功未入池 ${gap}</div>`
        : "";
      const errLine = err
        ? `<div class="task-error-line" title="${escapeHtml(task.last_error || err)}">${escapeHtml(err)}</div>`
        : "";
      const topType = topErrorTypeOf(task);
      const typeLine = topType
        ? `<div class="error-type-line">主因 ${escapeHtml(errorTypeLabel(topType))}${task.top_error_count ? ` ×${task.top_error_count}` : ""}</div>${renderErrorTypePills(task, { compact: true })}`
        : "";
      return `
      <div class="${cardClass}" data-task-id="${task.id}" role="button" tabindex="0" aria-label="查看任务 #${task.id}">
        <div class="task-row">
          <strong class="task-title" title="${escapeHtml(task.name)}">#${task.id} ${escapeHtml(task.name)}</strong>
          <span class="${statusClass(task.status)}">${escapeHtml(task.status)}</span>
        </div>
        <div class="task-subrow">目标 ${p.target} · 成功 ${p.success} · 入池 ${p.pushed} · 失败 ${p.failed}</div>
        <div class="progress-track" aria-hidden="true">
          <span class="progress-success" style="width:${p.successPct}%"></span>
          <span class="progress-failed" style="width:${p.failedPct}%"></span>
        </div>
        <div class="task-subrow task-progress-meta">进度 ${p.successPct}% · 入池 ${p.pushedPct}%</div>
        ${gapLine}
        ${typeLine}
        ${errLine}
        <div class="task-actions">
          <span class="task-action-hint">点击查看日志</span>
          <button class="button button-danger button-small" type="button" data-delete-task-id="${task.id}">删除</button>
        </div>
      </div>`;
    }).join("");

    taskListEl.querySelectorAll(".task-card[data-task-id]").forEach((card) => {
      const selectTask = () => {
        state.selectedTaskId = Number(card.dataset.taskId);
        renderTaskList();
        refreshDetail();
      };
      card.addEventListener("click", (event) => {
        if (event.target.closest("[data-delete-task-id]")) return;
        selectTask();
      });
      card.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectTask();
        }
      });
    });

    taskListEl.querySelectorAll("[data-delete-task-id]").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.stopPropagation();
        const taskId = Number(button.dataset.deleteTaskId);
        const confirmed = window.confirm(`确认删除任务 #${taskId} 吗？`);
        if (!confirmed) return;
        try {
          await fetchJson(`/api/tasks/${taskId}`, { method: "DELETE" });
          if (state.selectedTaskId === taskId) {
            state.selectedTaskId = null;
            detailTitleEl.textContent = "实时控制台";
            detailSummaryEl.innerHTML = "";
            detailMetaEl.innerHTML = "";
            detailMetaEl.classList.add("hidden");
            consoleOutputEl.textContent = "选择任务后显示输出";
            if (consoleMetaEl) consoleMetaEl.textContent = "选择任务后显示输出";
            if (toggleMailBtnEl) toggleMailBtnEl.textContent = "任务参数";
          }
          await refreshTasks();
          await refreshDetail();
        } catch (error) {
          formMessageEl.textContent = error.message;
          formMessageEl.className = "form-message error";
        }
      });
    });

    renderOverview();
  }

  function renderTaskDetail(task) {
    detailTitleEl.textContent = `任务 #${task.id} · ${task.name}`;
    stopBtnEl.disabled = !ACTIVE_STATUSES.has(task.status);
    if (downloadLogBtnEl) downloadLogBtnEl.disabled = false;
    const p = progressPercents(task);
    const gap = pushGapOf(task);
    const err = errorSummaryOf(task);
    const summaryItems = [
      ["状态", task.status, ""],
      ["目标", task.target_count, ""],
      ["成功", task.completed_count, ""],
      ["入池", task.pushed_count || 0, ""],
      ["入池缺口", gap, gap > 0 ? "warn" : ""],
      ["失败", task.failed_count, Number(task.failed_count || 0) > 0 ? "danger" : ""],
      ["新增/更新", `${task.pushed_created || 0}/${task.pushed_updated || 0}`, ""],
      ["进度", `${p.successPct}%`, ""],
      ["入池进度", `${p.pushedPct}%`, ""],
      ["轮次", task.current_round, ""],
      ["阶段", task.current_phase || "-", ""],
      ["最近错误", err || "-", err ? "danger" : ""],
    ];
    const topType = topErrorTypeOf(task);
    if (topType) {
      summaryItems.splice(5, 0, ["主因类型", `${errorTypeLabel(topType)}${task.top_error_count ? ` ×${task.top_error_count}` : ""}`, "danger"]);
    }
    detailSummaryEl.innerHTML = summaryItems.map(([label, value, klass]) => `
      <div class="summary-item ${klass}">
        <div class="meta-item-label">${escapeHtml(label)}</div>
        <div class="meta-item-value">${escapeHtml(value)}</div>
      </div>
    `).join("") + `
      <div class="summary-item summary-error-types full">
        <div class="meta-item-label">错误类型聚合</div>
        <div class="meta-item-value">${renderErrorTypePills(task) || "暂无分类错误"}</div>
      </div>
      <div class="summary-item summary-progress full ${gap > 0 ? "warn" : ""}">
        <div class="meta-item-label">完成进度${gap > 0 ? ` · 有 ${gap} 个成功未入池` : ""}</div>
        <div class="progress-track progress-track-lg" aria-hidden="true">
          <span class="progress-success" style="width:${p.successPct}%"></span>
          <span class="progress-failed" style="width:${p.failedPct}%"></span>
        </div>
      </div>`;

    const cfg = task.config || {};
    detailMetaEl.innerHTML = [
      ["邮箱 API Base", cfg.temp_mail_api_base || "-"],
      ["邮箱域名", cfg.temp_mail_domain || "-"],
      ["邮箱管理密码", cfg.temp_mail_admin_password || "-"],
      ["站点密码", cfg.temp_mail_site_password || "-"],
      ["请求代理", cfg.proxy || "-"],
      ["浏览器代理", cfg.browser_proxy || "-"],
      ["Login", (cfg.api && cfg.api.endpoint) || cfg.api_endpoint || "-"],
      ["Import", (cfg.api && cfg.api.import_endpoint) || cfg.api_import_endpoint || "-"],
      ["最近邮箱", task.last_email || "-"],
      ["最近错误", task.last_error || "-"],
      ["创建时间", task.created_at || "-"],
      ["开始时间", task.started_at || "-"],
      ["结束时间", task.finished_at || "-"],
      ["PID", task.pid || "-"],
    ].map(([label, value]) => `
      <div class="meta-item">
        <div class="meta-item-label">${escapeHtml(label)}</div>
        <div class="meta-item-value">${escapeHtml(value)}</div>
      </div>
    `).join("");
  }

  function getAuthToken() {
    try {
      const params = new URLSearchParams(window.location.search || "");
      const fromQuery = (params.get("token") || "").trim();
      if (fromQuery) {
        sessionStorage.setItem("console_auth_token", fromQuery);
        return fromQuery;
      }
      const fromPage = String(window.__PAGE_TOKEN__ || "").trim();
      if (fromPage) {
        sessionStorage.setItem("console_auth_token", fromPage);
        return fromPage;
      }
      return (sessionStorage.getItem("console_auth_token") || "").trim();
    } catch (error) {
      return "";
    }
  }

  async function fetchJson(url, options) {
    const opts = { ...(options || {}) };
    const headers = new Headers(opts.headers || {});
    const token = getAuthToken();
    if (token && !headers.has("Authorization")) {
      headers.set("Authorization", `Bearer ${token}`);
    }
    opts.headers = headers;
    const response = await fetch(apiUrl(url), opts);
    let data = {};
    try {
      data = await response.json();
    } catch (error) {
      data = {};
    }
    if (!response.ok) {
      const detail = data.detail;
      const message = typeof detail === "string"
        ? detail
        : (Array.isArray(detail) ? detail.map((x) => x.msg || JSON.stringify(x)).join("; ") : "Request failed");
      throw new Error(message || `HTTP ${response.status}`);
    }
    return data;
  }

  async function refreshTasks() {
    if (state.refreshingTasks) return;
    state.refreshingTasks = true;
    try {
      const data = await fetchJson("/api/tasks");
      state.tasks = data.tasks || [];
      if (!state.selectedTaskId && state.tasks.length) {
        state.selectedTaskId = state.tasks[0].id;
      }
      if (state.selectedTaskId && !state.tasks.some((t) => t.id === state.selectedTaskId)) {
        state.selectedTaskId = state.tasks[0] ? state.tasks[0].id : null;
      }
      renderTaskList();
    } finally {
      state.refreshingTasks = false;
    }
  }

  async function refreshDetail() {
    if (!state.selectedTaskId) {
      if (consoleMetaEl) consoleMetaEl.textContent = "选择任务后显示输出";
      return;
    }
    if (state.refreshingDetail) return;
    state.refreshingDetail = true;
    try {
      const taskData = await fetchJson(`/api/tasks/${state.selectedTaskId}`);
      renderTaskDetail(taskData.task);
      const logData = await fetchJson(`/api/tasks/${state.selectedTaskId}/logs?limit=250`);
      const lines = logData.lines || [];
      state.lastLogLines = lines;
      const nearBottom = consoleOutputEl.scrollHeight - consoleOutputEl.scrollTop - consoleOutputEl.clientHeight < 80;
      consoleOutputEl.textContent = lines.join("\n") || "暂无日志";
      if (state.autoScroll || nearBottom) {
        consoleOutputEl.scrollTop = consoleOutputEl.scrollHeight;
      }
      if (consoleMetaEl) {
        consoleMetaEl.textContent = `最近 ${lines.length} 行 · 状态 ${taskData.task.status || "-"}`;
      }
      if (downloadLogBtnEl) downloadLogBtnEl.disabled = false;
    } finally {
      state.refreshingDetail = false;
    }
  }

  async function refreshAll() {
    try {
      await refreshTasks();
      await refreshDetail();
    } catch (error) {
      formMessageEl.textContent = error.message;
      formMessageEl.className = "form-message error";
    }
  }

  async function refreshHealth() {
    if (state.refreshingHealth) return;
    state.refreshingHealth = true;
    try {
      healthMetaEl.textContent = "检测中...";
      const data = await fetchJson("/api/health");
      renderHealth(data);
    } catch (error) {
      healthMetaEl.textContent = `检测失败: ${error.message}`;
      healthGridEl.innerHTML = '<div class="empty">健康检查失败</div>';
    } finally {
      state.refreshingHealth = false;
    }
  }

  function hasActiveTasks() {
    return (state.tasks || []).some((task) => ACTIVE_STATUSES.has(String(task.status || "")));
  }

  function buildTaskPayload() {
    return {
      name: formEl.elements.name.value.trim(),
      count: Number(formEl.elements.count.value),
      proxy: formEl.elements.proxy.value.trim() || null,
      browser_proxy: formEl.elements.browser_proxy.value.trim() || null,
      temp_mail_api_base: formEl.elements.temp_mail_api_base.value.trim() || null,
      temp_mail_admin_password: formEl.elements.temp_mail_admin_password.value.trim() || null,
      temp_mail_domain: formEl.elements.temp_mail_domain.value.trim() || null,
      temp_mail_site_password: formEl.elements.temp_mail_site_password.value.trim() || null,
      api_endpoint: formEl.elements.api_endpoint.value.trim() || null,
      api_import_endpoint: (formEl.elements.api_import_endpoint?.value || "").trim() || null,
      api_admin_username: (formEl.elements.api_admin_username?.value || "").trim() || null,
      api_admin_password: (formEl.elements.api_admin_password?.value || "").trim() || null,
      api_token: formEl.elements.api_token.value.trim() || null,
      api_append: formEl.elements.api_append.checked ? true : null,
    };
  }

  function buildPreflightPayload() {
    const payload = buildTaskPayload();
    return {
      proxy: payload.proxy,
      browser_proxy: payload.browser_proxy,
      temp_mail_api_base: payload.temp_mail_api_base,
      temp_mail_admin_password: payload.temp_mail_admin_password,
      temp_mail_domain: payload.temp_mail_domain,
      temp_mail_site_password: payload.temp_mail_site_password,
      api_endpoint: payload.api_endpoint,
      api_import_endpoint: payload.api_import_endpoint,
      api_admin_username: payload.api_admin_username,
      api_admin_password: payload.api_admin_password,
    };
  }

  function renderPreflight(data) {
    if (!preflightBoxEl) return;
    const items = data.items || [];
    const ok = !!data.ok;
    preflightBoxEl.classList.remove("hidden", "ok", "bad");
    preflightBoxEl.classList.add(ok ? "ok" : "bad");
    const title = ok ? "预检通过，可以创建任务" : `预检未通过（${(data.blocking || []).length} 项异常）`;
    preflightBoxEl.innerHTML = `
      <div class="preflight-title">${escapeHtml(title)}</div>
      ${items.map((item) => `
        <div class="preflight-item">
          <strong>${escapeHtml(item.label || item.key || "-")}</strong>
          <span>${item.ok ? "正常" : "异常"} · ${escapeHtml(item.summary || "-")}</span>
        </div>
      `).join("")}
    `;
  }

  async function runPreflight(showMessage = true) {
    const payload = buildPreflightPayload();
    const data = await fetchJson("/api/preflight", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    renderPreflight(data);
    if (showMessage) {
      formMessageEl.textContent = data.ok ? "预检通过" : "预检未通过，请先检查异常项";
      formMessageEl.className = data.ok ? "form-message success" : "form-message error";
    }
    return data;
  }

  async function createTaskFromForm(force = false) {
    const payload = buildTaskPayload();
    if (!force) {
      try {
        const pre = await runPreflight(false);
        if (!pre.ok) {
          const go = window.confirm("创建前预检未通过。仍要创建任务吗？");
          if (!go) {
            formMessageEl.textContent = "已取消创建（预检未通过）";
            formMessageEl.className = "form-message error";
            return;
          }
        }
      } catch (error) {
        const go = window.confirm(`预检失败：${error.message}\n仍要创建任务吗？`);
        if (!go) {
          formMessageEl.textContent = error.message;
          formMessageEl.className = "form-message error";
          return;
        }
      }
    }
    const data = await fetchJson("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.selectedTaskId = data.task.id;
    formMessageEl.textContent = `任务 #${data.task.id} 已创建`;
    formMessageEl.className = "form-message success";
    await refreshAll();
  }

  formEl.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await createTaskFromForm(false);
    } catch (error) {
      formMessageEl.textContent = error.message;
      formMessageEl.className = "form-message error";
    }
  });

  async function cleanupTasks(statuses, label) {
    const confirmed = window.confirm(`确认清理${label}任务吗？运行中的任务不会被删除。`);
    if (!confirmed) return;
    try {
      const data = await fetchJson("/api/tasks/cleanup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ statuses }),
      });
      if (state.selectedTaskId && (data.deleted_ids || []).includes(state.selectedTaskId)) {
        state.selectedTaskId = null;
        detailTitleEl.textContent = "实时控制台";
        detailSummaryEl.innerHTML = "";
        detailMetaEl.innerHTML = "";
        detailMetaEl.classList.add("hidden");
        consoleOutputEl.textContent = "选择任务后显示输出";
        if (consoleMetaEl) consoleMetaEl.textContent = "选择任务后显示输出";
        if (downloadLogBtnEl) downloadLogBtnEl.disabled = true;
      }
      formMessageEl.textContent = `已清理 ${data.deleted_count || 0} 个任务`;
      formMessageEl.className = "form-message success";
      await refreshAll();
    } catch (error) {
      formMessageEl.textContent = error.message;
      formMessageEl.className = "form-message error";
    }
  }

  async function downloadSelectedLogs() {
    if (!state.selectedTaskId) return;
    try {
      const data = await fetchJson(`/api/tasks/${state.selectedTaskId}/logs/download?limit=5000`);
      const lines = data.lines || state.lastLogLines || [];
      const blob = new Blob([lines.join("\n") + "\n"], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = data.filename || `task-${state.selectedTaskId}.log`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (error) {
      formMessageEl.textContent = error.message;
      formMessageEl.className = "form-message error";
    }
  }

  stopBtnEl.addEventListener("click", async () => {
    if (!state.selectedTaskId) return;
    try {
      await fetchJson(`/api/tasks/${state.selectedTaskId}/stop`, { method: "POST" });
      await refreshAll();
    } catch (error) {
      formMessageEl.textContent = error.message;
      formMessageEl.className = "form-message error";
    }
  });

  refreshBtnEl.addEventListener("click", refreshAll);
  healthRefreshBtnEl.addEventListener("click", refreshHealth);

  settingsFormEl.addEventListener("submit", async (event) => {
    event.preventDefault();
    const concurrentRaw = Number(settingsFormEl.elements.max_concurrent_tasks?.value || 1);
    const adminPasswordInput = (settingsFormEl.elements.api_admin_password?.value || "").trim();
    const payload = {
      proxy: settingsFormEl.elements.proxy.value.trim(),
      browser_proxy: settingsFormEl.elements.browser_proxy.value.trim(),
      temp_mail_api_base: settingsFormEl.elements.temp_mail_api_base.value.trim(),
      temp_mail_admin_password: settingsFormEl.elements.temp_mail_admin_password.value.trim(),
      temp_mail_domain: settingsFormEl.elements.temp_mail_domain.value.trim(),
      temp_mail_site_password: settingsFormEl.elements.temp_mail_site_password.value.trim(),
      api_endpoint: settingsFormEl.elements.api_endpoint.value.trim(),
      api_import_endpoint: (settingsFormEl.elements.api_import_endpoint?.value || "").trim(),
      api_admin_username: (settingsFormEl.elements.api_admin_username?.value || "admin").trim() || "admin",
      api_admin_password: adminPasswordInput,
      api_token: settingsFormEl.elements.api_token.value.trim(),
      api_append: settingsFormEl.elements.api_append.checked,
      max_concurrent_tasks: Number.isFinite(concurrentRaw) ? concurrentRaw : 1,
    };
    try {
      const data = await fetchJson("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      window.__DEFAULTS__ = data.defaults || window.__DEFAULTS__;
      if (data.max_concurrent_tasks != null) {
        window.__DEFAULTS__.max_concurrent_tasks = data.max_concurrent_tasks;
      }
      if (data.max_concurrent_tasks_cap != null) {
        window.__DEFAULTS__.max_concurrent_tasks_cap = data.max_concurrent_tasks_cap;
      }
      settingsMessageEl.textContent = `默认配置已保存（并发上限 ${window.__DEFAULTS__.max_concurrent_tasks || payload.max_concurrent_tasks}）`;
      settingsMessageEl.className = "form-message success";
      setDefaults();
      await refreshHealth();
    } catch (error) {
      settingsMessageEl.textContent = error.message;
      settingsMessageEl.className = "form-message error";
    }
  });

  toggleAdvancedBtnEl.addEventListener("click", () => {
    advancedFieldsEl.classList.toggle("hidden");
    toggleAdvancedBtnEl.textContent = advancedFieldsEl.classList.contains("hidden") ? "高级设置" : "收起高级设置";
  });

  function toggleSettings() {
    setSettingsOpen(settingsFormEl.classList.contains("hidden"));
  }
  if (toggleSettingsBtnEl) toggleSettingsBtnEl.addEventListener("click", toggleSettings);
  if (toggleSettingsHeadBtnEl) toggleSettingsHeadBtnEl.addEventListener("click", toggleSettings);

  toggleMailBtnEl.addEventListener("click", () => {
    detailMetaEl.classList.toggle("hidden");
    toggleMailBtnEl.textContent = detailMetaEl.classList.contains("hidden") ? "任务参数" : "收起参数";
  });

  if (taskFiltersEl) {
    taskFiltersEl.addEventListener("click", (event) => {
      const btn = event.target.closest("[data-filter]");
      if (!btn) return;
      state.taskFilter = btn.dataset.filter || "all";
      taskFiltersEl.querySelectorAll(".chip").forEach((chip) => {
        chip.classList.toggle("active", chip === btn);
      });
      renderTaskList();
    });
  }

  if (taskSearchInputEl) {
    taskSearchInputEl.addEventListener("input", () => {
      state.taskSearch = taskSearchInputEl.value || "";
      renderTaskList();
    });
  }
  if (cleanupDoneBtnEl) {
    cleanupDoneBtnEl.addEventListener("click", () => cleanupTasks(["completed", "stopped"], "已完成/已停止"));
  }
  if (cleanupFailedBtnEl) {
    cleanupFailedBtnEl.addEventListener("click", () => cleanupTasks(["failed", "partial"], "失败/部分"));
  }
  if (cleanupAllTerminalBtnEl) {
    cleanupAllTerminalBtnEl.addEventListener("click", () => cleanupTasks(["completed", "stopped", "failed", "partial"], "全部终态"));
  }
  if (preflightBtnEl) {
    preflightBtnEl.addEventListener("click", async () => {
      try {
        await runPreflight(true);
      } catch (error) {
        formMessageEl.textContent = error.message;
        formMessageEl.className = "form-message error";
      }
    });
  }
  if (autoScrollToggleEl) {
    state.autoScroll = !!autoScrollToggleEl.checked;
    autoScrollToggleEl.addEventListener("change", () => {
      state.autoScroll = !!autoScrollToggleEl.checked;
    });
  }
  if (downloadLogBtnEl) {
    downloadLogBtnEl.addEventListener("click", downloadSelectedLogs);
  }
  if (applyTemplateBtnEl) {
    applyTemplateBtnEl.addEventListener("click", () => {
      const tpl = findTemplate(templateSelectEl?.value || "");
      if (!tpl) {
        formMessageEl.textContent = "请先选择模板";
        formMessageEl.className = "form-message error";
        return;
      }
      applyTemplateToForm(tpl);
      formMessageEl.textContent = `已套用模板：${tpl.name}`;
      formMessageEl.className = "form-message success";
    });
  }
  if (saveTemplateBtnEl) {
    saveTemplateBtnEl.addEventListener("click", async () => {
      try {
        const payload = buildTemplatePayloadFromForm();
        if (!payload) return;
        const data = await fetchJson("/api/templates", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        state.templates = data.templates || [];
        renderTemplates();
        if (templateSelectEl && data.template?.id) templateSelectEl.value = data.template.id;
        formMessageEl.textContent = `模板已保存：${payload.name}`;
        formMessageEl.className = "form-message success";
      } catch (error) {
        formMessageEl.textContent = error.message;
        formMessageEl.className = "form-message error";
      }
    });
  }
  if (deleteTemplateBtnEl) {
    deleteTemplateBtnEl.addEventListener("click", async () => {
      const key = templateSelectEl?.value || "";
      if (!key) {
        formMessageEl.textContent = "请先选择要删除的模板";
        formMessageEl.className = "form-message error";
        return;
      }
      const tpl = findTemplate(key);
      const label = tpl?.name || key;
      if (!window.confirm(`确认删除模板「${label}」吗？`)) return;
      try {
        const data = await fetchJson(`/api/templates/${encodeURIComponent(key)}`, { method: "DELETE" });
        state.templates = data.templates || [];
        renderTemplates();
        formMessageEl.textContent = `模板已删除：${label}`;
        formMessageEl.className = "form-message success";
      } catch (error) {
        formMessageEl.textContent = error.message;
        formMessageEl.className = "form-message error";
      }
    });
  }
  if (consoleOutputEl) {
    consoleOutputEl.addEventListener("scroll", () => {
      if (!autoScrollToggleEl) return;
      const nearBottom = consoleOutputEl.scrollHeight - consoleOutputEl.scrollTop - consoleOutputEl.clientHeight < 80;
      // If user scrolls up, keep their place unless they re-enable auto scroll.
      if (!nearBottom && state.autoScroll) {
        // soft hint only; do not force toggle off automatically
      }
    });
  }

  setDefaults();
  setSettingsOpen(false);
  refreshTemplates().catch((error) => {
    formMessageEl.textContent = error.message;
    formMessageEl.className = "form-message error";
  });
  refreshHealth();
  refreshAll();

  // Adaptive polling: task list every 3s; health 30s when active, 60s when idle.
  window.setInterval(() => {
    refreshAll();
  }, 3000);
  window.setInterval(() => {
    if (hasActiveTasks()) {
      refreshHealth();
    }
  }, 30000);
  window.setInterval(() => {
    if (!hasActiveTasks()) {
      refreshHealth();
    }
  }, 60000);
})();
