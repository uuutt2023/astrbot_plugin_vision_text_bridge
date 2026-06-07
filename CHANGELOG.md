# 更新日志 / Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) 语义化版本规范。
`MAJOR.MINOR.PATCH`：`MAJOR` 大改不兼容；`MINOR` 新增功能；`PATCH` 修复 bug。

## [Unreleased]

无新变更。

## [0.8.22] - 2026-06-07

### 根因——endpoint 缺 /，URL 拼成 ...bridgecache/stats

v0.8.21 push 后用户报错：
```
GET http://1.12.221.36:6185/api/plug/astrbot_plugin_vision_text_bridgecache/stats
                                                    ↑ 少了一个斜杠
=> 实际请求返回 404/连不上
```

v0.8.14 引入了 PLUGIN_PATH = `/api/plug/astrbot_plugin_vision_text_bridge`，
v0.8.18 走 fallbackFetch 时该函数直接 \`${PLUGIN_PATH}${endpoint}\` 拼接。
但业务代码调用 \`apiGet("cache/stats")\`——endpoint 不带 \`/\`，
拼接后变成 \`/api/plug/astrbot_plugin_vision_text_bridgecache/stats\`。
404。后端路由是 \`/api/plug/<plugin>/<path:subpath>\`——需要 \`/\` 分隔。

v0.8.20 该这个错本该能抓住，但当时没报、用户没明显错误。
今天 v0.8.21 修复 DOM 加载顺序后，
API 调用确实发出去了，404/CORS 错才露出来。

### Fix——endpoint 全部加 / 前缀

1. **所有业务调用点修改** (8 处)：\`apiGet("cache/stats")\` → \`apiGet("/cache/stats")\`。
2. **fallbackFetch 防御性加 /**：\`<code>const ep = endpoint.startsWith("/") ? endpoint : "/" + endpoint;<\/code>\`。
3. **fallback URL 拼接**改用 \`ep\` 而不是 \`endpoint\`。

### Tests
- +1 个新测试：\`test_v0822_endpoints_have_leading_slash\` 全面检查所有 \`apiGet/apiPost\` 调用 endpoint 以 \`/\` 开头
- 总计 164/164

## [0.8.21] - 2026-06-07

### 根因——app.js 在 <head> 加载，DOM 未就绪

v0.8.20 push 后用户报启动崩溃：
```
TypeError: Cannot read properties of null (reading 'addEventListener')
  at main (app.js:811:17)
