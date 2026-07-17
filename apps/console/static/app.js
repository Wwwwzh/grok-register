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
    lastPoolTrend: null,
    auditItems: [],
    auditEventNames: [],
    auditFilters: {
      level: "",
      event: "",
      task_id: "",
      q: "",
      from: "",
      to: "",
    },
    poolCompareWindow: {
      from: "",
      to: "",
      manual: false,
    },
    seenAuditIds: {},
    sseConnected: false,
    sseMode: "connecting", // live | polling | connecting | unsupported
    sseLastEventAt: null,
    sseSource: null,
    sseRetryMs: 2000,
    pollTimer: null,
    healthActiveTimer: null,
    healthIdleTimer: null,
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
  const retryPushBtnEl = document.getElementById("retryPushBtn");
  const refreshBtnEl = document.getElementById("refreshBtn");
  const healthRefreshBtnEl = document.getElementById("healthRefreshBtn");
  const toggleSettingsBtnEl = document.getElementById("toggleSettingsBtn");
  const toggleSettingsHeadBtnEl = document.getElementById("toggleSettingsHeadBtn");
  const toggleAdvancedBtnEl = document.getElementById("toggleAdvancedBtn");
  const toggleMailBtnEl = document.getElementById("toggleMailBtn");
  const advancedFieldsEl = document.getElementById("advancedFields");
  const healthGridEl = document.getElementById("healthGrid");
  const healthMetaEl = document.getElementById("healthMeta");
  const poolTrendBoxEl = document.getElementById("poolTrendBox");
  const poolTrendTitleEl = document.getElementById("poolTrendTitle");
  const poolTrendDeltaEl = document.getElementById("poolTrendDelta");
  const poolTrendChartEl = document.getElementById("poolTrendChart");
  const poolTrendRangesEl = document.getElementById("poolTrendRanges");
  const themeToggleBtnEl = document.getElementById("themeToggleBtn");
  const sseStatusEl = document.getElementById("sseStatus");
  const sseStatusTextEl = document.getElementById("sseStatusText");
  const toastHostEl = document.getElementById("toastHost");
  const auditListEl = document.getElementById("auditList");
  const auditMetaEl = document.getElementById("auditMeta");
  const refreshAuditBtnEl = document.getElementById("refreshAuditBtn");
  const exportAuditBtnEl = document.getElementById("exportAuditBtn");
  const jumpTaskAuditBtnEl = document.getElementById("jumpTaskAuditBtn");
  const taskPoolCompareBoxEl = document.getElementById("taskPoolCompareBox");
  const taskPoolCompareTitleEl = document.getElementById("taskPoolCompareTitle");
  const taskPoolCompareDeltaEl = document.getElementById("taskPoolCompareDelta");
  const taskPoolCompareMetaEl = document.getElementById("taskPoolCompareMeta");
  const taskPoolCompareChartEl = document.getElementById("taskPoolCompareChart");
  const refreshPoolCompareBtnEl = document.getElementById("refreshPoolCompareBtn");
  const auditLevelFilterEl = document.getElementById("auditLevelFilter");
  const auditEventFilterEl = document.getElementById("auditEventFilter");
  const auditTaskFilterEl = document.getElementById("auditTaskFilter");
  const auditQueryFilterEl = document.getElementById("auditQueryFilter");
  const auditFromFilterEl = document.getElementById("auditFromFilter");
  const auditToFilterEl = document.getElementById("auditToFilter");
  const auditFilterResetBtnEl = document.getElementById("auditFilterResetBtn");
  const poolCompareFromEl = document.getElementById("poolCompareFrom");
  const poolCompareToEl = document.getElementById("poolCompareTo");
  const applyPoolCompareWindowBtnEl = document.getElementById("applyPoolCompareWindowBtn");
  const resetPoolCompareWindowBtnEl = document.getElementById("resetPoolCompareWindowBtn");
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

  const THEME_KEY = "console_theme";

  function getPreferredTheme() {
    try {
      const saved = (localStorage.getItem(THEME_KEY) || "").trim();
      if (saved === "dark" || saved === "light") return saved;
    } catch (error) {}
    try {
      if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
        return "dark";
      }
    } catch (error) {}
    return "light";
  }

  function currentTheme() {
    const attr = document.documentElement.getAttribute("data-theme");
    return attr === "dark" ? "dark" : "light";
  }

  function applyTheme(theme, { persist = true } = {}) {
    const next = theme === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    if (themeToggleBtnEl) {
      themeToggleBtnEl.textContent = next === "dark" ? "亮色模式" : "暗色模式";
      themeToggleBtnEl.setAttribute("aria-pressed", next === "dark" ? "true" : "false");
      themeToggleBtnEl.title = next === "dark" ? "切换到亮色主题" : "切换到暗色主题";
    }
    if (persist) {
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch (error) {}
    }
  }

  function toggleTheme() {
    applyTheme(currentTheme() === "dark" ? "light" : "dark");
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

  function formatSigned(n) {
    const v = Number(n || 0);
    if (v > 0) return `+${v}`;
    return String(v);
  }

  function deltaClass(n) {
    const v = Number(n || 0);
    if (v > 0) return "up";
    if (v < 0) return "down";
    return "flat";
  }

  function sparklinePath(values, width, height, pad = 6) {
    const nums = values.map((v) => Number(v) || 0);
    if (!nums.length) return "";
    const min = Math.min(...nums);
    const max = Math.max(...nums);
    const span = Math.max(1, max - min);
    const n = nums.length;
    return nums.map((v, i) => {
      const x = n === 1 ? width / 2 : pad + (i * (width - pad * 2)) / (n - 1);
      const y = height - pad - ((v - min) / span) * (height - pad * 2);
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
    }).join(" ");
  }


  function normalizePoolTrendRange(range) {
    const key = String(range || "6h").toLowerCase();
    return ["1h", "6h", "24h"].includes(key) ? key : "6h";
  }

  function syncPoolTrendRangeButtons() {
    if (!poolTrendRangesEl) return;
    const active = normalizePoolTrendRange(state.poolTrendRange);
    poolTrendRangesEl.querySelectorAll("[data-range]").forEach((btn) => {
      const on = btn.getAttribute("data-range") === active;
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  async function loadPoolTrend(range, { silent = false } = {}) {
    const key = normalizePoolTrendRange(range || state.poolTrendRange || "6h");
    state.poolTrendRange = key;
    try {
      localStorage.setItem("console_pool_trend_range", key);
    } catch (error) {}
    syncPoolTrendRangeButtons();
    try {
      const trend = await fetchJson(`/api/pool/trend?range=${encodeURIComponent(key)}&limit=288`);
      renderPoolTrend(trend);
      return trend;
    } catch (error) {
      if (!silent && poolTrendTitleEl) {
        poolTrendTitleEl.textContent = `趋势加载失败: ${error.message}`;
      }
      throw error;
    }
  }

  function setPoolTrendRange(range) {
    const key = normalizePoolTrendRange(range);
    if (key === normalizePoolTrendRange(state.poolTrendRange) && state.lastPoolTrend && state.lastPoolTrend.range === key) {
      syncPoolTrendRangeButtons();
      return Promise.resolve(state.lastPoolTrend);
    }
    return loadPoolTrend(key);
  }

  function renderPoolTrend(trend) {
    if (!poolTrendChartEl) return;
    state.lastPoolTrend = trend || null;
    const points = (trend && trend.points) || [];
    const latest = (trend && trend.latest) || null;
    const delta = (trend && trend.delta) || {};
    const rangeLabel = normalizePoolTrendRange((trend && trend.range) || state.poolTrendRange || "6h");
    state.poolTrendRange = rangeLabel;
    syncPoolTrendRangeButtons();
    if (poolTrendTitleEl) {
      if (latest) {
        const count = (trend && trend.window && trend.window.count) || points.length || 0;
        poolTrendTitleEl.textContent = `${rangeLabel} · 总量 ${latest.total || 0} · web ${latest.grok_web || 0} / build ${latest.grok_build || 0} / console ${latest.grok_console || 0} · ${count}点`;
      } else {
        poolTrendTitleEl.textContent = `${rangeLabel} · 等待采样`;
      }
    }
    if (poolTrendDeltaEl) {
      if (points.length >= 2) {
        poolTrendDeltaEl.innerHTML = [
          `<span class="${deltaClass(delta.total)}">总量 ${formatSigned(delta.total)}</span>`,
          `<span class="${deltaClass(delta.grok_web)}">web ${formatSigned(delta.grok_web)}</span>`,
          `<span class="${deltaClass(delta.grok_build)}">build ${formatSigned(delta.grok_build)}</span>`,
          `<span class="${deltaClass(delta.grok_console)}">console ${formatSigned(delta.grok_console)}</span>`,
        ].join("<br>");
      } else {
        poolTrendDeltaEl.textContent = points.length ? "样本不足，继续采样中" : "尚无历史样本";
      }
    }
    if (!points.length) {
      poolTrendChartEl.innerHTML = `<div class="pool-trend-empty">健康检测后开始记录号池趋势</div>`;
      return;
    }
    const width = 320;
    const height = 120;
    const series = [
      { key: "total", color: "#b4472b", width: 2.4 },
      { key: "grok_web", color: "#2857b5", width: 1.8 },
      { key: "grok_build", color: "#2f7d48", width: 1.8 },
      { key: "grok_console", color: "#8f5b00", width: 1.8 },
    ];
    const paths = series.map((s) => {
      const d = sparklinePath(points.map((p) => p[s.key] || 0), width, height);
      return `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.width}" stroke-linecap="round" stroke-linejoin="round"></path>`;
    }).join("");
    const last = points[points.length - 1] || {};
    poolTrendChartEl.innerHTML = `
      <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="号池趋势图">
        ${paths}
        <circle cx="${width - 6}" cy="${(() => {
          const vals = points.map((p) => Number(p.total || 0));
          const min = Math.min(...vals);
          const max = Math.max(...vals);
          const span = Math.max(1, max - min);
          const v = Number(last.total || 0);
          return (height - 6 - ((v - min) / span) * (height - 12)).toFixed(1);
        })()}" r="3.2" fill="#b4472b"></circle>
      </svg>
    `;
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
    if (data.pool_trend) {
      renderPoolTrend(data.pool_trend);
    } else if (state.lastPoolTrend) {
      renderPoolTrend(state.lastPoolTrend);
    }

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
      const poolDelta = task.has_pool_delta ? Number(task.pool_delta_total || 0) : null;
      const poolDeltaClass = poolDelta == null
        ? ""
        : poolDelta > 0
          ? "up"
          : poolDelta < 0
            ? "down"
            : "flat";
      const poolDeltaLine = poolDelta == null
        ? ""
        : `<div class="task-card-pool-delta ${poolDeltaClass}" title="号池总量前后变化：${task.pool_before_total} → ${task.pool_after_total}">号池 ${poolDelta > 0 ? "+" : ""}${poolDelta}</div>`;
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
        ${gapLine}${poolDeltaLine}
        ${typeLine}
        ${errLine}
        <div class="task-actions">
          <span class="task-action-hint">点击查看日志</span>
          ${gap > 0 ? `<button class="button button-secondary button-small" type="button" data-retry-push-id="${task.id}">重试入池</button>` : ""}
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
        if (event.target.closest("[data-delete-task-id],[data-retry-push-id]")) return;
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
            if (jumpTaskAuditBtnEl) jumpTaskAuditBtnEl.disabled = true;
            clearTaskPoolCompare();
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

    taskListEl.querySelectorAll("[data-retry-push-id]").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.stopPropagation();
        const taskId = Number(button.dataset.retryPushId);
        button.disabled = true;
        try {
          await retryTaskPush(taskId, { source: "list" });
        } catch (error) {
          // toast already shown
        } finally {
          button.disabled = false;
        }
      });
    });

    renderOverview();
  }


  async function retryTaskPush(taskId, { source = "detail" } = {}) {
    const id = Number(taskId);
    if (!id) return;
    const confirmed = window.confirm(`确认重试任务 #${id} 的入池推送吗？\n将读取该任务 sso 文件并重新导入 grok2api。`);
    if (!confirmed) return;
    try {
      if (source === "detail" && detailTitleEl) {
        // soft status in console meta if available
      }
      const data = await fetchJson(`/api/tasks/${id}/push-retry`, { method: "POST" });
      const result = data.result || {};
      const ok = Boolean(data.ok || result.ok);
      const summary = result.summary || (ok ? "入池重试完成" : "入池重试失败");
      showToast(ok ? "入池重试成功" : "入池重试失败", summary, ok ? "success" : "error");
      if (formMessageEl && source === "list") {
        formMessageEl.textContent = summary;
        formMessageEl.className = ok ? "form-message" : "form-message error";
      }
      await refreshTasks();
      if (state.selectedTaskId === id) {
        await refreshDetail();
      }
      refreshAudit({ silent: true }).catch(() => {});
      return data;
    } catch (error) {
      showToast("入池重试失败", error.message || String(error), "error");
      if (formMessageEl) {
        formMessageEl.textContent = error.message || String(error);
        formMessageEl.className = "form-message error";
      }
      throw error;
    }
  }

  function renderTaskDetail(task) {
    detailTitleEl.textContent = `任务 #${task.id} · ${task.name}`;
    stopBtnEl.disabled = !ACTIVE_STATUSES.has(task.status);
    if (downloadLogBtnEl) downloadLogBtnEl.disabled = false;
    if (jumpTaskAuditBtnEl) {
      jumpTaskAuditBtnEl.disabled = false;
      jumpTaskAuditBtnEl.title = `筛选任务 #${task.id} 的审计记录`;
    }
    if (refreshPoolCompareBtnEl) refreshPoolCompareBtnEl.disabled = false;
    // load pool compare for this task (non-blocking)
    loadTaskPoolCompare(task.id, { silent: true }).catch(() => {});
    if (retryPushBtnEl) {
      const gap = pushGapOf(task);
      const canRetry = !ACTIVE_STATUSES.has(task.status) && (gap > 0 || Number(task.completed_count || 0) > 0);
      retryPushBtnEl.disabled = !canRetry;
      retryPushBtnEl.title = gap > 0
        ? `有 ${gap} 个成功未入池，可重试推送`
        : (canRetry ? "用任务 SSO 文件重新入池" : "无可重试入池的任务");
      retryPushBtnEl.textContent = gap > 0 ? `重试入池 (${gap})` : "重试入池";
    }
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


  function authTokenQuery() {
    const token = getAuthToken();
    return token ? `token=${encodeURIComponent(token)}` : "";
  }

  function scheduleTaskRefresh(reason) {
    if (state._taskRefreshTimer) return;
    state._taskRefreshTimer = window.setTimeout(async () => {
      state._taskRefreshTimer = null;
      try {
        await refreshTasks();
        await refreshDetail();
      } catch (error) {
        // Keep quiet on background SSE-driven refresh failures.
      }
    }, reason === "progress" ? 250 : 80);
  }

  function handleSsePayload(payload) {
    if (!payload || typeof payload !== "object") return;
    const eventName = payload.event || "message";
    state.sseLastEventAt = payload.ts || new Date().toLocaleTimeString();
    if (state.sseConnected) {
      updateSseStatus("live");
    }
    if (eventName === "ping" || eventName === "hello") return;
    if (eventName === "audit") {
      const item = payload.data || payload;
      if (item && item.id != null) {
        const id = String(item.id);
        if (!state.seenAuditIds[id]) {
          state.seenAuditIds[id] = true;
          const title = item.event === "task_finished"
            ? (item.level === "success" ? "任务完成" : item.level === "error" ? "任务失败" : "任务结束")
            : item.event === "task_created"
              ? "任务已创建"
              : item.event === "tasks_cleanup"
                ? "已清理任务"
                : item.event === "push_retry_ok"
                  ? "入池重试成功"
                  : item.event === "push_retry_failed"
                    ? "入池重试失败"
                    : "任务通知";
          if (["task_finished", "task_created", "task_deleted", "tasks_cleanup", "task_stopping", "task_stopped", "push_retry_ok", "push_retry_failed"].includes(item.event)) {
            showToast(title, item.message || "", item.level || "info");
          }
          if (item.event) {
            syncAuditEventOptions([item.event]);
          }
          // Only prepend into the visible list when it matches current filters.
          if (matchesAuditFilters(item)) {
            const next = [item, ...(state.auditItems || [])].slice(0, 40);
            const f = state.auditFilters || {};
            const parts = [];
            if (f.level) parts.push(`级别 ${f.level}`);
            if (f.event) parts.push(`事件 ${f.event}`);
            if (f.task_id) parts.push(`任务 #${f.task_id}`);
            if (f.q) parts.push(`关键词 “${f.q}”`);
            renderAuditList(next, { metaSuffix: parts.join(" · ") });
          }
        }
      } else {
        refreshAudit({ silent: true });
      }
      return;
    }
    if (eventName === "tasks_changed" || eventName === "task_created" || eventName === "task_deleted" || eventName === "task_started" || eventName === "task_finished" || eventName === "task_stopping") {
      scheduleTaskRefresh(eventName);
      return;
    }
    if (eventName === "task_progress") {
      const data = payload.data || {};
      const taskId = Number(data.task_id || 0);
      // Soft-update selected task progress without waiting for full poll.
      if (taskId && state.selectedTaskId === taskId) {
        scheduleTaskRefresh("progress");
      } else {
        scheduleTaskRefresh("progress");
      }
    }
  }


  function updateSseStatus(mode, detail = "") {
    const next = ["live", "polling", "connecting", "unsupported"].includes(mode)
      ? mode
      : (state.sseConnected ? "live" : "polling");
    state.sseMode = next;
    if (!sseStatusEl || !sseStatusTextEl) return;
    sseStatusEl.classList.remove("is-live", "is-polling", "is-connecting", "is-unsupported");
    sseStatusEl.classList.add(`is-${next}`);
    const labels = {
      live: "实时连接中",
      polling: "已降级轮询",
      connecting: "连接中...",
      unsupported: "仅轮询模式",
    };
    const pollHint = next === "live" ? "任务 15s / 健康 60-120s" : "任务 3s / 健康 30-60s";
    const text = labels[next] || next;
    sseStatusTextEl.textContent = detail ? `${text} · ${detail}` : text;
    const titleBits = [
      text,
      next === "live" ? "SSE 正常，轮询已降频" : next === "polling" ? "SSE 断开，回退高频轮询" : "",
      pollHint,
      state.sseLastEventAt ? `最近事件 ${state.sseLastEventAt}` : "",
    ].filter(Boolean);
    sseStatusEl.title = titleBits.join(" · ");
    sseStatusEl.setAttribute("data-mode", next);
    sseStatusEl.setAttribute("aria-label", titleBits.join("，"));
  }

  function stopSse() {
    if (state.sseSource) {
      try { state.sseSource.close(); } catch (error) {}
      state.sseSource = null;
    }
    state.sseConnected = false;
  }


  function showToast(title, body, level = "info", ttlMs = 4500) {
    if (!toastHostEl) return;
    const el = document.createElement("div");
    el.className = `toast ${level || "info"}`;
    el.innerHTML = `
      <div class="toast-title">${escapeHtml(title || "通知")}</div>
      <div class="toast-body">${escapeHtml(body || "")}</div>
    `;
    toastHostEl.prepend(el);
    window.setTimeout(() => {
      el.style.opacity = "0";
      el.style.transition = "opacity 200ms ease";
      window.setTimeout(() => el.remove(), 220);
    }, Math.max(2000, ttlMs));
  }

  function matchesAuditFilters(item, filters = state.auditFilters) {
    const f = filters || {};
    if (!item || typeof item !== "object") return false;
    const level = String(f.level || "").trim().toLowerCase();
    if (level && String(item.level || "").toLowerCase() !== level) return false;
    const event = String(f.event || "").trim();
    if (event && String(item.event || "") !== event) return false;
    const taskId = String(f.task_id || "").trim();
    if (taskId) {
      if (item.task_id == null || String(item.task_id) !== taskId) return false;
    }
    const q = String(f.q || "").trim().toLowerCase();
    if (q) {
      const hay = [
        item.message || "",
        item.event || "",
        item.task_id != null ? String(item.task_id) : "",
        item.level || "",
      ].join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    // Date range: compare YYYY-MM-DD (or full datetime) lexicographically against created_at
    const created = String(item.created_at || "").replace("T", " ").trim();
    const from = String(f.from || "").trim();
    const to = String(f.to || "").trim();
    if (from) {
      const fromBound = from.length === 10 ? `${from} 00:00:00` : from.replace("T", " ");
      if (created && created < fromBound) return false;
    }
    if (to) {
      const toBound = to.length === 10 ? `${to} 23:59:59` : to.replace("T", " ");
      if (created && created > toBound) return false;
    }
    return true;
  }

  function readAuditFiltersFromUi() {
    const filters = state.auditFilters || { level: "", event: "", task_id: "", q: "", from: "", to: "" };
    filters.level = auditLevelFilterEl ? String(auditLevelFilterEl.value || "").trim().toLowerCase() : (filters.level || "");
    filters.event = auditEventFilterEl ? String(auditEventFilterEl.value || "").trim() : (filters.event || "");
    filters.task_id = auditTaskFilterEl ? String(auditTaskFilterEl.value || "").trim() : (filters.task_id || "");
    filters.q = auditQueryFilterEl ? String(auditQueryFilterEl.value || "").trim() : (filters.q || "");
    filters.from = auditFromFilterEl ? String(auditFromFilterEl.value || "").trim() : (filters.from || "");
    filters.to = auditToFilterEl ? String(auditToFilterEl.value || "").trim() : (filters.to || "");
    state.auditFilters = filters;
    return filters;
  }

  function auditFiltersActive(filters = state.auditFilters) {
    const f = filters || {};
    return Boolean(
      (f.level || "").trim()
      || (f.event || "").trim()
      || (f.task_id || "").trim()
      || (f.q || "").trim()
      || (f.from || "").trim()
      || (f.to || "").trim()
    );
  }



  function toDatetimeLocalValue(value) {
    const raw = String(value || "").trim();
    if (!raw) return "";
    // Accept "YYYY-MM-DD HH:MM:SS" / ISO / date only
    const cleaned = raw.replace("T", " ").replace("Z", "").split(".")[0];
    const m = cleaned.match(/^(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}):(\d{2})(?::\d{2})?)?/);
    if (!m) return "";
    const date = m[1];
    const hh = m[2] || "00";
    const mm = m[3] || "00";
    return `${date}T${hh}:${mm}`;
  }

  function readPoolCompareWindowFromUi() {
    const from = poolCompareFromEl ? String(poolCompareFromEl.value || "").trim() : "";
    const to = poolCompareToEl ? String(poolCompareToEl.value || "").trim() : "";
    return { from, to, manual: Boolean(from || to) };
  }

  function setPoolCompareWindowInputs(fromValue, toValue, { manual = false } = {}) {
    const fromLocal = toDatetimeLocalValue(fromValue);
    const toLocal = toDatetimeLocalValue(toValue);
    if (poolCompareFromEl) poolCompareFromEl.value = fromLocal;
    if (poolCompareToEl) poolCompareToEl.value = toLocal;
    state.poolCompareWindow = {
      from: fromLocal,
      to: toLocal,
      manual: Boolean(manual && (fromLocal || toLocal)),
    };
  }

  function buildAuditExportUrl(extra = {}) {
    const filters = { ...readAuditFiltersFromUi(), ...extra };
    const params = new URLSearchParams();
    params.set("limit", "2000");
    if (filters.level) params.set("level", filters.level);
    if (filters.event) params.set("event", filters.event);
    if (filters.task_id) params.set("task_id", filters.task_id);
    if (filters.q) params.set("q", filters.q);
    if (filters.from) params.set("from", filters.from);
    if (filters.to) params.set("to", filters.to);
    // keep auth token in query if present (same as other downloads)
    const authQ = authTokenQuery();
    if (authQ) {
      const token = new URLSearchParams(authQ).get("token");
      if (token) params.set("token", token);
    }
    return apiUrl(`/api/audit/export?${params.toString()}`);
  }

  function exportAuditCsv() {
    const url = buildAuditExportUrl();
    // open download in same tab context
    const a = document.createElement("a");
    a.href = url;
    a.download = "";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
    showToast("开始导出", "正在下载当前筛选的审计 CSV", "info", 2200);
  }

  function jumpToTaskAudit(taskId) {
    const id = Number(taskId);
    if (!id) return;
    // set filter UI
    state.auditFilters = {
      ...(state.auditFilters || {}),
      level: "",
      event: "",
      task_id: String(id),
      q: "",
      from: "",
      to: "",
    };
    if (auditLevelFilterEl) auditLevelFilterEl.value = "";
    if (auditEventFilterEl) auditEventFilterEl.value = "";
    if (auditTaskFilterEl) auditTaskFilterEl.value = String(id);
    if (auditQueryFilterEl) auditQueryFilterEl.value = "";
    if (auditFromFilterEl) auditFromFilterEl.value = "";
    if (auditToFilterEl) auditToFilterEl.value = "";
    // scroll audit panel into view
    const panel = document.querySelector(".panel-audit");
    if (panel && typeof panel.scrollIntoView === "function") {
      panel.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    refreshAudit({ silent: true }).then(() => {
      showToast("已定位审计", `已筛选任务 #${id} 的审计记录`, "info", 2200);
    }).catch(() => {});
  }

  function renderTaskPoolCompare(data) {
    if (!taskPoolCompareBoxEl) return;
    taskPoolCompareBoxEl.classList.remove("hidden");
    const has = Boolean(data && data.has_compare);
    const delta = (data && data.delta) || {};
    const before = (data && data.before) || null;
    const after = (data && data.after) || null;
    const windowInfo = (data && data.window) || {};
    if (taskPoolCompareTitleEl) {
      if (!data) {
        taskPoolCompareTitleEl.textContent = "加载中...";
      } else if (!has) {
        taskPoolCompareTitleEl.textContent = "样本不足，暂无法对比（需任务时段附近有号池采样）";
      } else {
        taskPoolCompareTitleEl.textContent = `总量 ${before.total || 0} → ${after.total || 0} · 变化 ${formatSigned(delta.total)}`;
      }
    }
    if (taskPoolCompareDeltaEl) {
      if (has) {
        taskPoolCompareDeltaEl.innerHTML = [
          `<span class="${deltaClass(delta.total)}">总量 ${formatSigned(delta.total)}</span>`,
          `<span class="${deltaClass(delta.grok_web)}">web ${formatSigned(delta.grok_web)}</span>`,
          `<span class="${deltaClass(delta.grok_build)}">build ${formatSigned(delta.grok_build)}</span>`,
          `<span class="${deltaClass(delta.grok_console)}">console ${formatSigned(delta.grok_console)}</span>`,
        ].join("");
      } else {
        taskPoolCompareDeltaEl.textContent = "—";
      }
    }
    if (taskPoolCompareMetaEl) {
      const bits = [];
      if (windowInfo.manual) bits.push("手动时间窗");
      if (windowInfo.anchor_start) bits.push(`窗口开始 ${windowInfo.anchor_start}`);
      if (windowInfo.finished_at && !windowInfo.manual) bits.push(`任务结束 ${windowInfo.finished_at}`);
      else if (windowInfo.anchor_end) bits.push(`窗口结束 ${windowInfo.anchor_end}`);
      if (before && before.ts) bits.push(`前采样 ${before.ts}`);
      if (after && after.ts) bits.push(`后采样 ${after.ts}`);
      taskPoolCompareMetaEl.textContent = bits.join(" · ") || "等待对比数据";
    }
    if (taskPoolCompareChartEl) {
      const points = (data && data.points) || [];
      if (points.length < 2) {
        taskPoolCompareChartEl.innerHTML = `<div class="task-pool-compare-empty">${has ? "区间样本较少" : "健康检测采样后可对比任务前后号池"}</div>`;
      } else {
        const width = 360;
        const height = 84;
        const series = [
          { key: "total", color: "#b4472b", width: 2.2 },
          { key: "grok_web", color: "#2857b5", width: 1.5 },
          { key: "grok_build", color: "#2f7d48", width: 1.5 },
          { key: "grok_console", color: "#8f5b00", width: 1.5 },
        ];
        const paths = series.map((s) => {
          const d = sparklinePath(points.map((p) => p[s.key] || 0), width, height);
          return `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.width}" stroke-linecap="round" stroke-linejoin="round"></path>`;
        }).join("");
        taskPoolCompareChartEl.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="任务号池对比趋势">${paths}</svg>`;
      }
    }
  }

  async function loadTaskPoolCompare(taskId, { silent = false, useUiWindow = true, resetWindow = false } = {}) {
    const id = Number(taskId);
    if (!id) return null;
    if (refreshPoolCompareBtnEl) refreshPoolCompareBtnEl.disabled = false;
    if (applyPoolCompareWindowBtnEl) applyPoolCompareWindowBtnEl.disabled = false;
    if (resetPoolCompareWindowBtnEl) resetPoolCompareWindowBtnEl.disabled = false;
    if (taskPoolCompareBoxEl) taskPoolCompareBoxEl.classList.remove("hidden");
    if (taskPoolCompareTitleEl && !silent) taskPoolCompareTitleEl.textContent = "加载对比中...";
    try {
      const params = new URLSearchParams();
      if (resetWindow) {
        state.poolCompareWindow = { from: "", to: "", manual: false };
      }
      const win = useUiWindow ? readPoolCompareWindowFromUi() : (state.poolCompareWindow || {});
      if (!resetWindow && win && win.manual) {
        if (win.from) params.set("from", win.from);
        if (win.to) params.set("to", win.to);
        state.poolCompareWindow = { from: win.from || "", to: win.to || "", manual: true };
      }
      const qs = params.toString();
      const data = await fetchJson(`/api/tasks/${id}/pool-compare${qs ? `?${qs}` : ""}`);
      // If using default task window, seed datetime inputs from response anchors
      if (resetWindow || !(state.poolCompareWindow && state.poolCompareWindow.manual)) {
        const windowInfo = (data && data.window) || {};
        setPoolCompareWindowInputs(windowInfo.anchor_start, windowInfo.anchor_end, { manual: false });
      }
      renderTaskPoolCompare(data);
      return data;
    } catch (error) {
      if (taskPoolCompareTitleEl) {
        taskPoolCompareTitleEl.textContent = silent ? "对比暂不可用" : `对比加载失败: ${error.message}`;
      }
      if (taskPoolCompareDeltaEl) taskPoolCompareDeltaEl.textContent = "—";
      if (taskPoolCompareMetaEl) taskPoolCompareMetaEl.textContent = error.message || String(error);
      if (taskPoolCompareChartEl) {
        taskPoolCompareChartEl.innerHTML = `<div class="task-pool-compare-empty">加载失败</div>`;
      }
      return null;
    }
  }

  function clearTaskPoolCompare() {
    if (taskPoolCompareBoxEl) taskPoolCompareBoxEl.classList.add("hidden");
    if (refreshPoolCompareBtnEl) refreshPoolCompareBtnEl.disabled = true;
    if (applyPoolCompareWindowBtnEl) applyPoolCompareWindowBtnEl.disabled = true;
    if (resetPoolCompareWindowBtnEl) resetPoolCompareWindowBtnEl.disabled = true;
    if (poolCompareFromEl) poolCompareFromEl.value = "";
    if (poolCompareToEl) poolCompareToEl.value = "";
    state.poolCompareWindow = { from: "", to: "", manual: false };
    if (taskPoolCompareTitleEl) taskPoolCompareTitleEl.textContent = "选择任务后显示";
    if (taskPoolCompareDeltaEl) taskPoolCompareDeltaEl.textContent = "-";
    if (taskPoolCompareMetaEl) taskPoolCompareMetaEl.textContent = "";
    if (taskPoolCompareChartEl) taskPoolCompareChartEl.innerHTML = "";
  }

  function buildAuditQuery(extra = {}) {
    const filters = { ...readAuditFiltersFromUi(), ...extra };
    const params = new URLSearchParams();
    params.set("limit", "40");
    if (filters.level) params.set("level", filters.level);
    if (filters.event) params.set("event", filters.event);
    if (filters.task_id) params.set("task_id", filters.task_id);
    if (filters.q) params.set("q", filters.q);
    if (filters.from) params.set("from", filters.from);
    if (filters.to) params.set("to", filters.to);
    return `/api/audit?${params.toString()}`;
  }

  function syncAuditEventOptions(eventNames = []) {
    if (!auditEventFilterEl) return;
    const names = Array.from(new Set([...(eventNames || []), ...(state.auditEventNames || [])].filter(Boolean)));
    names.sort((a, b) => String(a).localeCompare(String(b)));
    state.auditEventNames = names;
    const current = String(state.auditFilters?.event || auditEventFilterEl.value || "");
    const options = ['<option value="">全部事件</option>']
      .concat(names.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`));
    auditEventFilterEl.innerHTML = options.join("");
    if (current && names.includes(current)) {
      auditEventFilterEl.value = current;
    } else if (current) {
      // keep a temporary option for active filter even if not in recent set
      const opt = document.createElement("option");
      opt.value = current;
      opt.textContent = current;
      auditEventFilterEl.appendChild(opt);
      auditEventFilterEl.value = current;
    } else {
      auditEventFilterEl.value = "";
    }
  }

  function renderAuditList(items, { metaSuffix = "" } = {}) {
    if (!auditListEl) return;
    const list = items || [];
    state.auditItems = list;
    if (auditMetaEl) {
      const active = auditFiltersActive();
      const base = list.length
        ? (active ? `筛选到 ${list.length} 条` : `最近 ${list.length} 条审计记录`)
        : (active ? "当前筛选无结果" : "暂无审计记录");
      auditMetaEl.textContent = metaSuffix ? `${base} · ${metaSuffix}` : base;
    }
    if (!list.length) {
      auditListEl.innerHTML = auditFiltersActive()
        ? '<div class="empty">没有匹配当前筛选的审计记录</div>'
        : '<div class="empty">创建/启停/结束/清理任务后会出现在这里</div>';
      return;
    }
    auditListEl.innerHTML = list.map((item) => {
      const level = item.level || "info";
      const taskTag = item.task_id != null ? `<span class="audit-tag">#${escapeHtml(item.task_id)}</span>` : "";
      const eventTag = `<span class="audit-tag">${escapeHtml(item.event || "-")}</span>`;
      const levelTag = `<span class="audit-tag level-${escapeHtml(level)}">${escapeHtml(level)}</span>`;
      return `
        <div class="audit-item">
          <div class="audit-time">${escapeHtml(item.created_at || "-")}</div>
          <div class="audit-main">
            <div class="audit-msg">${escapeHtml(item.message || "-")}</div>
            <div class="audit-tags">${levelTag}${eventTag}${taskTag}</div>
          </div>
        </div>
      `;
    }).join("");
  }

  async function refreshAudit({ silent = false } = {}) {
    try {
      const data = await fetchJson(buildAuditQuery());
      const items = data.items || [];
      if (Array.isArray(data.event_names)) {
        syncAuditEventOptions(data.event_names);
      }
      // toast only for newly seen high-signal events (unfiltered feed still useful when filters empty)
      const important = new Set([
        "task_finished", "task_created", "task_deleted", "tasks_cleanup",
        "task_stopping", "task_stopped", "push_retry_ok", "push_retry_failed",
      ]);
      for (const item of items.slice().reverse()) {
        const id = String(item.id || "");
        if (!id || state.seenAuditIds[id]) continue;
        state.seenAuditIds[id] = true;
        if (!silent && important.has(item.event)) {
          const title = item.event === "task_finished"
            ? (item.level === "success" ? "任务完成" : item.level === "error" ? "任务失败" : "任务结束")
            : item.event === "task_created"
              ? "任务已创建"
              : item.event === "tasks_cleanup"
                ? "已清理任务"
                : item.event === "push_retry_ok"
                  ? "入池重试成功"
                  : item.event === "push_retry_failed"
                    ? "入池重试失败"
                    : "任务通知";
          showToast(title, item.message || "", item.level || "info");
        }
      }
      // seed seen set fully
      items.forEach((item) => {
        if (item.id != null) state.seenAuditIds[String(item.id)] = true;
      });
      const f = state.auditFilters || {};
      const parts = [];
      if (f.level) parts.push(`级别 ${f.level}`);
      if (f.event) parts.push(`事件 ${f.event}`);
      if (f.task_id) parts.push(`任务 #${f.task_id}`);
      if (f.q) parts.push(`关键词 “${f.q}”`);
      renderAuditList(items, { metaSuffix: parts.join(" · ") });
    } catch (error) {
      if (auditMetaEl) auditMetaEl.textContent = `审计加载失败: ${error.message}`;
      if (!silent) {
        // keep quiet on background failures
      }
    }
  }

  function scheduleAuditRefresh() {
    if (state.auditFilterTimer) {
      window.clearTimeout(state.auditFilterTimer);
    }
    state.auditFilterTimer = window.setTimeout(() => {
      refreshAudit({ silent: true });
    }, 250);
  }

  function resetAuditFilters() {
    state.auditFilters = { level: "", event: "", task_id: "", q: "", from: "", to: "" };
    if (auditLevelFilterEl) auditLevelFilterEl.value = "";
    if (auditEventFilterEl) auditEventFilterEl.value = "";
    if (auditTaskFilterEl) auditTaskFilterEl.value = "";
    if (auditQueryFilterEl) auditQueryFilterEl.value = "";
    if (auditFromFilterEl) auditFromFilterEl.value = "";
    if (auditToFilterEl) auditToFilterEl.value = "";
    refreshAudit({ silent: true });
  }

  function connectSse() {
    if (!window.EventSource) {
      state.sseConnected = false;
      updateSseStatus("unsupported");
      restartPolling();
      return false;
    }
    updateSseStatus("connecting");
    stopSse();
    const qs = authTokenQuery();
    const taskQ = state.selectedTaskId ? `task_id=${encodeURIComponent(state.selectedTaskId)}` : "";
    const parts = [qs, taskQ].filter(Boolean).join("&");
    const url = apiUrl(`/api/events${parts ? `?${parts}` : ""}`);
    let es;
    try {
      es = new EventSource(url);
    } catch (error) {
      state.sseConnected = false;
      updateSseStatus("polling", "建立失败");
      restartPolling();
      return false;
    }
    state.sseSource = es;

    const onAny = (event) => {
      try {
        const payload = JSON.parse(event.data || "{}");
        // Some events wrap {event,data}, hello/ping do too.
        handleSsePayload(payload.event ? payload : { event: event.type || "message", data: payload });
      } catch (error) {}
    };

    ["hello", "ping", "audit", "tasks_changed", "task_created", "task_deleted", "task_started", "task_finished", "task_stopping", "task_progress", "message"].forEach((name) => {
      es.addEventListener(name, onAny);
    });

    es.onopen = () => {
      state.sseConnected = true;
      state.sseRetryMs = 2000;
      updateSseStatus("live");
      // When live stream is healthy, slow polling down.
      restartPolling();
    };
    es.onerror = () => {
      state.sseConnected = false;
      try { es.close(); } catch (error) {}
      state.sseSource = null;
      updateSseStatus("polling", "重连中");
      restartPolling();
      window.setTimeout(() => {
        updateSseStatus("connecting");
        connectSse();
      }, state.sseRetryMs);
      state.sseRetryMs = Math.min(15000, Math.floor(state.sseRetryMs * 1.5));
    };
    return true;
  }

  function clearPolling() {
    if (state.pollTimer) {
      window.clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    if (state.healthActiveTimer) {
      window.clearInterval(state.healthActiveTimer);
      state.healthActiveTimer = null;
    }
    if (state.healthIdleTimer) {
      window.clearInterval(state.healthIdleTimer);
      state.healthIdleTimer = null;
    }
  }

  function restartPolling() {
    clearPolling();
    // SSE healthy: light fallback poll. SSE down: original cadence.
    const taskMs = state.sseConnected ? 15000 : 3000;
    const healthActiveMs = state.sseConnected ? 60000 : 30000;
    const healthIdleMs = state.sseConnected ? 120000 : 60000;
    state.pollTimer = window.setInterval(() => {
      refreshAll();
    }, taskMs);
    state.healthActiveTimer = window.setInterval(() => {
      if (hasActiveTasks()) refreshHealth();
    }, healthActiveMs);
    state.healthIdleTimer = window.setInterval(() => {
      if (!hasActiveTasks()) refreshHealth();
    }, healthIdleMs);
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
      // Prefer longer dedicated trend history when available.
      try {
        const range = normalizePoolTrendRange(state.poolTrendRange || "6h");
        const trend = await fetchJson(`/api/pool/trend?range=${encodeURIComponent(range)}&limit=288`);
        if (trend && (trend.points || trend.latest)) {
          data.pool_trend = trend;
        }
      } catch (error) {
        // Keep embedded health trend if dedicated endpoint fails.
      }
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
        if (jumpTaskAuditBtnEl) jumpTaskAuditBtnEl.disabled = true;
        clearTaskPoolCompare();
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

    if (retryPushBtnEl) {
    retryPushBtnEl.addEventListener("click", async () => {
      if (!state.selectedTaskId) return;
      retryPushBtnEl.disabled = true;
      try {
        await retryTaskPush(state.selectedTaskId, { source: "detail" });
      } catch (error) {
        // toast already shown
      } finally {
        // re-enable based on latest task state after refresh
        const task = (state.tasks || []).find((t) => Number(t.id) === Number(state.selectedTaskId));
        if (task) {
          const gap = pushGapOf(task);
          const canRetry = !ACTIVE_STATUSES.has(task.status) && (gap > 0 || Number(task.completed_count || 0) > 0);
          retryPushBtnEl.disabled = !canRetry;
          retryPushBtnEl.textContent = gap > 0 ? `重试入池 (${gap})` : "重试入池";
        } else {
          retryPushBtnEl.disabled = false;
        }
      }
    });
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

  if (themeToggleBtnEl) {
    themeToggleBtnEl.addEventListener("click", toggleTheme);
  }
  if (refreshAuditBtnEl) {
    refreshAuditBtnEl.addEventListener("click", () => refreshAudit({ silent: false }));
  }
  if (exportAuditBtnEl) {
    exportAuditBtnEl.addEventListener("click", () => {
      try {
        exportAuditCsv();
      } catch (error) {
        showToast("导出失败", error.message || String(error), "error");
      }
    });
  }
  if (jumpTaskAuditBtnEl) {
    jumpTaskAuditBtnEl.addEventListener("click", () => {
      if (!state.selectedTaskId) return;
      jumpToTaskAudit(state.selectedTaskId);
    });
  }
  if (refreshPoolCompareBtnEl) {
    refreshPoolCompareBtnEl.addEventListener("click", () => {
      if (!state.selectedTaskId) return;
      loadTaskPoolCompare(state.selectedTaskId, { silent: false, useUiWindow: true });
    });
  }
  if (applyPoolCompareWindowBtnEl) {
    applyPoolCompareWindowBtnEl.addEventListener("click", () => {
      if (!state.selectedTaskId) return;
      const win = readPoolCompareWindowFromUi();
      if (!win.from && !win.to) {
        showToast("请选择时间窗", "请至少设置开始或结束时间", "warn");
        return;
      }
      state.poolCompareWindow = { from: win.from, to: win.to, manual: true };
      loadTaskPoolCompare(state.selectedTaskId, { silent: false, useUiWindow: true });
    });
  }
  if (resetPoolCompareWindowBtnEl) {
    resetPoolCompareWindowBtnEl.addEventListener("click", () => {
      if (!state.selectedTaskId) return;
      loadTaskPoolCompare(state.selectedTaskId, { silent: false, resetWindow: true, useUiWindow: false });
    });
  }
  if (auditFromFilterEl) {
    auditFromFilterEl.addEventListener("change", () => scheduleAuditRefresh());
  }
  if (auditToFilterEl) {
    auditToFilterEl.addEventListener("change", () => scheduleAuditRefresh());
  }
  if (auditLevelFilterEl) {
    auditLevelFilterEl.addEventListener("change", () => {
      readAuditFiltersFromUi();
      refreshAudit({ silent: true });
    });
  }
  if (auditEventFilterEl) {
    auditEventFilterEl.addEventListener("change", () => {
      readAuditFiltersFromUi();
      refreshAudit({ silent: true });
    });
  }
  if (auditTaskFilterEl) {
    auditTaskFilterEl.addEventListener("input", () => {
      readAuditFiltersFromUi();
      scheduleAuditRefresh();
    });
    auditTaskFilterEl.addEventListener("change", () => {
      readAuditFiltersFromUi();
      refreshAudit({ silent: true });
    });
  }
  if (auditQueryFilterEl) {
    auditQueryFilterEl.addEventListener("input", () => {
      readAuditFiltersFromUi();
      scheduleAuditRefresh();
    });
    auditQueryFilterEl.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        readAuditFiltersFromUi();
        refreshAudit({ silent: true });
      }
    });
  }
  if (auditFilterResetBtnEl) {
    auditFilterResetBtnEl.addEventListener("click", resetAuditFilters);
  }
  if (poolTrendRangesEl) {
    poolTrendRangesEl.addEventListener("click", (event) => {
      const btn = event.target.closest("[data-range]");
      if (!btn) return;
      setPoolTrendRange(btn.getAttribute("data-range"));
    });
  }
  syncPoolTrendRangeButtons();
  applyTheme(getPreferredTheme(), { persist: false });

  setDefaults();
  setSettingsOpen(false);
  refreshTemplates().catch((error) => {
    formMessageEl.textContent = error.message;
    formMessageEl.className = "form-message error";
  });
  refreshHealth();
  refreshAll();
  refreshAudit({ silent: true });
  updateSseStatus(window.EventSource ? "connecting" : "unsupported");
  connectSse();
  restartPolling();
})();
