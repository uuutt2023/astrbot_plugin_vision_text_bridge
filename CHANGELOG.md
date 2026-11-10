# 更新日志

## v2.1.1 (2026-11-10)

### 修复混合安装导致的 API 契约不匹配

用户 11/10 反馈：

```
AttributeError: 'VisionTextBridgePlugin' object has no attribute 'describe_images'
  File "main_server.py", line 254, in _describe_all_images
[OpenAI] Request failed with retryable error; retrying (2/5): Error code: 500 - mmx 描述返回空
... 重试到 5/5
```

#### 根因

仓库有两条分支，**图像理解 API 契约不同**：

| 分支 | `main.py` 暴露 | `main_server.py` 调用 |
|---|---|---|
| `main` | `describe_images(urls, prompt)` / `describe_one(url, prompt)` | `plugin.describe_images(urls, prompt)` |
| `full-version` | `_describe_one(url, source, vision_prompt=...)` | `plugin._describe_one(url, source, vision_prompt=...)` |

插件目录里一旦**混装了不同分支/不同版本的文件**
（`git pull` 半途失败、dashboard 装了旧 zip、只替换了部分文件），
`main_server.py` 调 `describe_images` 而 plugin 实例只有 `_describe_one`
→ `AttributeError` → 返 500 → **framework 对 5xx 重试 5 次**
→ 每次都白等一轮，5 次重试共浪费约 40 秒，最后还是失败。

#### 修复

| # | 改动 | 说明 |
|---|------|------|
| 1 | `main_server.py` 加 **API 契约兼容层** | 运行时探测 `describe_images` / `_describe_images` / `describe_one` / `_describe_one`，逐个降级 |
| 2 | 降级调用用 **`inspect.signature` 判签名** | 不靠 `TypeError` 猜——函数内部抛 `TypeError` 会被误判成签名不匹配，导致重复调用 |
| 3 | 契约完全不匹配 → 返 **400**（不是 500） | AstrBot `request_retry` 对 5xx 必重试 5 次，对 4xx 不重试 → **省掉 5× 无意义重试** |
| 4 | 错误体带结构化 `error.type = api_contract_mismatch` + 修复命令 | 一眼看出是什么问题、怎么修 |
| 5 | 新增 **`GET /diag`** 端点 | 返回 API 契约状态 / plugin 类名 / mmx_path / 修复提示 |
| 6 | 新增 **`/vtcheck`** 命令 | 诊断报告：git 分支 + commit、API 契约、`__pycache__` 陈旧检测、mmx 状态 |
| 7 | `initialize()` 启动自检 | 契约不一致立刻 ERROR 日志，不再等用户发图才炸 |
| 8 | `__pycache__` 检测改 **per-file 比对** | 之前是全局 `min()`，会误报；现在 pyc 比自己的源文件旧才算陈旧 |

#### 验证

```
=== API 契约兼容层 ===
[1] describe_images 版本        → HTTP 200  ✓
[2] 只有 _describe_one (混装)   → HTTP 200  ✓ 降级成功，不再 AttributeError
[3] 契约完全不符                → HTTP 400  ✓ 不触发 framework 5 次重试
[4] _diagnose_contract()        → ✓ 分级正确 (error / 降级警告)
[5] GET /diag                   → ✓

=== 端到端 HTTP (真起 server, urllib 打) ===
main 分支 (describe_images)      → HTTP 200  ✓
full-version (_describe_one)    → HTTP 200  ✓
完全不符                        → HTTP 400  ✓
```

#### 用户侧修复

```bash
cd /AstrBot/data/plugins/astrbot_plugin_vision_text_bridge
rm -rf __pycache__
git fetch origin
git reset --hard origin/main   # 或 origin/full-version, 二选一, 别混
```

装完发 `/vtcheck` 看诊断报告。

## v2.1.0 (2026-10-29)

### 性能优化（profile 驱动）

用 cProfile / timeit / 端到端 benchmark 对启动路径与请求路径做了实测，
下面是**测出来的**数字，不是拍脑袋。

#### 实测基线

