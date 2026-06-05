# 更新日志 / Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) 语义化版本规范。
`MAJOR.MINOR.PATCH`：`MAJOR` 大改不兼容；`MINOR` 新增功能；`PATCH` 修复 bug。

## [Unreleased]

无新变更。

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
