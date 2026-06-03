# 更新日志 / Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) 语义化版本规范。
`MAJOR.MINOR.PATCH`：`MAJOR` 大改不兼容；`MINOR` 新增功能；`PATCH` 修复 bug。

## [Unreleased]

无新变更。

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
