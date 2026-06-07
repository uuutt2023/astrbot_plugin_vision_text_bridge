/* Vision Text Bridge · Webui Logic
 *
 * v0.8.6 重写：glassmorphism 风格 + 缩略图 + 模态详情
 * v0.8.7.2: 全面接入 logger.js（控制台双输出 + on-screen panel）
 * v0.8.14: 防御性等 window.AstrBotPluginPage 出现
 * v0.8.18: 彻底放弃 bridge SDK——AstrBot 服务端 CORS wildcard + origin=null +
 *          credentials mode=include 三者撞，bridge-sdk.js fetch 永远会拒。
 *          改用 fetch 直打 backend (fallbackFetch)——跟其他 AstrBot 插件 webui 一致。
 * v0.8.20: 去掉 ESM import——logger 改 window.webuiLogger，app.js 改成普通 <script>。
 *          消除 module system 依赖（部分 AstrBot 版本对 type=module 处理不一致）。
 *          整个 webui 零外部依赖。
 *
 * 包裹为 async IIFE——解决 top-level await 在普通 <script> 不被支持的问题。
 */

(async function main() {
  // v0.8.21: 双保险——在 <body> 末尾加载了 app.js，但不同浏览器/插件的 HTML
  // parse 速度不一致。显式等 DOMContentLoaded 确保 body 全部 parse 完才动手。
  if (document.readyState === "loading") {
    try {
      await new Promise((resolve) => {
        document.addEventListener("DOMContentLoaded", resolve, { once: true });
      });
    } catch (e) { /* ignore */ }
  }

  // v0.8.20: 同步从 window 取 logger（保证不抛错）
  const logger = (typeof window !== "undefined" && window.webuiLogger) || {
    debug() {}, info() {}, warn() {}, error() {},
  };

  // v0.8.18: 极简 bridge stub——fallbackFetch 内部已经能跑，bridge 只为兼容旧路径存在
  const _fallbackBridge = {
    ready: () => Promise.resolve({ source: "fallback" }),
    apiGet: null,
    apiPost: null,
  };
  const bridge = window.AstrBotPluginPage || _fallbackBridge;
  const _isFallback = bridge === _fallbackBridge;
  try {
    logger.info("init", "bridge:", _isFallback ? "fallback (直 fetch)" : "AstrBotPluginPage (SDK)");
  } catch (e) { console.log("logger not ready:", e); }

  // v0.8.23: detect actual loaded version from index.html's script src
  // AstrBot may serve stale webui after restart——显式出示实际运行的版本，
  // 避免用户看到 console 报错不知道到底跑的是哪个版本
  try {
    const dbBadge = document.getElementById("db-path-badge");
    if (dbBadge) {
      // 从当前页面的 <script> 标签里拿 v=X.Y.Z
      const scripts = document.querySelectorAll('script[src*="app.js"]');
      let ver = "unknown";
      for (const s of scripts) {
        const m = s.src.match(/[?&]v=([0-9.]+)/);
        if (m) { ver = m[1]; break; }
      }
      dbBadge.textContent = `webui: v${ver}`;
      dbBadge.title = `app.js 实际加载的版本。仓库 head: ${(window.__VTB_HEAD_VERSION__) || "unknown"}。不一致说明 AstrBot 没加载新代码。`;
    }
  } catch (e) { console.warn("version badge init failed:", e); }

  // v0.8.19: 右上角 badge 让用户一眼看到 webui 是真加载好了
  try {
    const badge = document.getElementById("bridge-mode-badge");
    if (badge) {
      if (_isFallback) {
        badge.textContent = "🔌 fallback (直 fetch)";
        badge.classList.add("bridge-fallback");
        badge.title = "AstrBot page bridge SDK 不可用，webui 走 fallbackFetch 直 fetch backend。功能 100% 正常。";
      } else {
        badge.textContent = "🟢 bridge (SDK)";
        badge.classList.add("bridge-ok");
        badge.title = "AstrBotPluginPage bridge 注入成功";
      }
    }
  } catch (e) { console.warn("bridge badge init failed:", e); }

  if (typeof bridge.ready === "function") {
    try {
      const ctx = await bridge.ready();
      logger.info("init", "bridge.ready() 完成, ctx=", ctx);
    } catch (e) {
      logger.warn("init", "bridge.ready() 失败，继续走 fallback", e);
    }
  }

  // 把 bridge / _fallbackBridge 暴露到 window 上让下面 init 用
  window._vtb_bridge = bridge;

  // ============= 下面是原来的 webui 逻辑 =============
  const $ = (id) => document.getElementById(id);
  // v0.8.21: 防御性 bind——元素不存在时跳过，避免 TypeError 中断初始化
  const bind = (id, evt, fn) => {
    const el = document.getElementById(id);
    if (!el) {
      console.warn("[vtb] 跳过绑定 #" + id + "：元素不存在");
      return;
    }
    el.addEventListener(evt, fn);
  };

// v0.8.9: LRU 缩略图缓存——Map 维护插入顺序，set 越上限删头部
class LRUCache {
  constructor(limit = 100) {
    this.limit = limit;
    this.m = new Map();
  }
  has(k) { return this.m.has(k); }
  get(k) { return this.m.get(k); }
  set(k, v) {
    if (this.m.has(k)) this.m.delete(k);
    this.m.set(k, v);
    while (this.m.size > this.limit) {
      const first = this.m.keys().next().value;
      this.m.delete(first);
    }
    return v;
  }
  delete(k) { return this.m.delete(k); }
  clear() { this.m.clear(); }
  get size() { return this.m.size; }
}

// v0.8.9: 缩略图并发池——限制同时发 6 路请求，避免 20 条一起打 backend/bridge
class ThumbPool {
  constructor(max = 6) {
    this.max = max;
    this.active = 0;
    this.queue = [];
  }
  run(task) {
    return new Promise((resolve, reject) => {
      this.queue.push({ task, resolve, reject });
      this._drain();
    });
  }
  _drain() {
    while (this.active < this.max && this.queue.length > 0) {
      const { task, resolve, reject } = this.queue.shift();
      this.active++;
      task().then(
        (v) => { this.active--; this._drain(); resolve(v); },
        (e) => { this.active--; this._drain(); reject(e); },
      );
    }
  }
}
const thumbPool = new ThumbPool(6);

const state = {
  limit: 20,
  offset: 0,
  search: "",
  order_by: "created_at_desc",
  total: 0,
  loading: false,
  thumbCache: new LRUCache(100),  // v0.8.9: LRU 上限 100 张防 OOM
  apiStats: { calls: 0, errors: 0, lastLatencyMs: null },
};

// ----- Debug Panel (v0.8.7.2) -----

// v0.8.9: 增量 append——不再每次都全量 innerHTML 重写
const PANEL_MAX_NODES = 200;
const panelNodes = [];  // 存的 DOM 节点

function appendPanelNode(entry) {
  const body = $("debug-body");
  if (!body) return;
  const node = document.createElement("div");
  node.className = `debug-entry lvl-${entry.level}`;
  const ts = escapeHtml(entry.ts);
  const lvl = escapeHtml(entry.level.toUpperCase());
  const tag = escapeHtml(entry.tag);
  const msg = escapeHtml(entry.msg);
  node.innerHTML = `<span class="debug-ts">${ts}</span>
    <span class="debug-level">${lvl}</span>
    <span class="debug-tag">[${tag}]</span>
    <span class="debug-msg">${msg}</span>`;
  body.appendChild(node);
  panelNodes.push(node);
  while (panelNodes.length > PANEL_MAX_NODES) {
    const old = panelNodes.shift();
    old.remove();
  }
  body.scrollTop = body.scrollHeight;
}

function syncPanelCount() {
  const count = $("debug-count");
  if (count) count.textContent = String(logger.logs.length);
}

function renderDebugPanelFull() {
  // 全量重写——只在首次初始化/隐藏后面板重新打开/clear 复位 时调
  const body = $("debug-body");
  if (!body) return;
  body.innerHTML = "";
  panelNodes.length = 0;
  for (const e of logger.logs) appendPanelNode(e);
  syncPanelCount();
}

function initDebugPanel() {
  // 同步初始级别到 select
  const sel = $("debug-level");
  if (sel) sel.value = logger.levelName;
  // 订阅——增量模式，每条新日志只 append 一个 DOM 节点
  logger.onAppend((entry) => {
    if (entry.__clear) {
      renderDebugPanelFull();
      return;
    }
    appendPanelNode(entry);
    syncPanelCount();
  });
  // 首次渲染（以现有 logs 为准）
  renderDebugPanelFull();
  // 按钮
  $("debug-clear")?.addEventListener("click", () => {
    logger.clear();
    logger.info("debug-panel", "日志已清空");
  });
  $("debug-toggle")?.addEventListener("click", () => {
    $("debug-panel").hidden = true;
    $("debug-show").hidden = false;
    logger.debug("debug-panel", "面板隐藏（点 🐞 重新打开）");
  });
  $("debug-show")?.addEventListener("click", () => {
    $("debug-panel").hidden = false;
    $("debug-show").hidden = true;
    renderDebugPanelFull();
    logger.debug("debug-panel", "面板显示");
  });
  $("debug-copy")?.addEventListener("click", async () => {
    const text = logger.logs
      .map((e) => `[${e.ts}] [${e.level.toUpperCase()}] [${e.tag}] ${e.msg}`)
      .join("\n");
    try {
      await navigator.clipboard.writeText(text);
      logger.info("debug-panel", `已复制 ${logger.logs.length} 条日志到剪贴板`);
    } catch (e) {
      logger.warn("debug-panel", "clipboard 不可用，回退到 textarea", e);
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch { /* ignore */ }
      document.body.removeChild(ta);
    }
  });
  $("debug-download")?.addEventListener("click", () => {
    const text = logger.logs
      .map((e) => `[${e.ts}] [${e.level.toUpperCase()}] [${e.tag}] ${e.msg}`)
      .join("\n");
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `vtb_webui_${new Date().toISOString().replace(/[:.]/g, "-")}.log`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    logger.info("debug-panel", `已下载 ${logger.logs.length} 条日志`);
  });
  sel?.addEventListener("change", (e) => {
    logger.setLevel(e.target.value);
    logger.info("debug-panel", `日志级别切换为 ${e.target.value}`);
  });
  // 默认 debug 模式 (开发方便)。首次启动默认 info，但 URL 加 ?debug=1 直接开 debug
  const urlParams = new URLSearchParams(location.search);
  if (urlParams.has("debug")) {
    const v = urlParams.get("debug");
    if (v === "1" || v === "true") {
      logger.setLevel("debug");
      sel.value = "debug";
      logger.info("init", "URL ?debug=1 → 自动切到 debug 级别");
    }
  }
  logger.info("init", `Debug 面板初始化完成，当前级别=${logger.levelName}`);
}

initDebugPanel();

// ----- API 包装（统一加日志） -----

// v0.8.14: bridge.apiGet/apiPost 不存在时 fallback 到直 fetch backend
// v0.8.22: PLUGIN_PATH 末尾不加 /——下下面负责保证 endpoint 有 “/” 开头
const PLUGIN_PATH = `/api/plug/astrbot_plugin_vision_text_bridge`;
async function fallbackFetch(method, endpoint, payload) {
  // 防御性：endpoint 必须以 / 开头，偷漏了 / 会拼成 ...bridgecache/stats (错)
  const ep = endpoint.startsWith("/") ? endpoint : `/${endpoint}`;
  const url = `${PLUGIN_PATH}${ep}`;
  const init = {
    method,
    headers: { "Content-Type": "application/json" },
  };
  if (payload && Object.keys(payload).length > 0) {
    if (method === "GET") {
      const qs = new URLSearchParams(payload).toString();
      if (qs) init.url = `${url}?${qs}`;
      else init.url = url;
    } else {
      init.body = JSON.stringify(payload);
      init.url = url;
    }
  } else {
    init.url = url;
  }
  logger.warn("api", `fallback fetch: ${method} ${init.url}`);
  const resp = await fetch(init.url, init);
  return { ok: resp.ok, data: await resp.json(), status: resp.status };
}

async function apiGet(endpoint, params = {}) {
  const t0 = performance.now();
  state.apiStats.calls += 1;
  logger.debug("api", `GET ${endpoint}`, params);
  try {
    let resp;
    if (typeof bridge.apiGet === "function") {
      resp = await bridge.apiGet(endpoint, params);
    } else {
      resp = await fallbackFetch("GET", endpoint, params);
    }
    const dt = (performance.now() - t0).toFixed(1);
    state.apiStats.lastLatencyMs = dt;
    const data = resp?.data || resp;
    const ok = resp?.ok !== false;
    if (ok) {
      logger.info("api", `GET ${endpoint} OK ${dt}ms`, _summarize(data));
    } else {
      state.apiStats.errors += 1;
      logger.warn("api", `GET ${endpoint} 失败 ${dt}ms`, resp?.error || resp);
    }
    return resp;
  } catch (e) {
    state.apiStats.errors += 1;
    logger.error("api", `GET ${endpoint} 异常`, e);
    throw e;
  }
}

async function apiPost(endpoint, body = {}) {
  const t0 = performance.now();
  state.apiStats.calls += 1;
  logger.debug("api", `POST ${endpoint}`, body);
  try {
    let resp;
    if (typeof bridge.apiPost === "function") {
      resp = await bridge.apiPost(endpoint, body);
    } else {
      resp = await fallbackFetch("POST", endpoint, body);
    }
    const dt = (performance.now() - t0).toFixed(1);
    state.apiStats.lastLatencyMs = dt;
    const ok = resp?.ok !== false;
    const data = resp?.data || resp;
    if (ok) {
      logger.info("api", `POST ${endpoint} OK ${dt}ms`, _summarize(data));
    } else {
      state.apiStats.errors += 1;
      logger.warn("api", `POST ${endpoint} 失败 ${dt}ms`, resp?.error || resp);
    }
    return resp;
  } catch (e) {
    state.apiStats.errors += 1;
    logger.error("api", `POST ${endpoint} 异常`, e);
    throw e;
  }
}

function _summarize(data) {
  if (!data) return data;
  if (Array.isArray(data.items)) {
    return { total: data.total, items_count: data.items.length, first_id: data.items[0]?.image_id?.slice(0, 12) };
  }
  if (data.image_id) return { image_id: data.image_id.slice(0, 12), has_image: data.has_image, mime: data.mime_type };
  return data;
}

// ----- utils -----

function showToast(message, type = "success", duration = 2400) {
  logger.debug("ui", `toast ${type}: ${message}`);
  const el = $("toast");
  el.textContent = message;
  el.className = `toast show ${type}`;
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => {
    el.className = "toast";
  }, duration);
}

// v0.8.8: 自建 confirm（window.confirm 在 sandboxed iframe 里被禁用）
function customConfirm(message, title = "⚠️ 确认操作") {
  return new Promise((resolve) => {
    const modal = $("confirm-modal");
    const titleEl = $("confirm-title");
    const msgEl = $("confirm-message");
    const okBtn = $("confirm-ok");
    const cancelBtn = $("confirm-cancel");
    titleEl.textContent = title;
    msgEl.textContent = message;
    modal.hidden = false;
    const cleanup = (val) => {
      modal.hidden = true;
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      modal.removeEventListener("click", onMask);
      document.removeEventListener("keydown", onKey);
      resolve(val);
    };
    const onOk = () => cleanup(true);
    const onCancel = () => cleanup(false);
    const onMask = (e) => { if (e.target === modal) cleanup(false); };
    const onKey = (e) => { if (e.key === "Escape") cleanup(false); };
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    modal.addEventListener("click", onMask);
    document.addEventListener("keydown", onKey);
  });
}

function fmtTime(ts) {
  if (!ts || ts <= 0) return "-";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  );
}