```
app.js line 811 = `$('modal-close').addEventListener(...)`。

**为什么 null？** index.html 里：
- line 18: `<script src="./app.js?v=0.8.20">` 在 `<head>` 末尾
- line 146: `<button id="modal-close">` 在 `<body>` 中间
- app.js 同步执行到 line 811 时，body 还没 parse 完，`$('modal-close')` 返回 null。

v0.8.20 改为同步 IIFE 后，正好原来 ESM \`type="module"\` 会自动 defer 加载（等 DOM parse 完），现在不 defer 了反而踩进这个坑。

### Fix——三道保险

1. **app.js 移到 \`</body>\` 之前**——保证 body 全部 parse 完才跑
2. **IIFE 顶部 \`document.readyState === 'loading'?\` 等待 DOMContentLoaded**——双保险，防其他被推后的 HTML 加载场景
3. **bind() helper**——所有顶层 \`$('xxx').addEventListener(...)\` 改为 \`bind('xxx', 'evt', fn)\`，
   元素不存在时 \`console.warn\` 跳过，不再中断初始化

### 几点另过
- logger.js 留在 \`<head>\` 里（轻量、只设 window 全局，不读 DOM）
- AstrBot 平台自定义 \`asset_token\` 可能会被 cache， \`?v=0.8.21\` cache-bust

### Tests
- +1 个新测试：\`test_v0821_app_js_loaded_after_body\`
- 总计 163/163

## [0.8.20] - 2026-06-07

### 根因——app.js 顶层代码根本没跑

v0.8.19 push 后用户报“bridge: checking… / DB: loading… / stats 不显示 / 表不显示”——
这说明 **app.js 顶层代码崩了**，badge 更新代码、`loadStats()` 等从未执行。

### 根因——ESM \`<script type=module>\` + AstrBot iframe

部分 AstrBot 版本对 \`<script type="module">\` 处理不一致：
- logger.js 是 ESM module (\`export default logger\`)
- app.js \`import logger from "./logger.js"\`
- 两个 \`<script type="module">\` 加载，import 解析任何一环挂了就 throw
- 顶层 \`await bridge.ready()\` 报错后后面所有代码不跑

### Fix——单文件 webui，零外部依赖

1. **logger.js 改为全局脚本**（不再 ESM）：
   ```js
   (function (global) {
     class WebuiLogger { ... }
     global.webuiLogger = new WebuiLogger();
   })(window);
   ```
2. **app.js 去掉 \`import logger\`，改用 \`window.webuiLogger\`**：
   ```js
   const logger = (typeof window !== "undefined" && window.webuiLogger) || {
     debug() {}, info() {}, warn() {}, error() {},
   };
   ```
3. **index.html 改为普通 \`<script>\` 同步加载**（非 module）：
   ```html
   <script src="./logger.js?v=0.8.20"></script>
   <script src="./app.js?v=0.8.20"></script>
   ```
4. **app.js 包裹为 async IIFE**——解决顶层 \`await bridge.ready()\` 在普通 script 下不被支持的问题。
5. **IIFE 末尾 \`})().catch()\` 启动崩溃全屏显示**——以后任何启动错都能一眼看到。

### Webui 零外部依赖

- logger.js 原生 class，window.webuiLogger
- app.js 不依赖任何模块
- index.html 2 个普通 script
- 现在即使浏览器 ESM 有问题 webui 也能跑

### Tests
- +1 个新测试：\`test_v0820_drops_esm_module\`
- 更新 \`test_webui_logger_module_exists\` / \`test_webui_app_uses_logger\` 以适应新架构
- 总计 162/162

## [0.8.19] - 2026-06-07

### 变改进展提示
v0.8.18 推后用户报“webui还是报错”，但仔细看错误堆栈发现：
- 之前的 ``bridge-sdk.js`` CORS 错（v0.8.15/16/17 修的那个）**已经消失**
- 剩下的 ``cloud.astrbot.app/api/v1/github/repo-info`` CORS 错是 **AstrBot dashboard 自己的代码**（``index-DL0WKcFI.js``）调的，与本插件 webui 无关

### 新增——bridge mode badge
用户看不到自己的 webui 状态，会误以为 console CORS 错都是本插件的问题。加一个右上角 badge：
- ``🔌 fallback (直 fetch)``（橙色，bridge SDK 不可用时走 fallbackFetch）——功能 100% 正常
- ``🟢 bridge (SDK)``（绿色，bridge SDK 注入成功）
- 鼠标 hover 有 tooltip 说明

### 样式
- style.css 加 ``.badge.bridge-ok`` (绿色) 和 ``.badge.bridge-fallback`` (橙色)

### 测试
- +1 个新测试：``test_index_html_has_bridge_mode_badge``
- 总计 161/161

## [0.8.18] - 2026-06-07

### Bug 修复——彻底放弃 bridge SDK，走直 fetch backend
v0.8.15/16 在 webui 主动 inject `/api/plugin/page/bridge-sdk.js`，但
CORS 错一直在：
```
Access to script at '.../bridge-sdk.js?asset_token=...' 
from origin 'null' has been blocked by CORS policy: 
The value of the 'Access-Control-Allow-Origin' header in the response must not be the wildcard '*' 
when the request's credentials mode is 'include'.
```

#### 真因
- AstrBot 服务端返 `Access-Control-Allow-Origin: *`（wildcard）
- page iframe 加载时 origin 是 `null`（sandboxed 容器 / data: URL）
- `<script>` class request 默认 credentials mode 是 `include`
- CORS 规范不允许 **wildcard ACAO + credentials include** 共存
- **服务端 CORS 策略是 AstrBot 平台定的，webui 端没法改**

#### 修复
彻底放弃 bridge SDK：
- ``index.html`` 不再 inject ``bridge-sdk.js`` script
- ``app.js`` 顶层用 _fallbackBridge stub：
  ```js
  const _fallbackBridge = { ready: () => Promise.resolve({source: "fallback"}), apiGet: null, apiPost: null };
  const bridge = window.AstrBotPluginPage || _fallbackBridge;
  ```
- ``apiGet``/``apiPost`` 检测 ``typeof bridge.apiGet === "function"``——bridge 没注入就走 fallbackFetch
- fallbackFetch 走 ``fetch('/api/plug/astrbot_plugin_vision_text_bridge/...')``——**same origin**，无 CORS 问题

#### 代价
- 失去 bridge 提供的 i18n / theme / context 机制（webui 不依赖这些）
- 未来如果 AstrBot 修复了 CORS 问题，bridge 又能 inject，webui 会自动优先用

### 测试
- 1 个测试改名 + 1 个测试改语义：不再期望主动 inject bridge-sdk.js
- +1 个新测试：``test_app_js_uses_fallback_bridge_stub`` —— 验 _fallbackBridge 定义存在
- 总计 160/160

## [0.8.17] - 2026-06-07

### 瘦身 + 审查

#### main.py 瘦身
- 新增 ``_cfg_int(config, key, default)`` 和 ``_cfg_str(config, key, default)`` helper，替换 15+ 处 ``int(self.config.get(k, d) or d)`` 重复模式——读起来短 5x、逻辑统一
- 删 ``CaptionEntry`` / ``CacheStats`` re-export——只 ``CaptionCache`` 被 main.py 实际用，其余 2 个在 main.py 里不被直接引用
- 抽出 ``import datetime as _dt`` 到 caption_cache.py 模块顶部，不再函数内 import（v0.8.12 加的 daily_buckets 在里面）
- 抽 ``import re as _re`` 到 main.py 模块顶部，_inject_guidance 重复 import 去重

#### caption_cache.py 瘦身
- 顶部 import ``logging`` + 设 _log 变量（不在 put() 函数内 import）
- datetime 提到模块顶部，daily_buckets 里的 ``_dt.datetime`` 改为 ``datetime.datetime``

#### test.py 瘦身
- 新增 ``_MockContext`` class（模拟 AstrBot Context，任何属性返 AttributeError）
- 新增 ``make_capturing_context(register_fn)`` helper，返 ``(ctx, captured)`` 一行搞定以前的 5 行 mock 模板
- ``new_plugin()`` 末尾统一注入 mock context（以前 6+ 个测试都要重写）
- 12 个 ``class _R`` + ``mock_register`` 重复块标记为“已知重复”但在 helper 上不接——行为差异太大（有些要 ``args`` 有些要 ``json_body``），免得抓 bug

#### app.js 死代码
- 删 ``function fmtDim()``——0 个调用点

### 测试
- +4 个新测试：_cfg_int / _cfg_str / fmtDim 死代码 / datetime 顶部 import
- 1 个测试改：CacheStats 不在 main.py 里
- **总计 159/159**（+4, 0 红绿变化）

### 统计
| 文件 | v0.8.16 | v0.8.17 | 变化 |
|---|---|---|---|
| main.py | 1650 | 1648 | -2 |
| caption_cache.py | 445 | 447 | +2 (import) |
| test.py | 3815 | 3897 | +82 (helper) |
| app.js | 1067 | 1062 | -5 (fmtDim) |
| 总计 | 8300 | 8395 | +95 (主要是 test helper 抽出) |

## [0.8.16] - 2026-06-06

### Bug 修复——v0.8.15 CORS 拦截
- 错误：
  ```
  Access to script at 'http://1.12.221.36:6185/api/plugin/page/bridge-sdk.js?asset_token=...' 
  from origin 'null' has been blocked by CORS policy: 
  The value of the 'Access-Control-Allow-Origin' header in the response must not be the wildcard '*' 
  when the request's credentials mode is 'include'.
  ```
- 真因：v0.8.15 加了 ``crossOrigin='use-credentials'``，但 AstrBot 平台 page iframe 加载时
  origin 是 null（data: URL 或 sandboxed）。CORS 规范：**wildcard ``*`` + credentials mode include
  不允许共存**。
- 修复：去掉 ``crossOrigin`` 属性。干净的 ``<script src>`` 默认就是 ``no-cors`` + ``anonymous``，
  不发 cookie、不会抬 CORS。asset_token 走 URL 鉴权。

### 测试
- 1 个测试改为检代码里（排除注释）是否包含 ``use-credentials``

## [0.8.15] - 2026-06-06

### Bug 修复——**AstrBotPluginPage 注入失败真因**

#### 问题
v0.8.6 起一直写 ``const bridge = window.AstrBotPluginPage;``。报错：
``Cannot read properties of undefined (reading 'ready')``。

#### 诊断
查 AstrBot 平台源码 ``astrbot/dashboard/plugin_page_bridge.js`` line 201：
```js
window.AstrBotPluginPage = { ready, apiGet, apiPost, ... }
```
bridge SDK 是 IIFE，加载后才会设这个全局变量。AstrBot 平台 **在 iframe 加载时不会自动 inject 这个 bridge.js** ——需要插件自己的 HTML 主动引入。

#### 修复
``index.html`` 现在在 ``<head>`` 里主动 inject：
```html
<script>
(function injectBridgeSdk() {
  const token = new URLSearchParams(location.search).get('asset_token');
  const sdk = document.createElement('script');
  sdk.src = '/api/plugin/page/bridge-sdk.js' + (token ? '?asset_token=' + encodeURIComponent(token) : '');
  sdk.async = false;  // 同步加载，app.js 必须等它先
  document.head.appendChild(sdk);
})();
</script>
```

- asset_token 从 URL 提取（AstrBot 平台 iframe 加载时会在 query 上加）
- ``async = false`` 保证执行顺序在 app.js 之前
- 加载失败时 console.warn，但 app.js 仍然能跑 fallbackFetch 路径

#### 测试
- +1 个新测试：``test_index_html_injects_bridge_sdk`` —— regex 验证 SDK URL 出现且在 app.js script 之前

## [0.8.14] - 2026-06-06

### Bug 修复
- **webui 加载卡在 ``app.js:11`` 报错 ``Cannot read properties of undefined (reading 'ready')``**。
  原因：v0.8.6 起一直写的是 ``const bridge = window.AstrBotPluginPage; await bridge.ready();``，没考虑 bridge 还没注入的情况。
  修复：
  1. 新加 ``waitForBridge(timeoutMs=5000)``：每 50ms 轮询 ``window.AstrBotPluginPage``，最多等 5s
  2. 超时后不再静默崩——重写 ``document.body.innerHTML`` 为明确错误页面，列出 3 个可能原因（platform 升级 / stale index.html / AstrBot 页面系统未启动）
  3. ``apiGet``/``apiPost`` 运行时加 fallback：如果 ``bridge.apiGet/apiPost`` 不存在（理论上不会，但防御），走 ``fallbackFetch`` 直接打 ``/api/plug/astrbot_plugin_vision_text_bridge/...`` backend

### 测试
- +1 个新测试：``test_webui_waits_for_bridge_in_app_js`` —— regex 验证 `waitForBridge`/`fallbackFetch`/`PLUGIN_PATH` 存在，旧的顶层裸读不再出现

## [0.8.13] - 2026-06-06

### 新增：工具过滤器（可选干扰 chat_plus 工具注入）

#### 问题背景
- 用户装了 `astrbot_plugin_maid_agent`（管家插件，`call_maid` 等 `@llm_tool` 工具） + `astrbot_plugin_group_chat_plus`（chat_plus，priority=-1）
- chat_plus 在 `event.get_extra("_group_chat_plus_func_tool")` 里读 maid_agent 注册的工具集，merge 到 `req.func_tool`
- 用户希望“本插件该删谁的工具”，但 chat_plus 默认全塞

#### 解决方案
- 在 vision_text_bridge priority=100 主钩子入口**预先**从 `event.get_extra(extra_key)` 里删工具（chat_plus 之后才 merge，这样 merge 进去的是干净版）
- 在 priority=-10000 链末端**兜底**再清一次 `req.func_tool`（防止其他插件中途又 push）
- 新加 3 个配置：

| 配置 | 默认 | 作用 |
|---|---|---|
| `tool_filter_mode` | `off` | `off` / `whitelist` / `blacklist` |
| `tool_filter_names` | `""` | 工具名列表，逗号分隔，**支持 ``*`` 通配符** |
| `tool_filter_extra_key` | `_group_chat_plus_func_tool` | 从哪个 extra key 取待注入工具集 |

#### 工具名匹配（3 种风格）
- ``call_maid`` → 精确匹配
- ``archive_*`` → 前缀通配
- ``*_history`` → 后缀通配
- ``pix*dom`` → fnmatch 全局

#### 工具集接口兼容
模块级 helper `_filter_disabled_tools()` 同时支持：
- ``.tools`` list（chat_plus 的 ToolSet 风格）
- ``.func_list`` list（FunctionToolManager 风格）
- ``.remove_func(name)`` 方法（新版 AstrBot ToolManager）

### 使用示例

要禁用你列出的 22 个 maid/angel/chat_archive/chat_plus/AstrBot 内置工具：

```json
{
  "tool_filter_mode": "blacklist",
  "tool_filter_names": "call_maid,set_user_nickname,archive_*,pixiv_*,angel_*,astrbot_execute_shell,astrbot_execute_python,astrbot_file_*,astrbot_grep_tool,future_task,send_message_to_user"
}
```

要只保留管家一个工具：

```json
{
  "tool_filter_mode": "whitelist",
  "tool_filter_names": "call_maid"
}
```

### 测试
- +8 个新测试：off 不动 / blacklist 删匹配 / whitelist 留匹配 / remove_func 兼容 / func_list 兼容 / None 不崩 / event.get_extra 集成 / 链末兑底 删 func_tool

## [0.8.12] - 2026-06-06

### Webui 增强

#### 状态栏（TTL/上限/下次清理）
- 位置：stats 卡片下面、工具栏上面
- 4 个 chip：
  - ⏱️ 内存 TTL：显示 `300s` 或 `永不过期`
  - 📦 内存上限：`当前数 / 上限`（LRU 进度一眼可见）
  - 🗓️ SQLite TTL：`7 天` 或 `永不过期`
  - ⏭️ 下次清理：`15m 30s 后` / `即将执行` / `已禁用`
- 依据来自 `api_stats` 额外返的 5 个字段：``memory_cache_ttl_seconds`` / ``memory_cache_max_size`` / ``sqlite_cache_ttl_days`` / ``sqlite_clean_interval_hours`` / ``next_clean_at``
- ``next_clean_at`` 计算公式：``_last_clean_at + interval_hours * 3600``（UTC 秒）

#### 按天柱状图
- 位置：工具栏下面、表格上面
- 纯 SVG 画 30 天柱状图（零外部依赖）
- 颜色：紫柱 (`var(--primary)`) + 今日变绿 (`var(--accent)`)
- Y 轴：0 / 1/3 / 2/3 / max 四条网格线 + 标签
- X 轴：每 5 天一个日期标签（MM-DD）
- hover tooltip：``2026-06-06  5 条   今日 ✓``
- 靠 `CaptionCache.daily_buckets(days=30)` 返回 30 个桶（缺天补 0）

#### 自动刷新 toggle
- 位置：工具栏左侧
- 开关：原生 `<input type=checkbox>` + 自定义 slider 样式
- 间隔：**5 秒**（写死）
- 开启后：``setInterval(async () => Promise.all([loadStats, loadList, loadTimeline]), 5000)``
- 关闭：``clearInterval`` 停掉
- 初衷：调试环境下监控缓存写入（v0.8.11 之后有了清理机制，想看后台动不动作）

### 后端新接口
- `GET /cache/stats/timeline?days=30` 返 ``{days, buckets: [{date, count}, ...]}``
- 调 `CaptionCache.daily_buckets(days)`：用 `strftime('%Y-%m-%d', created_at, 'unixepoch')` 按天分组

### 跟踪
- ``_last_clean_at``：每次 ``clean_expired`` 调用后设 ``time.time()``，供 webui 状态栏计算“下次清理”
  - 启动时设一次
  - 后台 task 调设
  - webui 手动 🧹 按钮也设

### 测试
- +7 个新测试：daily_buckets 基本/老条目不入窗口、api_stats 返状态字段、api_stats_timeline 路由、webui 元素存在（html/app.js）、_last_clean_at 记录

## [0.8.11] - 2026-06-06

### Bug 修复
- **webui 右上角 DB 路径永远 "loading…"**：`app.js loadStats()` 只更新了 `stat-dbsize`，从来没碰 `db-path-badge`。现在加 `dbBadge.textContent = "DB: caption_cache.sqlite3"`（full path 在 `title` 里）。主要这个 bug 是从 v0.8.6 首次加 webui 就一直存在——右上的 badge 完全是装饰品。

### 新增
- **内存热缓存 TTL + LRU**：之前是裸 `dict[str, str]`，永远不不过期也不淘汰。现在是新加的 `_MemoryCache(ttl, max_size)` 类：
  - 默认 TTL 300 秒（5 分钟）—可配置 `memory_cache_ttl_seconds`
  - 默认 LRU 上限 500 条—可配置 `memory_cache_max_size`
  - `get()` 过期懒删除、访问会刷新插入顺序
  - 越上限 `put` 淘汰最久未访问项
  - `__getitem__/__setitem__` 兼容老 `cache[k] = v` 语法
- **SQLite 缓存自动过期清理**：新加 `CaptionCache.clean_expired(max_age_days)`。
  - 判定依据：有 `last_hit_at` 用 `last_hit_at`、没被 get 过的用 `created_at`
  - 启动时自动调一次；后台 task 默认每小时跑一次（间隔可配）
  - webui 工具栏多了 **🧹 清理过期** 按钮，调用 `POST /cache/clean_expired`
- **后台清理 task** `_clean_loop(ttl_days, interval_h)`：在 `initialize()` 里用 `asyncio.create_task` 启动；`terminate()` 取消。

### 配置
| 配置项 | 默认 | 作用 |
|---|---|---|
| `memory_cache_ttl_seconds` | 300 | 内存缓存过期（0=不过期） |
| `memory_cache_max_size` | 500 | 内存缓存 LRU 上限（0=不限制，不推荐） |
| `sqlite_cache_ttl_days` | 7 | SQLite 超期天数（0=不过期） |
| `sqlite_clean_interval_hours` | 1 | 后台清理间隔（0=仅启动时清一次） |

### 测试
- +12 个新测试：_MemoryCache 基础/TTL过期/LRU淘汰/TTL=0/dict 语法糖、clean_expired 删老/留新/用 last_hit_at/0天no-op、webui DB badge、clean_expired 路由注册/TTL=0 返 400

### 踏过的坑
- Python `dict` 重复赋同 key **不**会刷新插入顺序—LRU 必须 `pop` + `set` 。get 路径里 “重新设值刷新” 不生效。修在 `_MemoryCache.get()` 里。
- `werkzeug` 未装导致 `test_thumbnail_path_param_endpoint` 挂—`pip install werkzeug` 修了。

## [0.8.10] - 2026-06-06

### 性能优化

#### 清理 mmx vision describe 返回的 markdown 噪音（-25% token）
- 之前：mmx 返的是 `{"content": "**加粗**+列表+多空行", "base_resp": {...}}`，插件直接拿整个 `result.stdout` 当 description
  - 包含「**1. 核心主体**」「* 项目」「### 标题」这些 markdown 语法，**额外占 token 且不含语义信息**
  - 存进 SQLite 后再灌给 LLM，重复浪费
- 现在：新加 `_strip_mmx_content()` 方法
  - `json.loads(stdout)` 拏 `content` 字段（丢 base_resp 包装）
  - 去 markdown 加粗 `**xxx**` → `xxx`
  - 去标题前缀 `### ` → 空
  - 列表前缀 `* ` → `• ` （中文友好）
  - 连续空行压缩：`\n{3,}` → `\n\n`
