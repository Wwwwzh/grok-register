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
    lastHealth: null,
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
    return (tasks || []).filter((task) => {
      const status = String(task.status || "");
      if (state.taskFilter === "active") return ACTIVE_STATUSES.has(status);
      if (state.taskFilter === "done") return DONE_STATUSES.has(status);
      if (state.taskFilter === "failed") return FAILED_STATUSES.has(status);
      return true;
    });
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
      return `
      <div class="task-card ${selected}" data-task-id="${task.id}" role="button" tabindex="0" aria-label="查看任务 #${task.id}">
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
    const p = progressPercents(task);
    detailSummaryEl.innerHTML = [
      ["状态", task.status],
      ["目标", task.target_count],
      ["成功", task.completed_count],
      ["入池", task.pushed_count || 0],
      ["失败", task.failed_count],
      ["新增/更新", `${task.pushed_created || 0}/${task.pushed_updated || 0}`],
      ["进度", `${p.successPct}%`],
      ["入池进度", `${p.pushedPct}%`],
      ["轮次", task.current_round],
      ["阶段", task.current_phase || "-"],
    ].map(([label, value]) => `
      <div class="summary-item">
        <div class="meta-item-label">${escapeHtml(label)}</div>
        <div class="meta-item-value">${escapeHtml(value)}</div>
      </div>
    `).join("") + `
      <div class="summary-item summary-progress full">
        <div class="meta-item-label">完成进度</div>
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
      const nearBottom = consoleOutputEl.scrollHeight - consoleOutputEl.scrollTop - consoleOutputEl.clientHeight < 80;
      consoleOutputEl.textContent = lines.join("\n") || "暂无日志";
      if (nearBottom) {
        consoleOutputEl.scrollTop = consoleOutputEl.scrollHeight;
      }
      if (consoleMetaEl) {
        consoleMetaEl.textContent = `最近 ${lines.length} 行 · 状态 ${taskData.task.status || "-"}`;
      }
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

  formEl.addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {
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
    try {
      const data = await fetchJson("/api/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      state.selectedTaskId = data.task.id;
      formMessageEl.textContent = `任务 #${data.task.id} 已创建`;
      formMessageEl.className = "form-message success";
      await refreshAll();
    } catch (error) {
      formMessageEl.textContent = error.message;
      formMessageEl.className = "form-message error";
    }
  });

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

  setDefaults();
  setSettingsOpen(false);
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