function fmtSize(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = bytes;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function truncate(s, n) {
  if (!s) return "";
  return s.length > n ? s.slice(0, n) + "…" : s;
}

// ----- data loading -----

async function loadStats() {
  logger.debug("data", "loadStats() 开始");
  try {
    const resp = await apiGet("/cache/stats");
    const data = resp?.data || resp;
    $("stat-total").textContent = data.total ?? 0;
    $("stat-hits").textContent = data.total_hits ?? 0;
    $("stat-dbsize").textContent = fmtSize(data.db_size_bytes);
    $("stat-memcache").textContent = data.in_memory_cache_size ?? 0;
    // v0.8.11: 同步设置右上角 DB 路径 badge（以前是写死的 "loading…"，现在拿到实际路径）
    const dbBadge = $("db-path-badge");
    if (dbBadge && data.db_path) {
      const tail = data.db_path.split(/[/\\]/).pop() || data.db_path;
      dbBadge.textContent = `DB: ${tail}`;
      dbBadge.title = data.db_path;
    }
    // v0.8.12: 状态栏
    renderStatusBar(data);
    logger.info("data", "loadStats 完成", { total: data.total, hits: data.total_hits, dbsize: data.db_size_bytes });
  } catch (e) {
    logger.error("data", "loadStats 失败", e);
    showToast("加载统计失败: " + (e?.message || e), "error");
  }
}

// v0.8.12: 状态栏渲染（TTL/上限/下次清理）
function renderStatusBar(data) {
  const memTtl = data.memory_cache_ttl_seconds;
  $("status-mem-ttl").textContent = memTtl > 0 ? `${memTtl}s` : "永不过期";
  $("status-mem-max").textContent = data.memory_cache_max_size > 0
    ? `${data.in_memory_cache_size ?? 0} / ${data.memory_cache_max_size}`
    : "不限制";
  $("status-sql-ttl").textContent = data.sqlite_cache_ttl_days > 0
    ? `${data.sqlite_cache_ttl_days} 天`
    : "永不过期";
  const next = data.next_clean_at;
  if (!next || data.sqlite_clean_interval_hours === 0) {
    $("status-next-clean").textContent = "已禁用";
  } else {
    const ts = next * 1000;  // UTC 秒 → 毫秒
    const now = Date.now();
    const deltaMs = ts - now;
    if (deltaMs <= 0) {
      $("status-next-clean").textContent = "即将执行";
    } else {
      // 友好的相对时间
      const mins = Math.floor(deltaMs / 60000);
      const secs = Math.floor((deltaMs % 60000) / 1000);
      $("status-next-clean").textContent = mins > 0 ? `${mins}m ${secs}s 后` : `${secs}s 后`;
    }
  }
}

// v0.8.12: 拉取 + 画柱状图
let _timelineCache = null;  // 上次拉的 buckets，供自动刷新复用
async function loadTimeline() {
  try {
    const resp = await apiGet("/cache/stats/timeline", { days: 30 });
    const data = resp?.data || resp;
    _timelineCache = data.buckets || [];
    drawTimeline(_timelineCache);
    logger.debug("timeline", `已画 ${_timelineCache.length} 天柱状图`);
  } catch (e) {
    logger.error("timeline", "loadTimeline 失败", e);
  }
}

function drawTimeline(buckets) {
  const svg = $("timeline-svg");
  if (!svg) return;
  // 清除旧内容
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  if (!buckets || buckets.length === 0) return;
  const W = 800, H = 180;
  const padL = 36, padR = 12, padT = 12, padB = 28;
  const chartW = W - padL - padR;
  const chartH = H - padT - padB;
  const n = buckets.length;
  const maxCount = Math.max(1, ...buckets.map(b => b.count));
  const barW = chartW / n;
  const innerW = barW * 0.72;  // 柱间留空
  const innerOffset = (barW - innerW) / 2;

  const today = new Date().toISOString().slice(0, 10);

  // Y 轴网格 + 标签（0, mid, max）
  for (let i = 0; i <= 3; i++) {
    const y = padT + (chartH * i / 3);
    const v = Math.round(maxCount * (3 - i) / 3);
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", padL);
    line.setAttribute("x2", W - padR);
    line.setAttribute("y1", y);
    line.setAttribute("y2", y);
    line.setAttribute("class", "axis-line");
    svg.appendChild(line);
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", padL - 6);
    label.setAttribute("y", y + 3);
    label.setAttribute("text-anchor", "end");
    label.setAttribute("class", "axis-label");
    label.textContent = v.toString();
    svg.appendChild(label);
  }

  // 柱
  buckets.forEach((b, i) => {
    const h = (b.count / maxCount) * chartH;
    const x = padL + i * barW + innerOffset;
    const y = padT + chartH - h;
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("x", x);
    rect.setAttribute("y", y);
    rect.setAttribute("width", innerW);
    rect.setAttribute("height", Math.max(0, h));
    rect.setAttribute("class", b.date === today ? "bar bar-today" : "bar");
    rect.setAttribute("rx", 2);
    // hover tooltip
    rect.addEventListener("mouseenter", (e) => showTimelineTooltip(b, e));
    rect.addEventListener("mousemove", (e) => positionTimelineTooltip(e));
    rect.addEventListener("mouseleave", hideTimelineTooltip);
    svg.appendChild(rect);
  });

  // X 轴日期标签（隔 5 天一个，避免重叠）
  const step = Math.max(1, Math.floor(n / 6));
  for (let i = 0; i < n; i += step) {
    const b = buckets[i];
    const x = padL + i * barW + barW / 2;
    const y = H - padB + 16;
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", x);
    label.setAttribute("y", y);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("class", "axis-label");
    label.textContent = b.date.slice(5);  // MM-DD
    svg.appendChild(label);
  }
}

let _timelineTip = null;
function showTimelineTooltip(b, evt) {
  hideTimelineTooltip();
  const svg = $("timeline-svg");
  _timelineTip = document.createElementNS("http://www.w3.org/2000/svg", "g");
  const text1 = document.createElementNS("http://www.w3.org/2000/svg", "text");
  text1.setAttribute("class", "tooltip-text");
  text1.setAttribute("x", 0);
  text1.setAttribute("y", 14);
  text1.textContent = `${b.date}  ${b.count} 条`;
  const text2 = document.createElementNS("http://www.w3.org/2000/svg", "text");
  text2.setAttribute("class", "tooltip-text");
  text2.setAttribute("x", 0);
  text2.setAttribute("y", 30);
  text2.textContent = `今日${b.date === new Date().toISOString().slice(0, 10) ? " ✓" : ""}`;
  const w = Math.max(text1.getComputedTextLength?.() || 80, 80) + 16;
  const bg = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  bg.setAttribute("class", "tooltip-bg");
  bg.setAttribute("x", 0);
  bg.setAttribute("y", 0);
  bg.setAttribute("width", w);
  bg.setAttribute("height", 38);
  _timelineTip.appendChild(bg);
  _timelineTip.appendChild(text1);
  _timelineTip.appendChild(text2);
  _timelineTip.setAttribute("transform", `translate(${evt.offsetX + 12}, ${evt.offsetY - 8})`);
  svg.appendChild(_timelineTip);
}
function positionTimelineTooltip(evt) {
  if (!_timelineTip) return;
  _timelineTip.setAttribute("transform", `translate(${evt.offsetX + 12}, ${evt.offsetY - 8})`);
}
function hideTimelineTooltip() {
  if (_timelineTip && _timelineTip.parentNode) _timelineTip.parentNode.removeChild(_timelineTip);
  _timelineTip = null;
}

async function loadList() {
  if (state.loading) {
    logger.debug("data", "loadList 跳过（已在加载中）");
    return;
  }
  state.loading = true;
  logger.debug("data", "loadList 开始", { offset: state.offset, limit: state.limit, search: state.search });
  try {
    const resp = await apiGet("/cache/list", {
      limit: state.limit,
      offset: state.offset,
      search: state.search,
      order_by: state.order_by,
    });
    const data = resp?.data || resp;
    state.total = data.total ?? 0;
    const items = data.items || [];
    renderList(items);
    const from = state.total === 0 ? 0 : state.offset + 1;
    const to = Math.min(state.offset + state.limit, state.total);
    $("page-info").textContent = `共 ${state.total} 条 · 当前 ${from}-${to}`;
    logger.info("data", "loadList 完成", { total: state.total, returned: items.length, range: `${from}-${to}` });
  } catch (e) {
    logger.error("data", "loadList 失败", e);
    showToast("加载列表失败: " + (e?.message || e), "error");
    renderList([]);
  } finally {
    state.loading = false;
  }
}

function renderList(items) {
  logger.debug("render", `renderList: ${items.length} 条`);
  const tbody = $("cache-tbody");
  if (items.length === 0) {
    tbody.innerHTML = `<tr class="empty"><td colspan="7">
      <div class="empty-state">
        <div class="empty-icon">${state.search ? "🔍" : "📭"}</div>
        <div>${state.search ? "没有匹配项" : "暂无缓存"}</div>
      </div>
    </td></tr>`;
    return;
  }
  tbody.innerHTML = items
    .map((it) => {
      const descId = `desc-${it.image_id}`;
      const dim = it.width && it.height ? `${it.width}×${it.height}` : "—";
      const needsToggle = (it.description || "").length > 280;
      return `
    <tr data-id="${escapeHtml(it.image_id)}">
      <td>
        <div class="thumb-slot" data-id="${escapeHtml(it.image_id)}">
          <div class="thumb-placeholder">🖼️</div>
        </div>
      </td>
      <td>
        <div class="url-cell" title="${escapeHtml(it.image_url)}">
          ${escapeHtml(truncate(it.image_url, 200))}
        </div>
        <div style="margin-top: 4px;">
          <code style="font-size: 0.7rem; color: var(--text-muted);">${escapeHtml((it.image_id || "").slice(0, 16))}…</code>
        </div>
      </td>
      <td>
        <div class="desc-cell" id="${descId}">${escapeHtml(it.description || "")}</div>
        ${needsToggle
          ? `<button class="desc-toggle" data-target="${descId}">展开 ↓</button>`
          : ""}
      </td>
      <td class="hit-cell">${it.hit_count}</td>
      <td class="dim-cell">${dim}</td>
      <td class="time-cell">${fmtTime(it.created_at)}</td>
      <td class="action-cell">
        <button class="btn" data-action="view" data-id="${escapeHtml(it.image_id)}" title="查看大图">👁️</button>
        <button class="btn" data-action="regen" data-id="${escapeHtml(it.image_id)}" title="重新生成">🔁</button>
        <button class="btn danger" data-action="delete" data-id="${escapeHtml(it.image_id)}" title="删除">🗑️</button>
      </td>
    </tr>
  `;
    })
    .join("");

  // 懒加载缩略图（v0.8.9: 走并发池 6 路，不再一次性 20 个打 bridge）
  const slots = Array.from(tbody.querySelectorAll(".thumb-slot"));
  slots.forEach((slot) => ensureThumb(slot.dataset.id, slot));
  logger.debug("render", `已提交 ${slots.length} 张缩略图到并发池（上限 6）`);

  // 操作按钮
  tbody.querySelectorAll("button[data-action]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const action = btn.dataset.action;
      const id = btn.dataset.id;
      logger.debug("ui", `点击 ${action} 按钮`, { id });
      if (action === "delete") onDelete(id);
      else if (action === "regen") onRegenerate(id);
      else if (action === "view") onView(id);
    });
  });

  // 描述展开
  tbody.querySelectorAll(".desc-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const cell = $(btn.dataset.target);
      if (cell) {
        cell.classList.toggle("expanded");
        btn.textContent = cell.classList.contains("expanded") ? "收起 ↑" : "展开 ↓";
        logger.debug("ui", `描述展开状态切换: ${cell.classList.contains("expanded")}`);
      }
    });
  });
}