| 环节 | 耗时 | 是否瓶颈 |
|---|---|---|
| `base64.b64decode` (1.3MB 图) | 4.5 ms | 否 |
| tempfile 写入 + 删除 | 0.8 ms | 否 |
| `_decode_data_url_to_tempfile` 合计 | 5.7 ms | 否 |
| subprocess spawn 基线 | 3.9 ms | 否 |
| **mmx 单次调用 (网络往返)** | **≈ 11 s** | **是** |
| `_smart_url_preview` (1.7MB data URL) | 0.145 ms | 微小 |

结论：**瓶颈 100% 在 mmx 网络往返**，本地 CPU 开销都可以忽略。
所以优化方向是「少调 mmx」和「并发调 mmx」，而不是抠本地 CPU。

#### 改动

| # | 改动 | 实测收益 |
|---|------|---------|
| 1 | `describe_images` **请求内 URL 去重** — 同一张图出现多次只调一次 mmx，结果按原顺序还原 | 12 张含 6 组重复：**12 次调用 → 2 次**，墙钟 **3.0x** |
| 2 | `max_concurrent_vision` 默认 3 → **5** | 12 张图：**44s → 33s**（按 11s/次换算） |
| 3 | **启动顺序重构** — server + provider 优先，mmx 安装/登录移到后台 | `initialize()` 从「等 npm 装完」变成 **0.000s 返回** |
| 4 | `_smart_url_preview` 不再拷贝整个 base64（只算长度 + 切片前 8 字节） | 该函数 **187x**（0.145ms → 0.0008ms），单次省 0.14ms |

#### 启动顺序为什么重要

旧顺序 `等 mmx → 登录 → 起 server → 注册 provider` 有两个问题：

1. `npm install mmx-cli` 首次可能要几分钟 → AstrBot 启动被卡住
2. **install 失败会 `return` 提前退出** → server 没起、provider 没注册，
   插件等于完全没工作

新顺序 `起 server → 注册 provider → 后台准备 mmx`：

- provider 立刻可注册，AstrBot 不用等 npm
- mmx 没装好也只是单次请求返空，不影响插件加载
- `_bg_prepare_mmx` 失败只记 warning

#### 正确性验证

去重后必须与优化前**逐槽位一致**（顺序、内容、失败隔离）：

```
[1] 顺序/内容一致性  urls=['u1','u2','u1','u3','u2','u1'] → ✓ PASS
[2] 空输入                                    → ✓ PASS
[3] 失败 URL 隔离 (重复的坏 URL 也不互相影响)    → ✓ PASS
[4] _smart_url_preview 输出与优化前逐字节相同    → ✓ PASS
[5] initialize() 不被 mmx 准备阻塞             → ✓ PASS (0.000s)
```

#### 未做的优化（以及为什么）

- **不引入跨请求缓存**：v2.0.0 的设计决策是「精简插件为纯图像理解服务」，
  加回缓存会改变插件定位，且有隐私/磁盘占用顾虑。留给需要时再议。
- **不改 `redact_text`**：86ms 的开销只在 `verbose_mmx_subprocess=true` 时出现，
  默认关闭，不在生产路径上。
- **不提高并发到 12**：会显著提高撞 MiniMax 速率限制的概率，
  默认 5 是「明显变快但仍保守」的值，用户可自行调。

## v2.0.2 (2026-10-29)

### 修复 AstrBot 启动拖延

用户反馈：每次重启 AstrBot，插件要跑 **30 轮 × 30s** 的注册检测，拖延启动时间。

#### 根因

`_bg_register_provider` 旧逻辑是「失败重试 30 轮」：

```
max_attempts = 30 (register_max_attempts 默认值)
interval     = 5s
每轮: sleep(5s) + POST(webui, timeout=15s) + PUT(webui, timeout=15s)
```

Dashboard 尚未就绪时，每轮两个 HTTP 请求都要等满 15s connect timeout
→ 每轮约 35s ≈ 用户说的「30s」
→ 30 轮 ≈ **15 分钟**后台空转。

#### 修复

| 改动 | 说明 |
|---|---|
| `register_max_attempts` 默认 30 → **1** | 只注册一次，不重试 |
| 新增 `register_wait_dashboard_seconds`（默认 30） | 注册前用 **TCP connect 探测**等 Dashboard 端口就绪 |
| `_wait_dashboard_ready()` | 端口没监听 → `ConnectionRefused` 毫秒级返回，**不阻塞 event loop**；在监听 → 立即继续 |
| `openapi_key` 未配置 → 直接跳过 | 连端口都不探，零开销 |
| 新增 `/vtreg` 命令 | 自动注册失败时手动重注册的兜底 |
| httpx 超时拆分 | `connect=3s / read=15s`，Dashboard 没监听时秒失败，不再干等 15s |
| mmx 自动登录移入后台任务 | `mmx auth login` 最多可能挂 30s，不该拖慢启动 |