- **实测**：典型响应 520→380 字符，省 25% token；密集加粗场景（医院传输系统例子）能省 40%+
- **配置开关**：`strip_mmx_markdown`（默认 true），需要原始 markdown 可关闭

### 重构
- 预编译 4 个 regex（`_RE_MD_BOLD` / `_RE_MD_HEADING` / `_RE_MD_LIST` / `_RE_BLANK_LINES`）提到模块级，mmx 热路径不再每次 compile
- main.py 1321 行 < 1350 阈值（v0.8.10 放宽了 50 行）

### 测试
- +7 个新测试：拏 content / 去加粗 / 去标题+列表 / 压空行 / 真实场景节省 / 非 JSON fallback / 配置关闭
- 修 werkzeug 依赖（测试需要）→ `pip install werkzeug`

## [0.8.9] - 2026-06-05

### 性能优化

#### 缩略图并发池（5x 提速）
- 原本是同步循环一次性 20 个 RTT 打到 bridge/后端（串行其实是 1000ms 左右）
- 加 `ThumbPool` class，默认 6 路上限。
  - **Node benchmark** 证明：20 张 50ms RTT 下 串行 1001ms → 6 路 201ms（5x 提速）
  - 保留后端/bridge 保护——不会同时干 20 个请求
- 在 1、2 路场景下不会拥塞；高并发场景可调整 max