// ----- thumbnail lazy loading -----

async function ensureThumb(imageId, slot) {
  if (!imageId || !slot) return;
  if (state.thumbCache.has(imageId)) {
    const cached = state.thumbCache.get(imageId);
    if (cached.__err) {
      slot.innerHTML = `<div class="thumb-placeholder" title="缩略图加载失败">⚠️</div>`;
    } else if (cached.__none) {
      slot.innerHTML = `<div class="thumb-placeholder" title="该条目没有图片二进制 (v0.8.5 之前数据)">📦</div>`;
    } else {
      logger.debug("thumb", `命中缩略图缓存: ${imageId.slice(0, 12)}`);
      renderThumb(slot, cached);
    }
    return;
  }
  // v0.8.9: 走并发池（默认 6 路）避免一次性 20 个 RTT 堆 bridge
  return thumbPool.run(async () => {
    try {
      const resp = await apiGet(`/cache/thumbnail/${encodeURIComponent(imageId)}`);
      const data = resp?.data || resp;
      if (data?.has_image && data.data_url) {
        const thumb = { data_url: data.data_url, mime: data.mime_type, w: data.width, h: data.height };
        state.thumbCache.set(imageId, thumb);
        renderThumb(slot, thumb);
        logger.debug("thumb", `加载缩略图成功: ${imageId.slice(0, 12)}`, { mime: thumb.mime, w: thumb.w, h: thumb.h });
      } else {
        state.thumbCache.set(imageId, { __none: true });  // 失败也 cache 避免重试
        slot.innerHTML = `<div class="thumb-placeholder" title="该条目没有图片二进制 (v0.8.5 之前数据)">📦</div>`;
        logger.debug("thumb", `无缩略图（v0.8.5 之前数据）: ${imageId.slice(0, 12)}`);
      }
    } catch (e) {
      state.thumbCache.set(imageId, { __err: true });  // 失败也 cache 避免无限重试
      slot.innerHTML = `<div class="thumb-placeholder" title="缩略图加载失败">⚠️</div>`;
      logger.warn("thumb", `加载缩略图失败: ${imageId.slice(0, 12)}`, e);
    }
  });
}

