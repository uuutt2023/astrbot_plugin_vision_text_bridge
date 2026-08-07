"""main.py - 插件入口：MiniMax CLI 图像理解 OpenAI 兼容接口。

保留能力:
  - MiniMax CLI (mmx vision describe) 图像理解
  - OpenAI 兼容接口注册 (provider_registration)
  - 独立 OpenAI 兼容 server: 模拟 AI 模型接收 /v1/chat/completions，
    提取图片后调用 mmx 图像理解，返回理解内容

已移除:
  - WebUI 缓存管理面板
  - 图像理解缓存 (内存 / SQLite)
  - AstrBot LLM 请求拦截
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

# 插件目录加 sys.path (AstrBot 加载器不自动加)
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

try:
    from astrbot.api import AstrBotConfig
    from astrbot.api import logger as _astr_logger
except Exception:  # logger 可能不存在 — 兜底
    _astr_logger = None
import logging  # noqa: E402

logger = _astr_logger if _astr_logger is not None else logging.getLogger(__name__)
from astrbot.api.star import Context, Star, register  # noqa: E402

from mmx_runner import (  # noqa: E402
    MmxResult,
    build_vision_command,
    run_mmx,
    install_mmx_cli,
    install_mmx_local,
    find_local_mmx,
    diagnose_mmx_error,
    truncate,
    strip_mmx_content,
    preview,
    redact_text,
)

try:
    import main_server  # 独立 OpenAI 兼容 server (bypass framework JWT)
except ImportError:
    main_server = None

try:
    import provider_registration
except ImportError:
    provider_registration = None

from constants import DEFAULT_OPENAI_COMPAT_PORT  # noqa: E402

DEFAULT_VISION_PROMPT = (
    "请客观描述图中可见的元素（主体/场景/文字原文/色调/风格），"
    "严禁猜测游戏/番剧/品牌/角色名，看不出就说'无法确定'。"
)

# data URL 超过此阈值（字节）改用临时文件 --image 传参，规避 OS ARG_MAX 报错
_DATA_URL_CMD_THRESHOLD = 40 * 1024


def _flatten_group_config(config: dict) -> dict:
    """展平嵌套 group config，兼容多种 schema 格式。"""
    if not isinstance(config, dict):
        return config
    flat = dict(config)
    for _key, value in list(config.items()):
        if not isinstance(value, dict):
            continue
        if "items" in value and isinstance(value["items"], dict):
            for ik, iv in value["items"].items():
                flat[ik] = iv
        else:
            is_schema_def = any(
                mk in value
                for mk in ("description", "type", "hint", "default", "obvious_hint")
            )
            if not is_schema_def:
                for ik, iv in value.items():
                    if ik not in flat:
                        flat[ik] = iv
    return flat


def _read_plugin_version() -> str:
    """从 metadata.yaml 读版本号。"""
    try:
        import yaml  # AstrBot 依赖 PyYAML

        meta_path = Path(__file__).resolve().parent / "metadata.yaml"
        with open(meta_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return str(data.get("version", "0.0.0"))
    except Exception:
        return "0.0.0"


def _cfg_int(config, key: str, default: int) -> int:
    v = config.get(key, default)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


PLUGIN_VERSION = _read_plugin_version()


@register(
    "astrbot_plugin_vision_text_bridge",
    "uuutt",
    "MiniMax CLI 图像理解 OpenAI 兼容接口",
    PLUGIN_VERSION,
)
class VisionTextBridgePlugin(Star):
    """MiniMax CLI 图像理解服务：注册 OpenAI 兼容接口，模拟 AI 模型返回理解内容。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = (
            _flatten_group_config(config) if isinstance(config, dict) else config
        )
        self.mmx_path = self._resolve_mmx_path()
        self.npm_path = shutil.which("npm") or shutil.which("npm.cmd")
        self._vision_semaphore: asyncio.Semaphore | None = None
        self._diagnosed: set[str] = set()
        self._openai_compat_port: int | None = None

    # ------------------------------------------------------------------ mmx 路径

    def _resolve_mmx_path(self) -> str:
        mmx_path = (self.config.get("mmx_path") or "").strip()
        if mmx_path:
            return mmx_path
        local = find_local_mmx(str(_PLUGIN_DIR))
        if local:
            logger.info("[vision_text_bridge] 找到 plugin 本地装 mmx: %s", local)
            return local
        return shutil.which("mmx") or shutil.which("mmx.cmd") or ""

    # ------------------------------------------------------------------ lifecycle

    async def initialize(self) -> None:
        if not await self._ensure_mmx_cli():
            logger.warning(
                "[vision_text_bridge] mmx CLI 不可用，图像理解接口将不可用"
            )
            return
        await self._login_mmx_if_configured()
        await self._start_openai_compat_server()
        await self._auto_register_provider()

    async def terminate(self) -> None:
        if main_server is not None:
            try:
                await main_server.stop_solo_server()
            except Exception as e:
                logger.debug("[vision_text_bridge] 停止 server 异常: %s", e)
        logger.info("[vision_text_bridge] 插件已卸载")

    # ------------------------------------------------------------------ mmx 就绪

    async def _ensure_mmx_cli(self) -> bool:
        if self.mmx_path:
            return True
        if not self.config.get("auto_install_cli", True):
            return False
        local_target = str(_PLUGIN_DIR / ".mmx")
        if await install_mmx_local(self.npm_path, local_target):
            self.mmx_path = find_local_mmx(str(_PLUGIN_DIR)) or ""
            if self.mmx_path:
                logger.info(
                    "[vision_text_bridge] mmx-cli 本地装成功: %s", self.mmx_path
                )
                return True
        if await install_mmx_cli(self.npm_path):
            self.mmx_path = shutil.which("mmx") or shutil.which("mmx.cmd") or ""
            if self.mmx_path:
                logger.info(
                    "[vision_text_bridge] mmx-cli 全局装成功: %s", self.mmx_path
                )
                return True
        logger.warning(
            "[vision_text_bridge] 未找到 mmx CLI。请手动执行 npm install -g mmx-cli "
            "或在插件配置中指定 mmx_path。"
        )
        return False

    async def _login_mmx_if_configured(self) -> None:
        if not self.config.get("auto_login", True):
            return
        api_key = (self.config.get("minimax_api_key") or "").strip()
        if not api_key:
            logger.info("[vision_text_bridge] 未配置 minimax_api_key，跳过自动登录")
            return
        if not self.mmx_path:
            return
        masked = (
            f"{api_key[:4]}***REDACTED***(len={len(api_key)})"
            if self.config.get("redact_sensitive", True)
            else api_key
        )
        logger.info("[vision_text_bridge] 预登录 MiniMax CLI: %s", masked)
        try:
            r = await run_mmx(
                self.mmx_path,
                ("auth", "login", "--api-key", api_key),
                timeout=30,
                log_subprocess=self._should_log("mmx_subprocess"),
            )
            if r.ok:
                logger.info(
                    "[vision_text_bridge] 预登录成功: %s",
                    (r.stdout or "").strip() or "(无输出)",
                )
            else:
                logger.warning(
                    "[vision_text_bridge] 预登录失败: rc=%d, stderr=%s",
                    r.returncode,
                    (r.stderr or "").strip()[:200],
                )
        except Exception as e:
            logger.warning("[vision_text_bridge] 预登录异常: %s", e)

    # ------------------------------------------------------ OpenAI 兼容接口

    async def _start_openai_compat_server(self) -> None:
        if main_server is None:
            logger.warning(
                "[vision_text_bridge] main_server 模块未 import，跳过独立 server 启动"
            )
            return
        if not self.config.get("enabled", True):
            logger.info("[vision_text_bridge] OpenAI 兼容接口已关闭")
            return
        port = _cfg_int(self.config, "port", DEFAULT_OPENAI_COMPAT_PORT)
        try:
            actual_port = await main_server.start_solo_server(self, port=port)
        except Exception as e:
            logger.exception(
                "[vision_text_bridge] 启动独立 OpenAI endpoint server 失败: %s", e
            )
            return
        if actual_port is None:
            logger.warning("[vision_text_bridge] main_server.start_solo_server 失败")
        else:
            self._openai_compat_port = actual_port
            logger.info(
                "[vision_text_bridge] 独立 OpenAI 兼容 server 启动: 127.0.0.1:%d",
                actual_port,
            )

    async def _auto_register_provider(self) -> None:
        if not self.config.get("auto_register", True):
            logger.info("[vision_text_bridge] auto_register=False，跳过自动注册")
            return
        if provider_registration is None:
            logger.warning(
                "[vision_text_bridge] provider_registration 模块缺失，跳过注册"
            )
            return
        try:
            asyncio.create_task(self._bg_register_provider())
        except Exception as e:
            logger.exception("[vision_text_bridge] 创建后台注册任务失败: %s", e)

    async def _bg_register_provider(self) -> None:
        await asyncio.sleep(5)
        try:
            ok = await provider_registration.auto_register_provider(self)
        except Exception as e:
            logger.exception("[vision_text_bridge] _bg_register_provider 异常: %s", e)
            return
        if ok:
            logger.info("[vision_text_bridge] OpenAI 兼容 provider 注册成功")
        else:
            logger.warning(
                "[vision_text_bridge] provider 注册失败 — 请检查 openapi_key 或 webui_password 配置"
            )

    # ---------------------------------------------------------- 图像理解核心

    def _ensure_vision_semaphore(self) -> None:
        if self._vision_semaphore is None:
            self._vision_semaphore = asyncio.Semaphore(
                max(1, _cfg_int(self.config, "max_concurrent_vision", 3))
            )

    async def describe_one(self, url: str, vision_prompt: str = "") -> str:
        """对单张图调用 mmx vision describe，返回文字描述。失败返回空串。"""
        url = (url or "").strip()
        if not url:
            return ""
        self._ensure_vision_semaphore()
        timeout = max(5, _cfg_int(self.config, "command_timeout", 60))
        vision_prompt = (
            vision_prompt
            or self.config.get("vision_prompt", "")
            or DEFAULT_VISION_PROMPT
        )

        effective_url = url
        tmp_path = None
        if url.startswith("data:") and len(url) > _DATA_URL_CMD_THRESHOLD:
            tmp_path = self._decode_data_url_to_tempfile(url)
            if tmp_path:
                effective_url = tmp_path

        command = build_vision_command(effective_url, vision_prompt)
        async with self._vision_semaphore:
            result, err = await self._exec_mmx_safely(command, timeout, url)
            if err is not None:
                self._cleanup_tempfile(tmp_path)
                return ""
            if not (result.ok and result.stdout.strip()):
                self._log_mmx_failure(result, url)
                self._cleanup_tempfile(tmp_path)
                return ""
            description = truncate(
                strip_mmx_content(result.stdout, self.config), self.config
            )
            self._cleanup_tempfile(tmp_path)
            logger.info(
                "[vision_text_bridge] mmx 完成: %s, 长度=%d",
                self._smart_url_preview(url),
                len(description),
            )
            return description

    async def describe_images(self, urls, vision_prompt: str = "") -> list[str]:
        """并发理解多张图，返回与 urls 对应的描述列表。"""
        if not urls:
            return []
        results = await asyncio.gather(
            *[self.describe_one(u, vision_prompt=vision_prompt) for u in urls],
            return_exceptions=True,
        )
        return [r if isinstance(r, str) else "" for r in results]

    async def _exec_mmx_safely(self, command, timeout, url):
        """调 mmx 子进程, 把各种异常收拢为 (result, err) 二元返。"""
        try:
            result = await run_mmx(
                self.mmx_path,
                command,
                timeout,
                log_subprocess=self._should_log("mmx_subprocess"),
            )
            return result, None
        except asyncio.TimeoutError:
            logger.warning(
                "[vision_text_bridge] mmx 超时(%ss): %s",
                timeout,
                self._smart_url_preview(url),
            )
            return None, "timeout"
        except Exception as e:
            self._diagnose_mmx_error(str(e), url)
            logger.warning(
                "[vision_text_bridge] mmx 异常: %s, err=%s",
                self._smart_url_preview(url),
                e,
            )
            return None, str(e)

    def _log_mmx_failure(self, result: MmxResult, url: str) -> None:
        err_text = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"exit={result.returncode}"
        )
        self._diagnose_mmx_error(err_text, url)
        logger.warning(
            "[vision_text_bridge] mmx 失败: %s, exit=%d, err=%s",
            self._smart_url_preview(url),
            result.returncode,
            redact_text(err_text[:300]),
        )

    def _diagnose_mmx_error(self, err_text: str, url: str) -> None:
        diagnose_mmx_error(
            err_text, url, lambda u: self._smart_url_preview(u), self._diagnosed
        )

    # ------------------------------------------------------------- 工具函数

    @staticmethod
    def _decode_data_url_to_tempfile(url: str) -> str | None:
        """将 data: URL 解码为临时文件，返回路径。GIF 转 PNG (mmx 不支持 GIF)。"""
        comma = url.find(",")
        if comma <= 0:
            return None
        b64 = url[comma + 1:]
        ext = "png"
        prefix = url[:comma].lower()
        if "jpeg" in prefix or "jpg" in prefix:
            ext = "jpg"
        elif "webp" in prefix:
            ext = "webp"
        elif "gif" in prefix:
            ext = "gif"
        try:
            raw = base64.b64decode(b64)
        except Exception as e:
            logger.warning("[vision_text_bridge] base64 解码失败: %s", e)
            return None
        try:
            if ext == "gif":
                from PIL import Image

                img = Image.open(io.BytesIO(raw))
                if img.mode in ("P", "PA"):
                    img = img.convert("RGBA")
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                raw = buf.getvalue()
                ext = "png"
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
                f.write(raw)
                tmp_path = f.name
            logger.info(
                "[vision_text_bridge] data URL 过大(%dB)→临时文件 %s, 通过 --image 传参",
                len(url),
                tmp_path,
            )
            return tmp_path
        except Exception as e:
            logger.warning("[vision_text_bridge] 临时文件写入失败: %s", e)
            return None

    @staticmethod
    def _cleanup_tempfile(tmp_path: str | None) -> None:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _should_log(self, *flags: str) -> bool:
        if self.config.get("verbose_logging", False):
            return True
        return any(bool(self.config.get(f"verbose_{f}", False)) for f in flags)

    def _smart_url_preview(self, url: str, limit: int = 80) -> str:
        """URL 预览 — data URL 智能截断，普通 URL 退到 preview。"""
        if not url or not isinstance(url, str):
            return ""
        if url.startswith("data:") and "," in url:
            comma = url.find(",")
            prefix = url[:comma]
            b64 = url[comma + 1:]
            if len(b64) > 10:
                return f"{prefix},{b64[:8]}...{{{len(b64)}}}B"
            return f"{prefix},{b64[:8]}"
        return preview(url, limit, self.config)