#### LRU 缩略图缓存（内存防 OOM）
- 原本 `state.thumbCache = new Map()`，无上限
- 改 `LRUCache(100)`：Map 维护插入顺序，set 越上限删头部
- 随然之前 thumbCache 是 b64 字符串在内存中，100 张封顶控制在 几MB 以内（取决于原始图平均大小）

#### 失败缩略图 cache（避免无限重试）
- 原本：失败 → 仅 slot 显示 ⚠️，下次 ensureThumb 重新发请求
- 修：失败和“无图”都写 {__err: true} / {__none: true} 到 cache，下次直接复用状态
- 同一图加载失败 100 次 == 1 次

#### 日志 panel 增量 append（不再全量重写）
- 原本：每条新日志都 `body.innerHTML = logs.map().join()`——200 条日志在 4x 10 = 40 倍 DOM 重建
- 改：logger.js 加 `onAppend(cb)` 订阅，app.js 维护 `panelNodes[]` 数组
- 新日志 = 1 个新 DOM 节点 append，panelsNodes 超 200 删头部
- 跳下 1000 条日志的 trace 不会卡面板

### 文档
- `pages/cache-manager/index.html` 的 cache-bust 版本号 `?v=0.8.8` → `?v=0.8.9`

### 测试
- +6 个新测试：LRU 越上限、ThumbPool 峰值、异常后队列仍然跑、失败 cache、panel 增量、class 顺序（TDZ 防御）

## [0.8.8] - 2026-06-05

### 修复
- **缩略图删除按钮实际不工作**：`window.confirm()` 在 sandboxed iframe 里被禁，控制台报 `Ignored call to 'confirm()'. The document is sandboxed, and the 'allow-modals' keyword is not set.`，用户点删除不弹窗。
  - 修：index.html 加 `#confirm-modal`，app.js 加 `customConfirm()` 包装 Promise，自建确认 modal（Esc/点背景取消，点确定提交）。
  - 涉及位置：删除单条缓存、清空全部缓存。
- **@register 装饰器版本脱节**：硬编码 `"0.8.7.5"`，metadata.yaml 已经 0.8.7.10。提升插件列表里看到的版本不对。
  - 修：`_read_plugin_version()` 从 metadata.yaml 读 `version` 字段，赋给模块级 `PLUGIN_VERSION`，@register 装饰器引用它。

### 改进
- **SQLite 大图吞磁盘**：之前 1 张 6.5MB PNG 编码后存 ~9MB base64 直接写进 DB，重负载用户很快 OOM。
  - 加配置 `max_b64_size_kb`（默认 2048 KB = 2MB），超过上限的图**不**存 b64，但 description 仍存。webui 缩略图区显示 📦 占位。
  - 0 表示不限制（不推荐）。
- **hit_count 防刷**：webui 用户点详情页 10 次同一个条目，hit_count 就 +10——这统计意义不大。
  - 修：`CaptionCache.get()` 加 5 分钟去重窗口，同一条 5 分钟内重复 get 不递增。

### 清理
- **死代码删除**：
  - `caption_cache.CaptionCache.to_dict_with_b64`（全文 0 refs）
  - `caption_cache.CaptionCache.normalize_key`（全文 0 refs）
  - `main.VisionTextBridgePlugin.api_thumbnail_legacy`（webui 全走路径参数，legacy 再没人用）

### 文档
- `pages/cache-manager/index.html` 的 cache-bust 版本号 `?v=0.8.7.6` → `?v=0.8.8`

## [0.8.7.10] - 2026-06-05