function renderThumb(slot, thumb) {
  slot.innerHTML = `<img class="thumb" src="${thumb.data_url}" alt="thumb" loading="lazy" />`;
  const img = slot.querySelector("img");
  if (img) {
    img.addEventListener("click", () => {
      logger.debug("ui", "点击缩略图看大图");
      showModalImg(thumb);
    });
  }
}

// ----- modal: 查看详情 -----

async function onView(imageId) {
  logger.info("ui", `查看缓存详情: ${imageId.slice(0, 12)}`);
  try {
    const resp = await apiGet(`/cache/thumbnail/${encodeURIComponent(imageId)}`);
    const data = resp?.data || resp;
    const body = $("modal-body");
    let html = "";
    if (data?.has_image) {
      html += `<div class="field">
        <div class="field-label">缩略图</div>
        <img src="${data.data_url}" alt="full" />
      </div>`;
    } else {
      html += `<div class="field">
        <div class="field-label">缩略图</div>
        <div style="color: var(--text-muted);">该条目未存储图片二进制（v0.8.5 之前的数据）</div>
      </div>`;
    }
    html += `
      <div class="field">
        <div class="field-label">image_id</div>
        <div class="field-value"><code>${escapeHtml(imageId)}</code></div>
      </div>
      <div class="field">
        <div class="field-label">mime_type / 尺寸</div>
        <div class="field-value">${escapeHtml(data.mime_type || "—")} · ${data.width || "—"}×${data.height || "—"} · ${fmtSize(data.file_size || 0)}</div>
      </div>`;
    body.innerHTML = html;
    $("detail-modal").hidden = false;
  } catch (e) {
    logger.error("ui", "查看缓存详情失败", e);
    showToast("查看失败: " + (e?.message || e), "error");
  }
}

