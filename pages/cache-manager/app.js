/* Vision Text Bridge · Webui Logic
 *
 * v0.8.6 重写：glassmorphism 风格 + 缩略图 + 模态详情
 */

const bridge = window.AstrBotPluginPage;
const ctx = await bridge.ready();

const $ = (id) => document.getElementById(id);

const state = {
  limit: 20,
  offset: 0,
  search: "",
  order_by: "created_at_desc",
  total: 0,
  loading: false,
  thumbCache: new Map(),  // image_id -> { data_url, mime }
};

// ----- utils -----

function showToast(message, type = "success", duration = 2400) {
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
  try {
    const resp = await bridge.apiGet("cache/stats");
    const data = resp?.data || resp;
    $("stat-total").textContent = data.total ?? 0;
    $("stat-hits").textContent = data.total_hits ?? 0;
    $("stat-dbsize").textContent = fmtSize(data.db_size_bytes);
    $("stat-memcache").textContent = data.in_memory_cache_size ?? 0;
  } catch (e) {
    showToast("加载统计失败: " + (e?.message || e), "error");
  }
}

async function loadList() {
  if (state.loading) return;
  state.loading = true;
  try {
    const resp = await bridge.apiGet("cache/list", {
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
  } catch (e) {
    showToast("加载列表失败: " + (e?.message || e), "error");
    renderList([]);
  } finally {
    state.loading = false;
  }
}

function renderList(items) {
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
      const dim = it.width && it.height
        ? `${it.width}×${it.height}`
        : "—";
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
        <button class="btn" data-action="view" data-id="${escapeHtml(it.image_id)}">👁️</button>
        <button class="btn" data-action="regen" data-id="${escapeHtml(it.image_id)}">🔁</button>
        <button class="btn danger" data-action="delete" data-id="${escapeHtml(it.image_id)}">🗑️</button>
      </td>
    </tr>
  `;
    })
    .join("");

  // 懒加载缩略图
  tbody.querySelectorAll(".thumb-slot").forEach((slot) => {
    const id = slot.dataset.id;
    ensureThumb(id, slot);
  });

  // 操作按钮
  tbody.querySelectorAll("button[data-action]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const action = btn.dataset.action;
      const id = btn.dataset.id;
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
      }
    });
  });
}

// ----- thumbnail lazy loading -----

async function ensureThumb(imageId, slot) {
  if (!imageId || !slot) return;
  if (state.thumbCache.has(imageId)) {
    renderThumb(slot, state.thumbCache.get(imageId));
    return;
  }
  try {
    const resp = await bridge.apiGet("cache/thumbnail", { image_id: imageId });
    const data = resp?.data || resp;
    if (data?.has_image && data.data_url) {
      const thumb = { data_url: data.data_url, mime: data.mime_type, w: data.width, h: data.height };
      state.thumbCache.set(imageId, thumb);
      renderThumb(slot, thumb);
    } else {
      // 没有图（老 v0.8.5 迁移过来的条目），显示占位
      slot.innerHTML = `<div class="thumb-placeholder" title="该条目没有图片二进制 (v0.8.5 之前数据)">📦</div>`;
    }
  } catch (e) {
    slot.innerHTML = `<div class="thumb-placeholder" title="缩略图加载失败">⚠️</div>`;
  }
}

function renderThumb(slot, thumb) {
  slot.innerHTML = `<img class="thumb" src="${thumb.data_url}" alt="thumb" loading="lazy" />`;
  const img = slot.querySelector("img");
  if (img) {
    img.addEventListener("click", () => showModalImg(thumb));
  }
}

// ----- modal: 查看详情 -----

async function onView(imageId) {
  try {
    const resp = await bridge.apiGet("cache/thumbnail", { image_id: imageId });
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
    showToast("查看失败: " + (e?.message || e), "error");
  }
}

function showModalImg(thumb) {
  const body = $("modal-body");
  body.innerHTML = `<div class="field">
    <div class="field-label">原图（${thumb.mime || "image"}）</div>
    <img src="${thumb.data_url}" alt="full" />
  </div>`;
  $("detail-modal").hidden = false;
}

$("modal-close").addEventListener("click", () => {
  $("detail-modal").hidden = true;
});

$("detail-modal").addEventListener("click", (e) => {
  if (e.target.id === "detail-modal") $("detail-modal").hidden = true;
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("detail-modal").hidden) {
    $("detail-modal").hidden = true;
  }
});

// ----- actions -----

async function onDelete(id) {
  if (!confirm("删除缓存条目?\n" + id)) return;
  try {
    const resp = await bridge.apiPost("cache/delete", { key: id });
    if (resp?.ok !== false) {
      state.thumbCache.delete(id);
      showToast("已删除");
      await Promise.all([loadStats(), loadList()]);
    } else {
      showToast("删除失败: " + (resp?.error || "未知"), "error");
    }
  } catch (e) {
    showToast("删除失败: " + (e?.message || e), "error");
  }
}

async function onRegenerate(id) {
  showToast("正在重新生成描述...");
  try {
    const resp = await bridge.apiPost("cache/regenerate", { key: id });
    if (resp?.ok !== false) {
      const data = resp?.data || resp;
      if (data.ok) {
        showToast("重新生成成功（长度=" + (data.description?.length || 0) + "）");
      } else {
        showToast("重新生成失败：mmx 调用失败", "error");
      }
      await Promise.all([loadStats(), loadList()]);
    } else {
      showToast("重新生成失败: " + (resp?.error || "未知"), "error");
    }
  } catch (e) {
    showToast("重新生成失败: " + (e?.message || e), "error");
  }
}

async function onClear() {
  if (!confirm("确定要清空所有缓存条目吗？此操作不可撤销。")) return;
  try {
    const resp = await bridge.apiPost("cache/clear", {});
    const data = resp?.data || resp;
    showToast("已清空 " + (data?.cleared ?? 0) + " 条");
    state.thumbCache.clear();
    await Promise.all([loadStats(), loadList()]);
  } catch (e) {
    showToast("清空失败: " + (e?.message || e), "error");
  }
}

async function onExport() {
  try {
    const resp = await bridge.apiGet("cache/export");
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
    showToast("已下载 " + (data.count || 0) + " 条");
  } catch (e) {
    showToast("导出失败: " + (e?.message || e), "error");
  }
}

async function onDiag() {
  // v0.8.7.1 新增: 诊断面板。验证 SQLite 里到底有什么
  try {
    const resp = await bridge.apiGet("cache/diag");
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
    showToast("诊断失败: " + (e?.message || e), "error");
  }
}

// ----- event binding -----

$("refresh-btn").addEventListener("click", async () => {
  state.thumbCache.clear();
  await Promise.all([loadStats(), loadList()]);
});
$("clear-btn").addEventListener("click", onClear);
$("export-btn").addEventListener("click", onExport);
$("diag-btn").addEventListener("click", onDiag);

let searchTimer = null;
$("search-input").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.search = e.target.value.trim();
    state.offset = 0;
    loadList();
  }, 300);
});

$("order-by").addEventListener("change", (e) => {
  state.order_by = e.target.value;
  state.offset = 0;
  loadList();
});

$("prev-btn").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - state.limit);
  loadList();
});
$("next-btn").addEventListener("click", () => {
  if (state.offset + state.limit < state.total) {
    state.offset += state.limit;
    loadList();
  }
});

// 键盘快捷键
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  if (e.key === "r" || e.key === "R") {
    $("refresh-btn").click();
  }
});

// 初始加载
await Promise.all([loadStats(), loadList()]);

bridge.onContext(() => {
  /* no-op */
});