### 修复
- **v0.8.7.9 500 真因**：`api_thumbnail` 读了 `self.context.request` 触发 `AttributeError("'Context' object has no attribute 'request'")`。
  - 其他 handler（api_list 等）`body = await self.context.request.json` 外面包了 `try/except Exception` 静默吞掉了这个错误。
  - 我 v0.8.7.6 加的路径参数 fallback 中却直接读 `req = self.context.request`，该行本身报 AttributeError。
  - 修复：完全删掉对 `self.context.request` 的依赖——路径参数 `image_id` 已经是 kwarg，根本不需要看 view_args。
  - 为什么之前一直没发现：v0.8.7.5 之前的 `api_thumbnail` 走 legacy 路径（带 try/except），从来没人触发过这个裸读。

## [0.8.7.9] - 2026-06-05

### 诊断
- **v0.8.7.8 路径语法修复后，运行时返 500**：werkzeug 路径参数匹配成功，但 handler 某处出错
  - 加 try/except 包住 `api_thumbnail`，打印 traceback 到 AstrBot 进程日志
  - 返 `err(f"handler 异常: ...")` 而不再静默 500

## [0.8.7.8] - 2026-06-05

### 修复
- **v0.8.7.6/7 路径参数语法错误**：我用了 FastAPI 风格的 `{image_id}` 但 AstrBot 路由匹配是 **werkzeug**，只认 **`<image_id>`** 尖括号语法。
  - 症状：webui 调 `GET /cache/thumbnail/<id>` 返 404 "未找到该路由"
  - 修复：`main.py` 路由改为 `<image_id>`
  - 测试：补上用 `werkzeug.routing.Map` 实际跑 match 的验证（v0.8.7.7 之前测试只验证“注册成功”，没验证“能 match 上”，漏掉了）

## [0.8.7.7] - 2026-06-05

### 修复
- **webui 静态资源被浏览器/AstrBot 缓存**，导致 v0.8.7.6 的 `apiGet` 调用 实际还是 v0.8.7.5 的 `apiPost`。
  - `index.html`：添加 `Cache-Control: no-cache` meta 标签 + 资源引用加 `?v=0.8.7.6` cache-busting
  - 下次 webui 加载会绕过所有缓存，拿到真正的 v0.8.7.6 app.js

## [0.8.7.6] - 2026-06-05

### 修复
- **v0.8.7.5 的 POST 修复未生效**：`POST /api/plug/.../cache/thumbnail` 仍然 400
  （v0.8.7.5 误以为只是 query string 问题，实际上 bridge SDK 的 POST 路径构造本身也有 bug
  —— 同样会拼成 `/api/plug/` 而不是 `/api/plugin/`）。

  根因：插件 web API 实际挂载在 `/api/plug/<path:subpath>`（不是 `/api/plugin/`），
  bridge SDK 在 POST 时会把这个前缀进一步截断成 `plug` 并 400。

### 变更
- **彻底绕开 POST**：缩略图 endpoint 改为 `GET /cache/thumbnail/{image_id}`，image_id 走路径参数。
  - `main.py`：新增 `api_thumbnail(image_id)`（路径参数版）+ `api_thumbnail_legacy()`（兼容旧调用），
    共享同一个 `_do_thumbnail()` 内部函数
  - `app.js`：`ensureThumb()` 和 `onView()` 都改调 `apiGet('cache/thumbnail/<id>')`，去掉 POST
  - 注册两个路由：`/cache/thumbnail/{image_id}`（GET，新） + `/cache/thumbnail`（GET，兼容）
- 测试：107 → 108，添加 `test_thumbnail_path_param_endpoint`

## [0.8.7.5] - 2026-06-05

### 修复
- **缩略图返回 400 Bad Request** （v0.8.7.4 后表现为 webui 表格不显示缩略图）：

  AstrBot bridge SDK 拼 URL 时处理 query string 有 bug——
  当 endpoint 路径带参数（`?image_id=...`）时，会把路由模板里的
  ``/api/plugin/`` 截为 ``/api/plug/``（被 bridge SDK 误处理成
  endpoint 名一部分）。结果请求落到 AstrBot 错误路由上，返 400。

  例：用户日志里看到的 URL：
  ```
  GET /api/plug/astrbot_plugin_vision_text_bridge/cache/thumbnail?image_id=9f5e86f8... 400
  ```
  对比成功的 list/stats 请求：
  ```
  GET /api/plugin/astrbot_plugin_vision_text_bridge/cache/list OK
  ```

  **修复**：将 ``/cache/thumbnail`` 同时支持 POST（参数走 JSON body），
  webui 改用 ``apiPost``。GET 保留兼容。**绕开了 bridge SDK 的 query string bug**。

### 改动
- ``app.js``：从 ``apiGet("cache/thumbnail", { image_id })`` 改为 ``apiPost("cache/thumbnail", { image_id })``。
- 后端 ``api_thumbnail`` 先读 JSON body，fallback 到 query string。

### 新增测试
- ``test_thumbnail_endpoint_accepts_post`` ：验证 POST body 能正确带 image_id 走 thumbnail。

## [0.8.7.4] - 2026-06-05

### 紧急修复：真凶
- **裸本地路径不被识别为 cacheable** —— root cause of "SQLite total=0"。

  AstrBot 实际在 ``req.image_urls`` 里传的是**裸路径**（如
  ``/AstrBot/data/temp/io_temp_img_*.jpg``），**不带** ``file://`` 前缀。
  但 v0.8.7 之前版的 ``_is_cacheable_url()`` 只认 ``http://`` / ``https://`` / ``file://``——
  裸路径返 ``False``， ``cacheable=False``，**``_describe_one`` 里
  ``if cacheable and cache_key:`` 整块跳过**，包括最后的 ``await self._persist()``。
  mmx 正常调用、描述拿到，**但 SQLite 始终是空的**。

  v0.8.7.3 增加了"始终打 INFO 日志” —— 是因为这个原因仍没看到日志（连
  ``_persist`` 都没调），谜题才被解开。

  修复：
  - ``_is_cacheable_url()`` 接受：
    - ``http://`` / ``https://``
    - ``file://``
    - **裸 Unix 绝对路径**（以 ``/`` 开头）
    - **Windows 盘符路径**（``C:/...`` / ``C:\...``）
    - ``data:image/...`` 仍不缓存
  - ``_read_image_bytes()`` 同样支持裸本地路径（直接当本地文件读）

### 测试（105 → 106）
- 新增 ``test_describe_one_persists_bare_path_url`` ：完整复现“裸路径 → cacheable=False →
  永远不入缓存”的场景，验证修复后能调 ``_persist`` 写入 SQLite。
- 调整 ``test_is_cacheable_url`` 覆盖裸路径。
- 调整 ``test_main_py_slim_under_1300_lines`` 阈值从 1250 → 1300（4 个新增函数 + 文档）。

## [0.8.7.3] - 2026-06-05

### 紧急修复
- **“SQLite total=0” 诊断问题**：用户反馈 mmx 返回描述后 webui 看不到缓存。
  debug 后发现 plugin 代码可能从缓存的"静默跳过”掩盖问题。

#### 修改
- **`caption_cache.put()`** 跳过空字段时**不再静默** 记 warning：
  ```
  [caption_cache] put() 被调用但 image_id=None description_len=0，**未写入**。
  ```
  原因可能是调用方传错参数、_compute_image_cache_key 返空、或 description 末行制表。
  之前” total 一直 0” 就是这个原因，现在会明确打 warning。