function showModalImg(thumb) {
  logger.debug("ui", "弹大图 modal");
  const body = $("modal-body");
  body.innerHTML = `<div class="field">
    <div class="field-label">原图（${thumb.mime || "image"}）</div>
    <img src="${thumb.data_url}" alt="full" />
  </div>`;
  $("detail-modal").hidden = false;
}

bind("modal-close", "click", () => {
  logger.debug("ui", "关闭详情 modal");
  $("detail-modal").hidden = true;
});

bind("detail-modal", "click", (e) => {
  if (e.target.id === "detail-modal") {
    logger.debug("ui", "点击 modal 背景关闭");
    $("detail-modal").hidden = true;
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("detail-modal").hidden) {
    $("detail-modal").hidden = true;
    logger.debug("ui", "Esc 键关闭 modal");
  }
});

// ----- actions -----

async function onDelete(id) {
  logger.info("ui", `请求删除: ${id.slice(0, 12)}`);
  if (!await customConfirm(`删除缓存条目?\n${id}`)) {
    logger.debug("ui", "删除被取消");
    return;
  }
  try {
    const resp = await apiPost("/cache/delete", { key: id });
    if (resp?.ok !== false) {
      state.thumbCache.delete(id);
      logger.info("ui", `删除成功: ${id.slice(0, 12)}`);
      showToast("已删除");
      await Promise.all([loadStats(), loadList()]);
    } else {
      logger.warn("ui", `删除失败: ${resp?.error || "未知"}`);
      showToast("删除失败: " + (resp?.error || "未知"), "error");
    }
  } catch (e) {
    logger.error("ui", "删除异常", e);
    showToast("删除失败: " + (e?.message || e), "error");
  }
}