#### 行为对比

| 场景 | 旧 | 新 |
|---|---|---|
| openapi_key 未配 | 空转 15 分钟 | **0.001s** 跳过 |
| Dashboard 未就绪 | 空转 15 分钟 | 等 ≤30s 就绪窗口后跳过 |
| Dashboard 已就绪 | 5s 后注册成功 | **立即**注册一次 |

### 兼容性

- 目标框架：AstrBot v4.29.3（已核对 `ProviderConfigRequest` / `require_provider_scope` 认证方式无变化）
- `register_max_attempts` 保留为配置项，用户可自行调大（默认 1）
- 未配置 `openapi_key` 的用户行为不变（只是不再空转）

## v2.0.1 (2026-08-07)

### 注册认证精简

- provider 注册认证只保留 OpenAPI Key（`X-API-Key` header），删除 username/password 登录回退
- `_read_webui_credentials` 精简为 `_read_dashboard_port`
- 配置 JSON 重排：按使用顺序 `MiniMax API Key` → `图像理解`（vision_prompt）→ `MiniMax CLI` → `并发` → `OpenAI 兼容接口` → `日志` → `脱敏`
- 删除 `webui_username` / `webui_password` 配置项
- 修复 `_prepare_credentials` 中 config 存在性检查位于访问之后的问题

## v2.0.0 (2026-08-07)

### 精简重构

本版本移除 LLM 拦截、缓存与 WebUI，插件回归纯图像理解服务。

#### 保留

- MiniMax CLI（`mmx vision describe`）图像理解
- OpenAI 兼容接口 `/v1/chat/completions` + `GET /health`
- 通过 webui HTTP API 注册为 AstrBot provider（模拟 AI 模型，接收请求后返回理解内容）
- mmx 自动安装 / 自动登录 / 错误诊断

#### 删除

| 删除内容 | 说明 |
|---|---|
| `pages/`（WebUI） | 缓存管理面板前端 |
| `web_api.py` | 缓存管理 API 与 framework webui 路由 |
| `caption_cache.py` | SQLite + 内存 LRU 图像描述缓存 |
| `image_utils.py` | 拦截用图片提取 helpers |
| `tool_filter.py` | function tools 过滤 |
| `chat_archive_integration.py` | 跨插件缓存协同 |
| `vision_bridge_provider.py` | 自定义 Provider 兜底类（未使用） |
| 配置 | 缓存 / 权限 / 工具过滤 / LLM 提示 / 输入处理等冗余项 |

#### 文件结构

| 文件 | 功能 |
|---|---|
| `main.py` | 插件入口：mmx 就绪 + 接口启动 + provider 注册 + 图像理解 |
| `main_server.py` | 独立 OpenAI 兼容 server（模拟 AI 模型返回理解内容） |
| `mmx_runner.py` | MiniMax CLI subprocess 包装 |
| `provider_registration.py` | 通过 webui HTTP API 注册 provider |
| `constants.py` | 常量（端口、provider id、URL 前缀） |

## v1.1.0 (2026-07-10)

### 新功能

- 智能图片理解拦截（on_llm_request priority=100 + 链尾清理 priority=-10000）
- 描述缓存：内存 LRU + SQLite WAL（md5(image_bytes) 为 key）
- 描述 TTL + 后台清理任务（默认 7 天 TTL，1 小时扫一次）
- OpenAI 兼容 endpoint：`POST /v1/chat/completions`（loopback 127.0.0.1:2023）
- 跨插件兼容检测（图片对话插件等）
- 自动注册 OpenAI 兼容 provider 到 framework（通过 webui HTTP API）
- 权限控制：群白名单 / 用户白名单 / 仅私聊
- 工具调用 2 阶段过滤
- WebUI 管理页（缓存统计、诊断、清理、媒体完整性扫描、调试模式）
- 自动标签生成（auto-tagging）
- 跨插件协同探测