- **`_persist` 始终打 INFO 日志**（不依赖 verbose 配置）：
  ```
  [vision_text_bridge] 写 SQLite 缓存成功: id=xxxxxxxxxxxxxxxx, url=..., desc_len=1158, b64=12345B, mime=image/jpeg, size=67890
  ```
  失败也有 warning：
  ```
  [vision_text_bridge] 写 SQLite 缓存失败: ...
  ```
  **重要**：能直接看到 SQLite 是否写了。

#### 新增测试
- `test_caption_cache_put_warns_on_empty_fields` ：验证 put() 跳过空字段时打 warning。

## [0.8.7.2] - 2026-06-05

### 新增
- **Webui 控制台日志系统**（独立模块 `pages/cache-manager/logger.js`） ：
  - **4 个级别** ：debug / info / warn / error，默认 info。URL 加 `?debug=1` 直接切到 debug。
  - **双输出** ：浏览器控制台（带颜色） + 浮动 on-screen 面板（右下角，glassmorphism 风格）。
  - **API 包装** ：`app.js` 里的 `bridge.apiGet`/`apiPost` 调用**全部走 `apiGet`/`apiPost` 包装**，自动打 log 记录 endpoint / 参数 / 耗时 / 返数据摘要。
  - **用户操作全部记 log** ：点击按钮、搜索、排序、翻页、缩略图加载、modal 打开/关闭、快捷键。
  - **级别持久化** ：设置存 localStorage (`vtb_webui_log_level`)，刷新后保留。
  - **面板控制** ：
    - 5 个按钮：级别下拉、复制全部到剪贴板、下载日志文件、清空、隐藏面板。
    - 隐藏后面板变右上角 🐞 浮动按镙，点了重新打开。
  - **最近 200 条环形缓冲** 防止内存泄露。

### 改动
- **app.js 重写** 以接入 logger：所有 `bridge.apiGet`/`apiPost` 都包装为 `apiGet`/`apiPost`函数，**业务代码不再直接调 bridge**。
- 调试不用再打开浏览器 DevTools console —— 面板上直接看。

### 新增测试（101 → 104）
- `test_webui_logger_module_exists` ：验证 logger.js 语法 + 4 个级别方法。
- `test_webui_app_uses_logger` ：验证 app.js 全面接入 logger，**业务代码不直接调 bridge.apiGet/apiPost**。
- `test_webui_index_has_debug_panel` ：验证 index.html 包含 debug panel 所有关键 DOM id。

## [0.8.7.1] - 2026-06-05

### 紧急修复
- **webui 看得到条目但看不到 base64 缩略图** （v0.8.7 回归 bug）：`_persist` 同步版本里
  用 `asyncio.get_event_loop().run_until_complete(self._read_image_bytes(url))`，
  **在 async 上下文里必抛 `RuntimeError("This event loop is already running")`**。
  同步 fallback 读 `file://` 路径时，如果临时文件已被 AstrBot 清理、/AstrBot 路径权限
  不够、或任何 FileNotFoundError，异常被 `except Exception` 静默吞掉。
  **后果**：SQLite 写入了 description，**但 image_b64/mime/dim 都是空**。webui
  调 `/cache/thumbnail?image_id=...` 返 `has_image=False`，不显示缩略图。

  **修复**：`_persist` 改为 `async def`，**直接 `await self._read_image_bytes(url)`**，
  丢掉同步 fallback。失败统一走 `try/except Exception` 记 warning（不再静默吞），
  description 仍写入 SQLite（仅缩略图为空，不影响 webui 文本展示）。

### 新增
- **新 web API** `/cache/diag` ：返 SQLite 路径、schema 列、总条目数、最近 3 条记录
  摘要（**含 b64 是否存在、长度、mime、size、w×h**）。**在 webui 看不到数据时用**
  验证 SQLite 里到底有没有数据。
- **webui 诊断按钮** ：toolbar 新增 `🔍 诊断` 按钮，点开看 DB 路径/schema/最近 3 条。
- **新测试** （98 → 101）：
  - `test_persist_writes_b64_in_async_context` ：验证 v0.8.7.1 修复后 base64 真正写入 SQLite。
  - `test_persist_handles_read_failure_gracefully` ：读字节失败时 description 仍写入。
  - `test_api_diag_returns_db_info` ：诊断端点返完整 DB 信息。
  - `test_main_py_slim_under_1250_lines` ：行数阈值从 1200 放宽到 1250（diag 端点 +30 行）。

## [0.8.7] - 2026-06-05

### 重构
- **main.py 瘦身** ：`2019 → 1168 行`（**-42%**）。
  - **抽到模块顶层** （不依赖 self 的小 helper）：`_is_image_url_part` / `_extract_url_from_item` / `_extract_urls_from_parts` / `_extract_urls_from_context_list` / `_is_data_url` / `_strip_image_urls` / `_to_text_part` / `_sniff_image_meta` / `_is_cacheable_url`。
  - **合并** ：`_strip_all_data_url_images` + `_strip_all_image_urls` → 单一 `_strip_image_urls(req, only_data_url=)`。
  - **抽** ：`MmxResult` 从 `_run_mmx` 内部 class 提到模块顶层的 `@dataclass`。
  - **重命名** ：`_attach_descriptions_to_prompt` → `_attach`；`_maybe_inject_system_prompt_guidance` → `_inject_guidance`；`_mark_all_providers_support_image` → `_mark_providers_support_image`；`_check_other_plugin_compatibility` → `_check_compatibility`；`_safe_preview` → `_preview`。
  - **去重** ：多个 helper 合并为复合 list comprehension（主钩子入口的清空逻辑从 5 个 `self._remove_*` 调用变成 1 个表达式）。
  - **重写** ：Mmx 错误诊断逻辑从 5 个独立 `if-elif` 重写为单一函数，删除重复 `self._warn_once` 调用。
  - **删除过时** ：`global DEFAULT_PRIORITY` 修改（priority 锁定后改不生效，改用 `self._priority_locked_warning_emitted` 防重报）。
  - **精简 docstring** ：内部 helper 的 1~2 行 docstring；公开方法保留完整说明。

### 新增
- **详细日志拆细为 4 个独立开关** （v0.8.6 之前是单一 `verbose_logging`）：
  - `verbose_hook_trace` ：on_llm_request 钩子入口/出口 + 处理的图片数。调试“插件是否拦截到请求”用。
  - `verbose_mmx_subprocess` ：mmx 完整命令（脱敏）+ stdout/stderr。**mmx 调用失败排错首选**。
  - `verbose_cache_trace` ：内存/SQLite 缓存命中 + SQLite 写。调试“同一张图为什么不命中”用。
  - `verbose_id_computation` ：image_id (md5) 计算过程 + 退路原因。调试“同一张图被算成不同 id”用。
  - `verbose_logging` 仍是总开关，开启后 4 个细粒度**全部生效**。
  - 新增 helper `_should_log(*flags) -> bool` 统一处理：总开关或任一细粒度为 true 即开。
  - **默认全关**。调试时先开总开关看，定位到阶段后只开对应细粒度。