async function onRegenerate(id) {
  logger.info("ui", `请求重新生成: ${id.slice(0, 12)}`);
  showToast("正在重新生成描述...");
  try {
    const resp = await apiPost("cache/regenerate", { key: id });
    if (resp?.ok !== false) {
      const data = resp?.data || resp;
      if (data.ok) {
        logger.info("ui", `重新生成成功: ${id.slice(0, 12)} (长度=${data.description?.length || 0})`);
        showToast("重新生成成功（长度=" + (data.description?.length || 0) + "）");
      } else {
        logger.warn("ui", `重新生成失败: mmx 调用失败`);
        showToast("重新生成失败：mmx 调用失败", "error");
      }
      await Promise.all([loadStats(), loadList()]);
    } else {
      logger.warn("ui", `重新生成失败: ${resp?.error || "未知"}`);
      showToast("重新生成失败: " + (resp?.error || "未知"), "error");
    }
  } catch (e) {
    logger.error("ui", "重新生成异常", e);
    showToast("重新生成失败: " + (e?.message || e), "error");
  }
}

async function onClear() {
  logger.info("ui", "请求清空全部缓存");
  if (!await customConfirm("确定要清空所有缓存条目吗？此操作不可撤销。", "🗑️ 清空确认")) {
    logger.debug("ui", "清空被取消");
    return;
  }
  try {
    const resp = await apiPost("cache/clear", {});
    const data = resp?.data || resp;
    logger.info("ui", `清空完成: ${data?.cleared ?? 0} 条`);
    showToast("已清空 " + (data?.cleared ?? 0) + " 条");
    state.thumbCache.clear();
    await Promise.all([loadStats(), loadList()]);
  } catch (e) {
    logger.error("ui", "清空异常", e);
    showToast("清空失败: " + (e?.message || e), "error");
  }
}