### 重构

#### 文件结构

| 文件 | 功能 |
|---|---|
| `main.py` | 插件入口 + lifecycle + 业务核心 |
| `web_api.py` | webui API endpoints（框架 `/api/plug/<plugin>/*`） |
| `main_server.py` | 独立 OpenAI 兼容 server（`127.0.0.1:<port>`） |
| `provider_registration.py` | 通过 webui HTTP API 注册 provider |
| `vision_bridge_provider.py` | 自定义 Provider class（兜底） |
| `caption_cache.py` | SQLite + 内存 LRU 缓存 |
| `image_utils.py` | 图片读取、元数据、URL 提取（聚合 image_fetch/image_meta） |
| `mmx_runner.py` | MiniMax CLI subprocess 包装 |
| `tool_filter.py` | 工具调用过滤器 |
| `constants.py` | 常量（端口、provider id、URL 前缀） |

#### 重构历史（按 commit 顺序）

| commit | 说明 |
|---|---|
| `8b960f3` | 修 `_patch_user_config_type` 改 entry 模式 + `model_config.model = "vision-bridge"` |
| `01132e6` | user config 删 vision_text_bridge_compat entry + 写 `pm.providers_config[id]` |
| `9eaed51` | 集中 log inline（不依赖 `log_details` kwarg） |
| `aafdc3d` | 修 `log_details` kwarg + author 改名 |
| `014aafd` | 日志清理（不出现其他插件名） + 集中 log |
| `4e996e6` | `VisionBridgeProvider.meta()` + `get_provider_type()` |
| `36becd8` | 真正根因：user config 完整修复（`key=[]` → `key=['placeholder']` + `api_base` 指向我方 endpoint） |
| `30b34a7` | 持久化改写 `cmd_config.json` type + 移除所有测试文件 |
| `e6b3500` | 系统审查 15 项优化（性能/逻辑/质量） |
| `38a0f39` | `dashboard_port` 可配置 + schema 精简 |
| `8cb34e1` | 自定义 LLM provider class（绕开 openai SDK api_key 校验） |
| `9635316` | logger NameError + OpenAI api_key 必填修复（v1.1.0 启动崩） |
| `2fb274c` | 重命名 smart_imagechat_hub 兼容配置 + dashboard 操作步骤 |
| `7fffea9` | 不再调 framework create_provider（避中 pm.provider_class_map 重置） |
| `40039ec` | `meta()` 返 dataclass-like object（避免 dict getattr skip） |
| `0bcc02b` | 改调 framework create_provider API（让 dashboard 实时显示） |
| `beee464` | 不再调 `_register_custom_provider_type`（杀掉 25 个其它 provider instance） |
| `cc462f8` | 解决 `'Token 无效' 401`（独立 server on 127.0.0.1:2023 bypass JWT） |
| `1ccd469` | 精简代码：仅保留 webui HTTP API + 独立 server（−724 行） |
| `e14d779` | `main_server.py` 改用 Python stdlib `asyncio.start_server`（零依赖）+ INFO log + schema 默认 2023 |
| `0622f44` | 通过环境变量 `DASHBOARD_PASSWORD` 配置 webui 密码（**已废弃**，用户改用 plugin schema） |
| `9ef77cf` | 在 `_conf_schema.json` 加 `webui_username` / `webui_password` 字段（用户手动输入） |

### 删的代码

| 删除路径 | 删除原因 |
|---|---|
| `image_meta.py` + `image_fetch.py` | 合并入 `image_utils.py` |
| `config_helpers.py` | main.py 顶部 inline helpers 已覆盖 |
| `_find_cmd_config_file / _sanitize_cmd_config_file / _patch_user_config_type` | 改成 webui HTTP API 注册，不再 mutate cmd_config.json |
| `_register_custom_provider_type` | 改 framework `openai_chat_completion` type，无需 custom type |
| `_cleanup_broken_instances / _add_or_replace_inst / is_provider_already_registered` | 改用 framework create_provider path，由 framework 处理重复 |
| `_build_api_base` 改 main_server | 128.0.0.1:2023 直接硬编码（用户能改 dashboard_port） |

## v1.0.0 (2026-06-XX)

- 首发版本：基础图片拦截 + mmx 视觉理解
