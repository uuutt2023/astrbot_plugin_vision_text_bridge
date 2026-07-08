# ARCHITECTURE.md — 仓库文件归类

> v1.1.0+ 统一化版本后的代码组织说明。所有 .py 模块顶部有标准化 docstring（一行职责 + 详细说明 + 作者）。

## 顶层文件

| 文件 | 职责 |
| --- | --- |
| `main.py` | 插件入口 + AstrBot 钩子 (`on_llm_request` priority=100 / `strip_residual_base64` priority=-10000) + 业务核心 + 配置兼容 |
| `metadata.yaml` | AstrBot 元数据（name / version / display_name / desc / repo / 支持的平台） |
| `_conf_schema.json` | 11 个嵌套 group 的插件配置 schema（基础 / MiniMax CLI / 并发 / 图像理解 / 缓存 / 输入处理 / LLM 提示 / 跨插件兼容 / 日志 / 脱敏 / smart_imagechat_hub 兼容） |
| `README.md` | 用户文档（工作流程 / 配置 / 兼容性 / 缓存管理） |
| `CHANGELOG.md` | 完整变更日志（v0.7-v1.1.0） |
| `ARCHITECTURE.md` | 本文件——仓库结构说明 |
| `.gitignore` | 防止误跟踪 __pycache__ / .pyc / .pytest_cache / data/ 等 |
| `pages/cache-manager/` | webui 静态资源（index.html / app.js / style.css / logger.js） |

## 业务核心模块（根目录 .py）

### 图像处理

| 文件 | 职责 |
| --- | --- |
| `image_utils.py` | 通用 image URL helper（`_is_cacheable_url` / `extract_image_url` / `collect_image_urls_from_components` / `is_bot_avatar_url`） |
| `image_meta.py` | 图片元数据提取（尺寸 / 格式） |
| `image_fetch.py` | 图片下载 + 缓存（mmx 子进程读图用） |

### 缓存 + 持久化

| 文件 | 职责 |
| --- | --- |
| `caption_cache.py` | SQLite 描述缓存（WAL 模式，TTL 自动清理） |

### 外部命令

| 文件 | 职责 |
| --- | --- |
| `mmx_runner.py` | mmx CLI 子进程封装（`run_mmx` / `install_mmx_local` / `install_mmx_cli` / `find_local_mmx` / `login_mmx` / `diagnose_mmx_error` / 脱敏） |

### Web API

| 文件 | 职责 |
| --- | --- |
| `web_api.py` | 全部 web API 路由（12 个 handler + `/v1/chat/completions` OpenAI compatible + `/image/caption`） |

### 工具 + 配置

| 文件 | 职责 |
| --- | --- |
| `config_helpers.py` | 嵌套 group config 读 helper（`cfg_int` / `cfg_str` / `cfg_bool` / `cfg_group_*`） |
| `tool_filter.py` | LLM tool_call 过滤（禁用某些工具防止幻觉） |

### 跨插件兼容

| 文件 | 职责 |
| --- | --- |
| `chat_archive_integration.py` | 与 `astrbot_plugin_chat_archive` 协同（缩略图走 chat_archive web_cache） |
| `smart_imagechat_hub_integration.py` | 与 `astrbot_plugin_smart_imagechat_hub` 兼容（OpenAI compatible endpoint + 自动注册 provider） |

## 测试模块（tests/ + test_*.py）

### 共享 stub

| 文件 | 职责 |
| --- | --- |
| `tests/stub_helpers.py` | 沙箱 stub + plugin 构造 helper（`install_stubs` / `make_test_plugin` / `make_test_plugin_with_caption_cache`） |

### 测试用例（10 个 test_*.py）

| 文件 | 职责 | 用例数 |
| --- | --- | --- |
| `test.py` | 插件核心功能 | 176 |
| `test_chat_archive.py` | chat_archive 协同 | 10 |
| `test_auto_install.py` | mmx-cli 自动安装 / 持久化 | 9 |
| `test_smart_imagechat_hub.py` | smart_imagechat_hub 兼容 | 18 |
| `test_schema.py` | schema + flatten_group_config | 15 |
| `test_integration_status.py` | `/cache/integration_status` 端点 | 8 |
| `test_no_hit_count.py` | 完全移除 hit_count 反向验证 | 5 |
| `test_regenerate.py` | `/cache/regenerate` 修复 | 3 |
| `test_theme.py` | 主题切换 | 12 |
| **合计** | | **256** |

## 依赖关系

```
metadata.yaml ─┐
               ├──> main.py (注册 plugin + 读版本)
_conf_schema.json ─> main.py (读 config)
                ↓
                ├─> caption_cache.py (SQLite 缓存)
                ├─> mmx_runner.py (mmx 子进程)
                ├─> web_api.py (web 路由)
                │      ↓
                │      └─> chat_archive_integration.py
                │      └─> smart_imagechat_hub_integration.py
                ├─> config_helpers.py (config 读)
                ├─> tool_filter.py
                ├─> image_utils.py
                ├─> image_meta.py
                └─> image_fetch.py
                ↓
                └─> chat_archive_integration.py
                └─> smart_imagechat_hub_integration.py
```

## 版本号规范

- `metadata.yaml` 唯一权威
- `PLUGIN_VERSION` 在 `main.py` 从 `metadata.yaml` 读，避免硬编码
- webui cache-bust `?v=1.1.0` 与 `metadata.yaml` version 同步
- CHANGELOG 顶部记录每次发布

## 为什么保持根目录平铺

**不**用 `core/` / `web/` / `integration/` 子目录，因为：

1. **AstrBot 框架要求**——插件主入口 `main.py` 必须在插件根目录，import 也按根目录结构
2. **不破坏 git history**——8 个月 + 80 commits 的平铺结构是历史
3. **不破坏测试**——`test_*.py` 大量 `from main import X` / `from web_api import Y`——移动要改 100+ import
4. **本插件规模适中**——21 个 .py 文件，根目录可读

**如果未来真的需要子目录**，可以拆出：
- `core/` (image_utils / config_helpers / tool_filter)
- `web/` (web_api)
- `integration/` (chat_archive_integration / smart_imagechat_hub_integration)

但要同时改 21 个文件的 import + 改测试——大型重构。当前平铺 + ARCHITECTURE.md 描述是性价比最高的方案。

---

最后更新: 2026-07-08