async function onCleanExpired() {
  logger.info("ui", "请求清理过期缓存");
  if (!await customConfirm("清理所有超期未命中的缓存条目?\n（SQLite + 内存热缓存）", "🧹 清理过期")) {
    logger.debug("ui", "清理被取消");
    return;
  }
  try {
    const resp = await apiPost("cache/clean_expired", {});
    if (resp?.ok !== false) {
      const data = resp?.data || resp;
      const sql = data?.deleted_sqlite ?? 0;
      const mem = data?.purged_memory ?? 0;
      logger.info("ui", `清理完成: SQLite=${sql}条, 内存=${mem}条 (TTL=${data?.ttl_days}天)`);
      showToast(`清理完成: SQLite ${sql} 条 + 内存 ${mem} 条过期`);
      state.thumbCache.clear();
      await Promise.all([loadStats(), loadList()]);
    } else {
      logger.warn("ui", `清理失败: ${resp?.error || "未知"}`);
      showToast("清理失败: " + (resp?.error || "未知"), "error");
    }
  } catch (e) {
    logger.error("ui", "清理异常", e);
    showToast("清理失败: " + (e?.message || e), "error");
  }
}

async function onExport() {
  logger.info("ui", "导出 JSON");
  try {
    const resp = await apiGet("cache/export");
    const data = resp?.data || resp;
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `vision_text_bridge_cache_${Math.floor(Date.now() / 1000)}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    logger.info("ui", `已下载 ${data.count || 0} 条`);
    showToast("已下载 " + (data.count || 0) + " 条");
  } catch (e) {
    logger.error("ui", "导出失败", e);
    showToast("导出失败: " + (e?.message || e), "error");
  }
}

async function onDiag() {
  logger.info("ui", "打开诊断面板");
  try {
    const resp = await apiGet("cache/diag");
    const data = resp?.data || resp;
    const body = $("modal-body");
    let html = "";
    if (!data.cache_initialized) {
      html = `<div class="field"><div class="field-label">错误</div>
        <div class="field-value" style="color: var(--danger);">${escapeHtml(data.hint || "未知")}</div></div>`;
    } else if (data.error) {
      html = `<div class="field"><div class="field-label">SQLite 错误</div>
        <div class="field-value" style="color: var(--danger);">${escapeHtml(data.error)}</div></div>`;
    } else {
      html += `<div class="field"><div class="field-label">DB 路径</div>
        <div class="field-value"><code>${escapeHtml(data.db_path || "")}</code></div></div>`;
      html += `<div class="field"><div class="field-label">总条目数 / 内存热缓存</div>
        <div class="field-value">${data.total_entries} 条 / ${data.in_memory_cache_size} 条</div></div>`;
      html += `<div class="field"><div class="field-label">Schema 列</div>
        <div class="field-value"><pre>${escapeHtml((data.schema_columns || []).join(", "))}</pre></div></div>`;
      if (data.recent_3 && data.recent_3.length > 0) {
        html += `<div class="field"><div class="field-label">最近 ${data.recent_3.length} 条</div>
          <div class="field-value"><pre>${escapeHtml(JSON.stringify(data.recent_3, null, 2))}</pre></div></div>`;
      } else {
        html += `<div class="field"><div class="field-label">最近记录</div>
          <div class="field-value" style="color: var(--danger);">⚠️ SQLite 表是空的——没数据被写入</div></div>`;
      }
    }
    body.innerHTML = html;
    $("detail-modal").hidden = false;
  } catch (e) {
    logger.error("ui", "诊断失败", e);
    showToast("诊断失败: " + (e?.message || e), "error");
  }
}

// ----- event binding -----

bind("refresh-btn", "click", async () => {
  logger.info("ui", "点击刷新按钮");
  state.thumbCache.clear();
  await Promise.all([loadStats(), loadList(), loadTimeline()]);
});
bind("clear-btn", "click", onClear);
$("clean-expired-btn")?.addEventListener("click", onCleanExpired);
bind("export-btn", "click", onExport);

// v0.8.12: 自动刷新 toggle
let _autoRefreshTimer = null;
const AUTO_REFRESH_MS = 5000;  // 5 秒间隔
function setAutoRefresh(enabled) {
  if (_autoRefreshTimer) {
    clearInterval(_autoRefreshTimer);
    _autoRefreshTimer = null;
  }
  if (enabled) {
    logger.info("auto-refresh", `开启自动刷新, 间隔 ${AUTO_REFRESH_MS}ms`);
    _autoRefreshTimer = setInterval(async () => {
      logger.debug("auto-refresh", "tick");
      try {
        await Promise.all([loadStats(), loadList(), loadTimeline()]);
      } catch (e) {
        logger.warn("auto-refresh", "tick 失败: " + (e?.message || e));
      }
    }, AUTO_REFRESH_MS);
  } else {
    logger.info("auto-refresh", "关闭自动刷新");
  }
}
$("auto-refresh-toggle")?.addEventListener("change", (e) => {
  setAutoRefresh(e.target.checked);
});
bind("diag-btn", "click", onDiag);

let searchTimer = null;
bind("search-input", "input", (e) => {
  clearTimeout(searchTimer);
  const v = e.target.value.trim();
  logger.debug("ui", `搜索输入变化: "${v}"`);
  searchTimer = setTimeout(() => {
    state.search = v;
    state.offset = 0;
    loadList();
  }, 300);
});

bind("order-by", "change", (e) => {
  logger.info("ui", `排序切换: ${e.target.value}`);
  state.order_by = e.target.value;
  state.offset = 0;
  loadList();
});

bind("prev-btn", "click", () => {
  state.offset = Math.max(0, state.offset - state.limit);
  logger.info("ui", `翻到上一页, offset=${state.offset}`);
  loadList();
});
bind("next-btn", "click", () => {
  if (state.offset + state.limit < state.total) {
    state.offset += state.limit;
    logger.info("ui", `翻到下一页, offset=${state.offset}`);
    loadList();
  } else {
    logger.debug("ui", "已是最后一页");
  }
});

// 键盘快捷键
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  if (e.key === "r" || e.key === "R") {
    logger.debug("ui", "快捷键 R → 刷新");
    $("refresh-btn").click();
  }
});

// 初始加载
logger.info("init", "开始初始加载 stats + list + timeline");
await Promise.all([loadStats(), loadList(), loadTimeline()]);
logger.info("init", "初始加载完成", { ...state.apiStats, logs_count: logger.logs.length });

if (typeof bridge.onContext === "function") {
  bridge.onContext(() => {
    logger.debug("init", "bridge context 变化（无操作）");
  });
}

})().catch((e) => {
  console.error("[vtb] 启动崩溃:", e);
  try {
    document.body.innerHTML = '<div style="padding: 24px; color: #f87171; background: #0b0f19; font-family: monospace; white-space: pre-wrap; min-height: 100vh;">'
      + '<h1>❌ Webui 启动失败</h1>'
      + '<pre style="color: #e2e8f0;">' + (e && e.stack ? e.stack : String(e)) + '</pre>'
      + '</div>';
  } catch (_) { /* ignore */ }
});