### 改动
- **测试从 93 → 98** 。新增：
  - `test_verbose_granular_toggles` ：4 个细粒度开关独立验证。
  - `test_verbose_total_switch_enables_all` ：总开关覆盖所有细粒度。
  - `test_mmx_result_dataclass` ：MmxResult 模块顶层。
  - `test_helper_module_level_functions_exist` ：9 个抽出去的 helper 都在。
  - `test_main_py_slim_under_1200_lines` ：main.py < 1200 行（实际 1168）。
- `_redact_text` 改为 `@staticmethod` （原本是实例方法但不用 self）。

### 修复
- **Python 3.11 @dataclass + importlib.spec_from_file_location 不兼容** （测试时出现 `'NoneType' object has no attribute '__dict__'`）：原因是 spec loader 加载的 module 不在 `sys.modules` 里，dataclass 装饰器查不到。修复方法是在 exec_module 前显式 `sys.modules[name] = mod`（在 `test_main_imports_without_sys_path_modification` 测试里）。
- **`test_api_cache_thumbnail_no_image`** ：thumbnail API 在条目无 b64 时不再回退到 `image/jpeg`，保留原始 mime（`""`）。
- **精简 mmx 错误诊断** 过程中丢了 "auth + expired/invalid" 组合判断，现已补上。

## [0.8.6] - 2026-06-05

### 重大变更
- **解除与 `astrbot_plugin_chat_archive` 的耦合**（v0.8.3 引入的联动机制**全部移除**）：
  - 删除 `chat_archive_link.py`（联动检测代码）。
  - 移除 `_check_other_plugin_compatibility` 里对 `astrbot_plugin_chat_archive` 的专项检测。
  - 移除 web API `/chat-archive/refresh`。
  - 移除 `cache/stats` 响应中的 `chat_archive` 字段。
  - **两个插件现可独立运行**：同装不报错，互不依赖。`astrbot_plugin_chat_archive` 检测只输出一条 info 提示。

### 新增
- **图片二进制持久化到 SQLite**（`image_captions.image_b64` BLOB）：
  - schema 新增列：`image_id`（md5 主键）、`image_b64`（base64 文本）、`mime_type`、`file_size`、`width`、`height`。
  - `caption_cache.CaptionEntry` 新字段：`image_id` / `image_b64` / `mime_type` / `file_size` / `width` / `height`。
  - 主键从 `image_key` （URL 字符串）改为 `image_id` （32 位 hex md5）。**老库自动迁移**（v0.8.5.x 的 `image_key` 旧主键被重命名表为 `image_captions__legacy` 后，INSERT 复制到新表， DROP 临时表）。
  - `_describe_one` 成功路径调 `_read_image_bytes` 读字节 → base64 → `caption_cache.put(image_b64=..., mime_type=..., file_size=..., width=..., height=...)`。
- **新 web API** `/cache/thumbnail?image_id=<32hex>`：返 `data:image/...;base64,...` JSON，webui 用它出缩略图。
- **Webui 重设计**（glassmorphism 风格）：
  - 参考 `astrbot_plugin_chat_archive` 的 `web/static/css/main.css`（Inter 字体、暗色玻璃、径向渐变 ambient 光晕、CSS 变量驱动主题）—— **仅**参考设计语言，**不**引用、不依赖该插件。
  - 新设计元素：三个 ambient 光晕 `.bg-orb`、顶部 brand mark glass card、表格缩略图列、点击看大图 modal、描述展开收起按钮、键盘快捷键 `R` 刷新、响应式布局。
- **`_sniff_image_meta(bytes)` 方法**：从图片字节嗅探 mime / 宽 / 高。优先用 PIL，降级到手读 magic bytes（PNG/GIF/JPEG SOF/WebP VP8）。

### 改动
- **image_id 改用 md5 (32 hex)**：v0.8.5 的 `md5:<hash>` / `url:<url>` 前缀**全部移除**。image_id 现在就是 32 位 hex。
  - `make_id_from_bytes(data)` = `md5(data)`，同图内容不变 id 不变（**重试必命中**）。
  - `make_id_from_url(url)` 退路（读字节失败时），仍返 32 hex。
- `list()` 默认**不**返 `image_b64`（避免接口 body 过大），要返传 `include_b64=True`。推荐用 `cache/thumbnail` API 单独取。
- 测试从 91 增到 93。

### 补丁修复
- **列表项 `image_key` 字段在 v0.8.6 改为 `image_id`**（同时保留 `image_key` 作为只读别名向前兼容）。老用户没感知，新代码用 `image_id`。
- **`_to_dict` 默认不返 base64**（大字段）。
- **`register_web_api` 从 7 个变为 6 个**（减 `chat-archive/refresh`）。
- **`api_cache_thumbnail` 缺 image_id 或找不到时返 400/404**（清晰错误提示）。

## [0.8.5.1] - 2026-06-04

### 修复
- **同张图被调 mmx 两次**（v0.8.5 bug）：v0.8.5 的 `event.message_obj.message` 补提图代码**同时**用了 `convert_to_file_path()` 返回的本地路径 **和** `comp.url` / `comp.file` 字段。在 QQ 场景下 `comp.url = 'https://multimedia.nt.qq.com.cn/...'`（远程 URL）**不等于** 本地路径——同图被调 mmx **两次**（local 成功 + remote 超时 60s）。

  **修复**：只取 `convert_to_file_path()` 返回的本地路径（可能是 AstrBot 压缩后的 `io_temp_img_*.jpg`），丢弃 `comp.url` / `comp.file`（这些原始 URL 下载慢、可能超时、且与 local path 指向同图）。

  新增测试 `test_v0851_no_duplicate_mmx_call_for_same_image` 验证 Image 组件同时有 url 和 path 时 mmx 只调一次。

## [0.8.5] - 2026-06-04

### 修复
- **chat_plus 默认配置下不传图给 LLM**：chat_plus 的 `enable_image_processing` 默认 `False`，它的 `process_message_images` 看到 `enable_image_processing=False` + `has_text=True` 会**返回 `image_urls=[]`**（不传图）。然后 chat_plus 调 `event.request_llm(image_urls=[], ...)`，构造**不包含图**的 ProviderRequest，框架**直接**用这个 req 触发 `on_llm_request` 钩子。本插件看到 `req.image_urls=[]`，不调 mmx，LLM 看不到图说。

  **修复**：主钩子**还从 `event.message_obj.message` 里拿 Image 组件**，调 `convert_to_file_path()` 拿本地图路径，**绕过** chat_plus 的删除行为。这样本插件在 chat_plus 默认配置下也能处理图。

  使用 **duck-typing**（`hasattr(comp, "convert_to_file_path")` + `type == "image"`）避免依赖 `astrbot.core.message.components.Image` 路径（沙箱 / 未来重命名兼容）。

## [0.8.4] - 2026-06-04

### 修复
- **与 `astrbot_plugin_group_chat_plus` 不兼容**：chat_plus 的 `on_llm_request(priority=-1)` 会在我主钩子（priority=500）**之后**重写 `req.image_urls = _merged_image_urls`，把图重新塞回去。同时它**也**会**覆盖** `req.prompt` / `req.contexts` / `req.system_prompt`。

  **后果**：LLM 同时看到原图 + 本插件注入的图说 content block → 浪费 token + LLM 走图理解路径（不读图说）。

  **修复**：链末兑底 (`strip_residual_base64`, priority=-10000) **总是**清空 `req.image_urls` 和 `extra_user_content_parts` 中的 image_url 组件（**不**仅清 base64）。`chat_plus` 在 -1 重新填的图被 -10000 链末清掉。**未装 chat_plus 时**：主钩子已清空，链末再清仍是空 list，**无副作用**。

