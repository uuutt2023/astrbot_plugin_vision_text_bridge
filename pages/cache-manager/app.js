/* Vision Text Bridge · 缓存管理页面逻辑 */

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
};

function showToast(message, type = "success") {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast show ${type}`;
  setTimeout(() => {
    el.className = "toast";
  }, 2400);
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

async function loadStats() {
  try {
    const resp = await bridge.apiGet("cache/stats");
    const data = resp?.data || resp;
    $("stat-total").textContent = data.total ?? 0;
    $("stat-hits").textContent = data.total_hits ?? 0;
    $("stat-dbsize").textContent = fmtSize(data.db_size_bytes);
    $("stat-memcache").textContent = data.in_memory_cache_size ?? 0;
    if (data.chat_archive) {
      if (data.chat_archive.available) {
        $("stat-chatarchive").innerHTML =
          "✅ 已联动 — web_cache: <code>" +
          escapeHtml(data.chat_archive.web_cache_dir || "(unknown)") +
          "</code>";
      } else {
        $("stat-chatarchive").innerHTML =
          "❌ 未检测到 astrbot_plugin_chat_archive。本插件不依赖它，但启用可共享图片文件缓存。";
      }
    }
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
    tbody.innerHTML = `<tr class="empty"><td colspan="7">${
      state.search ? "没有匹配项" : "暂无缓存"
    }</td></tr>`;
    return;
  }
  tbody.innerHTML = items
    .map(
      (it, i) => `
    <tr data-key="${escapeHtml(it.image_key)}">
      <td class="muted">${state.offset + i + 1}</td>
      <td>
        <div class="url-cell" title="${escapeHtml(it.image_url)}">
          ${escapeHtml(truncate(it.image_url, 200))}
        </div>
      </td>
      <td>
        <div class="desc-cell">${escapeHtml(truncate(it.description, 280))}</div>
      </td>
      <td class="muted">${it.hit_count}</td>
      <td class="muted">${fmtTime(it.created_at)}</td>
      <td class="muted">${it.last_hit_at ? fmtTime(it.last_hit_at) : "-"}</td>
      <td>
        <div class="row-actions">
          <button class="btn" data-action="regen" data-key="${escapeHtml(
            it.image_key
          )}">🔁 重新生成</button>
          <button class="btn danger" data-action="delete" data-key="${escapeHtml(
            it.image_key
          )}">🗑️ 删除</button>
        </div>
      </td>
    </tr>
  `
    )
    .join("");

  tbody.querySelectorAll("button[data-action]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const action = btn.dataset.action;
      const key = btn.dataset.key;
      if (action === "delete") onDelete(key);
      else if (action === "regen") onRegenerate(key);
    });
  });
}

function truncate(s, n) {
  if (!s) return "";
  return s.length > n ? s.slice(0, n) + "…" : s;
}

async function onDelete(key) {
  if (!confirm(`删除缓存条目?\n${truncate(key, 120)}`)) return;
  try {
    const resp = await bridge.apiPost("cache/delete", { key });
    if (resp?.ok !== false) {
      showToast("已删除");
      await Promise.all([loadStats(), loadList()]);
    } else {
      showToast("删除失败: " + (resp?.error || "未知"), "error");
    }
  } catch (e) {
    showToast("删除失败: " + (e?.message || e), "error");
  }
}

async function onRegenerate(key) {
  showToast("正在重新生成描述...");
  try {
    const resp = await bridge.apiPost("cache/regenerate", { key });
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

async function onChatArchiveRefresh() {
  try {
    const resp = await bridge.apiPost("chat-archive/refresh", {});
    const data = resp?.data || resp;
    if (data?.available) {
      showToast("Chat Archive 联动已启用");
    } else {
      showToast("未检测到 Chat Archive 插件");
    }
    await loadStats();
  } catch (e) {
    showToast("刷新失败: " + (e?.message || e), "error");
  }
}

// 事件绑定
$("refresh-btn").addEventListener("click", async () => {
  await Promise.all([loadStats(), loadList()]);
});
$("clear-btn").addEventListener("click", onClear);
$("export-btn").addEventListener("click", onExport);
$("chatarchive-refresh-btn").addEventListener("click", onChatArchiveRefresh);

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

// 初始加载
await Promise.all([loadStats(), loadList()]);

// 监听上下文变化（语言切换等），目前无需重新渲染
bridge.onContext(() => {
  /* no-op */
});
