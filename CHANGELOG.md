# 更新日志

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
