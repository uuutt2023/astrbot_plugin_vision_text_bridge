/* Vision Text Bridge · Webui Logic
 *
 * v0.8.6 重写：glassmorphism 风格 + 缩略图 + 模态详情
 * v0.8.7.2: 全面接入 logger.js（控制台双输出 + on-screen panel）
 */

import logger from "./logger.js";

const bridge = window.AstrBotPluginPage;
logger.info("init", "等待 bridge.ready()…");
const ctx = await bridge.ready();
logger.info("init", "bridge.ready() 完成, ctx=", ctx);

const $ = (id) => document.getElementById(id);

const state = {
  limit: 20,
  offset: 0,
  search: "",
  order_by: "created_at_desc",
  total: 0,
  loading: false,
  thumbCache: new Map(),  // image_id -> { data_url, mime }
  apiStats: { calls: 0, errors: 0, lastLatencyMs: null },
};

// ----- Debug Panel (v0.8.7.2) -----

function renderDebugPanel() {
  const body = $("debug-body");
  const count = $("debug-count");
  if (!body) return;
  const logs = logger.logs;
  count.textContent = String(logs.length);
  body.innerHTML = logs
    .map((e) => {
      const ts = escapeHtml(e.ts);
      const lvl = escapeHtml(e.level.toUpperCase());
      const tag = escapeHtml(e.tag);
      const msg = escapeHtml(e.msg);
      return `<div class="debug-entry lvl-${e.level}">
        <span class="debug-ts">${ts}</span>
        <span class="debug-level">${lvl}</span>
        <span class="debug-tag">[${tag}]</span>
        <span class="debug-msg">${msg}</span>
      </div>`;
    })
    .join("");
  // 滚到底
  body.scrollTop = body.scrollHeight;
}

function initDebugPanel() {
  // 同步初始级别到 select
  const sel = $("debug-level");
  if (sel) sel.value = logger.levelName;
  // 订阅日志
  logger.subscribe((event) => {
    if (event === "__level__" || event === "__log__" || event === "__clear__") {
      renderDebugPanel();
    }
  });
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
    renderDebugPanel();
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

async function apiGet(endpoint, params = {}) {
  const t0 = performance.now();
  state.apiStats.calls += 1;
  logger.debug("api", `GET ${endpoint}`, params);
  try {
    const resp = await bridge.apiGet(endpoint, params);
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
    const resp = await bridge.apiPost(endpoint, body);
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

function fmtDim(w, h) {
  if (!w || !h) return "—";
  return `${w}×${h}`;
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
    const resp = await apiGet("cache/stats");
    const data = resp?.data || resp;
    $("stat-total").textContent = data.total ?? 0;
    $("stat-hits").textContent = data.total_hits ?? 0;
    $("stat-dbsize").textContent = fmtSize(data.db_size_bytes);
    $("stat-memcache").textContent = data.in_memory_cache_size ?? 0;
    logger.info("data", "loadStats 完成", { total: data.total, hits: data.total_hits, dbsize: data.db_size_bytes });
  } catch (e) {
    logger.error("data", "loadStats 失败", e);
    showToast("加载统计失败: " + (e?.message || e), "error");
  }
}

async function loadList() {
  if (state.loading) {
    logger.debug("data", "loadList 跳过（已在加载中）");
    return;
  }
  state.loading = true;
  logger.debug("data", "loadList 开始", { offset: state.offset, limit: state.limit, search: state.search });
  try {
    const resp = await apiGet("cache/list", {
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

  // 懒加载缩略图
  let thumbCount = 0;
  tbody.querySelectorAll(".thumb-slot").forEach((slot) => {
    const id = slot.dataset.id;
    ensureThumb(id, slot);
    thumbCount += 1;
  });
  logger.debug("render", `开始懒加载 ${thumbCount} 张缩略图`);

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
    logger.debug("thumb", `命中缩略图缓存: ${imageId.slice(0, 12)}`);
    renderThumb(slot, state.thumbCache.get(imageId));
    return;
  }
  try {
    const resp = await apiPost("cache/thumbnail", { image_id: imageId });
    const data = resp?.data || resp;
    if (data?.has_image && data.data_url) {
      const thumb = { data_url: data.data_url, mime: data.mime_type, w: data.width, h: data.height };
      state.thumbCache.set(imageId, thumb);
      renderThumb(slot, thumb);
      logger.debug("thumb", `加载缩略图成功: ${imageId.slice(0, 12)}`, { mime: thumb.mime, w: thumb.w, h: thumb.h });
    } else {
      slot.innerHTML = `<div class="thumb-placeholder" title="该条目没有图片二进制 (v0.8.5 之前数据)">📦</div>`;
      logger.debug("thumb", `无缩略图（v0.8.5 之前数据）: ${imageId.slice(0, 12)}`);
    }
  } catch (e) {
    slot.innerHTML = `<div class="thumb-placeholder" title="缩略图加载失败">⚠️</div>`;
    logger.warn("thumb", `加载缩略图失败: ${imageId.slice(0, 12)}`, e);
  }
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
    const resp = await apiPost("cache/thumbnail", { image_id: imageId });
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

$("modal-close").addEventListener("click", () => {
  logger.debug("ui", "关闭详情 modal");
  $("detail-modal").hidden = true;
});

$("detail-modal").addEventListener("click", (e) => {
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
  if (!confirm("删除缓存条目?\n" + id)) {
    logger.debug("ui", "删除被取消");
    return;
  }
  try {
    const resp = await apiPost("cache/delete", { key: id });
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
  if (!confirm("确定要清空所有缓存条目吗？此操作不可撤销。")) {
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

$("refresh-btn").addEventListener("click", async () => {
  logger.info("ui", "点击刷新按钮");
  state.thumbCache.clear();
  await Promise.all([loadStats(), loadList()]);
});
$("clear-btn").addEventListener("click", onClear);
$("export-btn").addEventListener("click", onExport);
$("diag-btn").addEventListener("click", onDiag);

let searchTimer = null;
$("search-input").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const v = e.target.value.trim();
  logger.debug("ui", `搜索输入变化: "${v}"`);
  searchTimer = setTimeout(() => {
    state.search = v;
    state.offset = 0;
    loadList();
  }, 300);
});

$("order-by").addEventListener("change", (e) => {
  logger.info("ui", `排序切换: ${e.target.value}`);
  state.order_by = e.target.value;
  state.offset = 0;
  loadList();
});

$("prev-btn").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - state.limit);
  logger.info("ui", `翻到上一页, offset=${state.offset}`);
  loadList();
});
$("next-btn").addEventListener("click", () => {
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
logger.info("init", "开始初始加载 stats + list");
await Promise.all([loadStats(), loadList()]);
logger.info("init", "初始加载完成", { ...state.apiStats, logs_count: logger.logs.length });

bridge.onContext(() => {
  logger.debug("init", "bridge context 变化（无操作）");
});