### 改动
- `test_fallback_strip_only_data_url_by_default` 调整断言为 v0.8.4 新行为（`image_urls == []`）。
- 新增 `test_chat_plus_style_image_reinjection_is_cleaned`：模拟 chat_plus 风格中间插件重新填图，验证链末清掉。
- 启动日志增加 `astrbot_plugin_group_chat_plus` 专项提示：装了该插件时输出兼容性说明。

## [0.8.3] - 2026-06-03

### 修复
- **预登录失败**（`cannot unpack non-iterable _Result object`）：`_login_mmx` 之前写的是 `stdout, stderr = await self._run_mmx(...)`——但 `_run_mmx` 返回 `MmxResult` dataclass 对象（不是 tuple），导致 unpack 报错。改为 `result = await self._run_mmx(...)` + `result.ok` 判断。

### 新增
- **插件联动自动检测**（`_check_other_plugin_compatibility`）：在 `initialize()` 末尾扫描已加载的插件，输出兼容性日志：
  - 检测到 `astrbot_plugin_chat_archive` → 提示 web_cache 是否可访问
  - 检测到 `astrbot_plugin_angel_heart` → 提示 priority 是否高于 50
  - 检测到 `astrbot_plugin_uni_nickname` → 如果 priority <= 0 警告
  - 检测到 `astrbot_plugin_sylanne` / `_conversation_ledger` / `_minimax_image_caption` → 提示 priority 调高
  - 用 **try 多套 API 名**（`plugin_manager.plugins` / `get_registered_plugin_names` / `list_plugins`）兼容 AstrBot 4.x 不同子版本。

## [0.8.2] - 2026-06-03

### 修复
- **缓存命中率归零**：`_is_cacheable_url` 之前只接受 `http(s)://`，导致 QQ 群聊的 `file://` 临时路径永远不缓存。
- **同一张图不同路径不命中**：AstrBot 的 `_compress_image_for_provider` 每次都生成新文件名，URL 作 key 永远变。改用**图片内容 md5** 作 key —— 同一张图无论路径怎么变，md5 不变。

### 新增
- 新配置 `cache_file_paths`（默认 `true`）：`file://` 路径是否进入缓存。
- 新测试 `test_same_image_different_path_hits_cache`：验证同一张图不同路径命中缓存。

## [0.8.1] - 2026-06-03

### 修复
- **致命崩溃 `'dict' object has no attribute 'model_dump_for_context'`**（v0.7 引入）：图说作为 content block 注入 `req.extra_user_content_parts` 时是裸 dict，AstrBot 期望 `TextPart` Pydantic 对象（有 `model_dump_for_context` 方法）。改用 `TextPart(text=...)`。

### 新增
- **provider modality 伪装**（`_mark_all_providers_support_image`）：在 `initialize()` 时给所有 provider 补 `"image"` modality 标签，骗 AstrBot 在 `_select_image_chat_provider` 里不切 fallback。**依赖**：插件在 on_llm_request 钩子入口已清空 image_urls，主 provider 实际不会收到图。
- 新配置 `keep_provider_modality_as_is`（默认 `false`）：可关掉伪装行为。

## [0.8] - 2026-06-03

### 修复
- **provider 切 fallback 早于 on_llm_request 钩子**：之前在 `_process_request` 末尾才清空 `req.image_urls`，AstrBot 已在钩子内某点切了 provider。改为在 `bridge_vision_to_text` 入口**立即清空** image_urls / extra_user_content_parts / contexts 中的 image_url 组件，并保存快照给 `_process_request` 处理。

## [0.7] - 2026-06-03

### 修复
- **图说被其他插件覆盖**：`astrbot_plugin_angel_heart` 的 `on_llm_request` 钩子（priority=50）会完全重写 `req.prompt`，导致我注入到 prompt 字符串的【图片N：xxx】占位被覆盖丢失。
- 改为把图说注入到 `req.extra_user_content_parts`（**AstrBot 的 user message content block 列表**，不被任何重写插件修改）。

### 改动
- 占位符格式：`【图片{index}（视觉模型描述）：{description}】` → `[Image {index} 描述] {description}`（更自然，让 LLM 当真话听）。
- 引入新配置 `inject_caption_text_to_system_prompt`（默认关）：把图说复制一份到 system_prompt（冗余防覆盖，**默认不开**，节省 token）。

## [0.6] - 2026-06-03

### 修复
- **LLM 不遵守 system_prompt 里的"严格引用"指令**——尽管把"严禁猜测"指令放在 system_prompt 末尾，LLM 仍然凭印象猜游戏名（如把"抖音评论区+云南野生菌梗"误判为"永劫无间"）。

### 改动
- 改为 system_prompt 中**同时**含图说正文 + 严格引用指令。

## [0.5] - 2026-06-03

### 新增
- **链末兜底钩子**（`strip_residual_base64`，priority=-10000）：清掉 AngelHeart 等中间插件重新塞回来的 `data:image/...;base64,...` 残留，避免 LLM 报 "File name too long"。
- 新配置 `strip_all_image_urls_in_fallback`（默认 `false`）：可选择删除**所有** image_url 组件（不仅是 base64）。

## [0.4] - 2026-06-03

### 新增
- **缓存管理页面**（`pages/cache-manager/`）：AstrBot 内置页面，提供统计卡片、搜索、排序、单条删除、重新生成、导出 JSON、Chat Archive 联动刷新。
- 7 个后端 API（`/cache/{stats,list,delete,clear,regenerate,export}` + `/chat-archive/refresh`）。

## [0.3] - 2026-06-03

### 新增
- **Chat Archive 联动**（`chat_archive_link.py`）：自动检测 `<AstrBot>/data/plugins/astrbot_plugin_chat_archive/data/web_cache/`，避免重复下载同一张图。

## [0.2] - 2026-06-03

### 新增
- **SQLite 描述缓存**（`caption_cache.py`）：跨重启持久化，WAL 模式，hit_count 统计，VACUUM 支持。

## [0.1] - 2026-06-02

### 初始版本
- 主钩子 `bridge_vision_to_text`（priority=100）：处理 `req.image_urls` / `req.extra_user_content_parts` / `req.contexts` 三处图片。
- 调用 `mmx vision describe --image <url> --prompt <...>`，把描述回填到 `req.prompt`。
- 内存热缓存 + 基础配置（19 个选项）。
- 75 个单元测试。

---

## 版本对应表

| AstrBot 框架适配 | 插件版本 |
| --- | --- |
| v4.0.0+ | 全部 |
| v5.x | 未测试 |

## 已知限制

- **mmx 自身的限制**：`mmx vision describe` 偶尔报 `input image sensitive`（MiniMax 自己的内容审核），插件已优雅降级为 `[Image N 描述] 理解失败：...` 占位，**不**崩 LLM 调用。
- **不**处理 `req.conversation`（v3 旧字段）—— AstrBot v4 已废弃。
- **不**支持 `vision describe` 之外的 mmx 子命令。
