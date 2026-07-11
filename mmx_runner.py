"""mmx_runner.py - mmx CLI 子进程封装 + 安装/诊断/描述。

功能:
  - run_mmx: 调 mmx 子进程 + 解析输出
  - install_mmx_local: 把 mmx-cli 装到 plugin 本地目录 (--prefix)
  - install_mmx_cli: 全局 npm install -g mmx-cli, 返 mmx 绝对路径
  - find_local_mmx: 找 plugin 本地装的 mmx
  - login_mmx: mmx auth login
  - diagnose_mmx_error: 错误诊断
  - redact_text / redact_args: 脱敏

作者: uuutt
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass


def _safe_int(v, default):
    try:
        return int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _safe_str(v, default=""):
    if v is None:
        return default
    return str(v)



from astrbot.api import logger



# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class MmxResult:
    """``mmx`` 子进程调用结果。"""
    stdout: str
    stderr: str
    returncode: int
    ok: bool


# ---------------------------------------------------------------------------
# 跨实例共享: 错误诊断 set
# ---------------------------------------------------------------------------
# 不在 mmx_runner 内部持 set — 调用方 (plugin) 传 ``_diagnosed: set`` 进来,
# 这样测试 ``main.VisionTextBridgePlugin._DIAGNOSED`` 看到的就是 plugin 那个 set。


# ---------------------------------------------------------------------------
# 命令构造
# ---------------------------------------------------------------------------

def build_vision_command(image: str, prompt: str) -> tuple[str, ...]:
    """构造 ``mmx vision describe`` CLI 参数。

    ``image`` 以 ``file-`` 开头 → ``--file-id``; 其它 → ``--image``。
    拼上 ``--prompt <p>`` (可选)。
    """
    if image.startswith("file-"):
        cmd = ["vision", "describe", "--file-id", image]
    else:
        cmd = ["vision", "describe", "--image", image]
    if prompt:
        cmd.extend(["--prompt", prompt])
    return tuple(cmd)


# ---------------------------------------------------------------------------
# 子进程调用
# ---------------------------------------------------------------------------

async def run_mmx(
    mmx_path: str,
    args: tuple[str, ...],
    timeout: float,
    log_subprocess: bool = False,
) -> MmxResult:
    """调 mmx 子进程, 返 ``MmxResult``。

    ``log_subprocess`` 为 True 时把 cmd + stdout/stderr (2000 字符) 全打 INFO log。
    log 走 ``redact_text`` 脱敏, 不需要传 redacted_args (自己 redact)。
    """
    if not mmx_path:
        return MmxResult("", "mmx CLI 未配置或未安装", -1, False)

    if log_subprocess:
        logger.info("[vision_text_bridge] mmx cmd: %s", redact_text(" ".join(args)))

    proc = await asyncio.create_subprocess_exec(
        mmx_path, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        # : 二次超时保护 — wait() 本身可能再 hang (僵尸进程), 2s 兜底
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass
        logger.warning(
            "[vision_text_bridge] mmx 子进程超时(%ss): %s",
            timeout, redact_text(" ".join(args)),
        )
        return MmxResult("", f"mmx timeout after {timeout}s", -1, False)

    stdout_s = stdout.decode("utf-8", errors="replace")
    stderr_s = stderr.decode("utf-8", errors="replace")
    if log_subprocess:
        logger.info(
            "[vision_text_bridge] mmx rc=%d, stdout=%dB, stderr=%dB\n%s\n%s",
            proc.returncode, len(stdout_s), len(stderr_s),
            redact_text(stdout_s[:2000]),
            redact_text(stderr_s[:2000]),
        )
    return MmxResult(stdout_s, stderr_s, proc.returncode, proc.returncode == 0)


# ---------------------------------------------------------------------------
# 预登录 / 安装
# ---------------------------------------------------------------------------

async def login_mmx(mmx_path: str, api_key: str, config: dict) -> None:
    """预登录 mmx, 拉取 / 刷新 session 缓存。失败只警告不影响启动。"""
    if not mmx_path:
        return
    masked = (
        f"{api_key[:4]}***REDACTED***(len={len(api_key)})"
        if config.get("redact_sensitive", True) else api_key
    )
    logger.info("[vision_text_bridge] 预登录 MiniMax CLI: %s", masked)
    try:
        r = await run_mmx(
            mmx_path,
            ("auth", "login", "--api-key", api_key),
            timeout=30,
        )
        if r.ok:
            logger.info(
                "[vision_text_bridge] 预登录成功: %s",
                (r.stdout or "").strip() or "(无输出)",
            )
        else:
            logger.warning(
                "[vision_text_bridge] 预登录失败: rc=%d, stderr=%s",
                r.returncode, (r.stderr or "").strip()[:200],
            )
    except Exception as e:
        logger.warning("[vision_text_bridge] 预登录异常: %s", e)


async def install_mmx_cli(npm_path: str | None) -> bool:
    """通过 npm 全局安装 mmx-cli。失败返 False, 警告不抛。

    Returns:
        True: 安装成功 (或认为已装)
        False: npm 不可用 / 安装失败 / 超时
    """
    if not npm_path:
        logger.warning("[vision_text_bridge] 未找到 npm，无法自动安装 mmx-cli")
        return False
    logger.info("[vision_text_bridge] 开始自动安装 mmx-cli...")
    try:
        proc = await asyncio.create_subprocess_exec(
            npm_path, "install", "-g", "mmx-cli",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("[vision_text_bridge] 自动安装 mmx-cli 超时")
            return False
        if proc.returncode != 0:
            logger.warning(
                "[vision_text_bridge] 自动安装失败: %s",
                stderr.decode("utf-8", errors="replace"),
            )
            return False
        else:
            logger.info("[vision_text_bridge] mmx-cli 安装完成")
            return True
    except Exception:
        logger.exception("[vision_text_bridge] 自动安装 mmx-cli 异常")
        return False


async def install_mmx_local(npm_path: str | None, target_dir: str) -> bool:
    """把 mmx-cli 装到 plugin 本地目录 (--prefix target_dir), 不需 root, 不改 system PATH.

    Returns:
        True: 装成功
        False: npm 不可用 / 装失败
    """
    if not npm_path:
        logger.warning("[vision_text_bridge] 未找到 npm，无法本地装 mmx-cli")
        return False
    from pathlib import Path as _P
    td = _P(target_dir)
    td.mkdir(parents=True, exist_ok=True)
    logger.info("[vision_text_bridge] 装 mmx-cli 到本地目录: %s", td)
    try:
        proc = await asyncio.create_subprocess_exec(
            npm_path, "install", "--prefix", str(td), "mmx-cli",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("[vision_text_bridge] 本地装 mmx-cli 超时")
            return False
        if proc.returncode != 0:
            logger.warning(
                "[vision_text_bridge] 本地装 mmx-cli 失败: %s",
                stderr.decode("utf-8", errors="replace"),
            )
            return False
        logger.info("[vision_text_bridge] mmx-cli 本地装成功: %s", td)
        return True
    except Exception:
        logger.exception("[vision_text_bridge] 本地装 mmx-cli 异常")
        return False


def find_local_mmx(plugin_dir: str) -> str | None:
    """查找 plugin 本地装的 mmx 二进制。多个可能位置。"""
    from pathlib import Path as _P
    pd = _P(plugin_dir)
    # npm 装 --prefix 后的布局
    candidates = [
        pd / ".mmx" / "node_modules" / ".bin" / "mmx",  # Linux
        pd / ".mmx" / "node_modules" / ".bin" / "mmx.cmd",  # Windows
        pd / ".mmx" / "bin" / "mmx",  # 备选
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


# ---------------------------------------------------------------------------
# 错误诊断 (warn-once)
# ---------------------------------------------------------------------------

def diagnose_mmx_error(
    err_text: str,
    url: str,
    preview_url_fn,
    _diagnosed: set[str],
) -> None:
    """mmx 错误首次出现时警告一次。

    ``preview_url_fn`` 脱敏输出 url (走 redact)。
    ``_diagnosed`` plugin 传 ``VisionTextBridgePlugin._DIAGNOSED`` 进来,
    保证测试能 inspect。
    """
    if not err_text:
        return
    lo = err_text.lower()

    if "insufficient balance" in lo or "余额" in err_text or (
        "quota" in lo and ("exceed" in lo or "limit" in lo or "不足" in err_text)
    ):
        _warn_once(_diagnosed, "balance", "[vision_text_bridge] mmx 报 'insufficient balance'。可能：\n"
            "  (1) mmx 路由到不识别该 key 的 endpoint\n"
            "  (2) 该 key 实际属另一环境（staging/test），未在生产 Token Plan 中\n"
            "  (3) mmx CLI 版本过旧、调用已废弃 endpoint\n"
            "  (4) 这个 key 仅开通 text、未开通 vision\n"
            "排查：`mmx --version` / `mmx auth status` / `mmx quota` / "
            "手动 `mmx vision describe --image <本地图>`\n"
            "若 1~3 正常但 4 报错, 几乎确认是 mmx 版本/endpoint 的问题, "
            "请加 `verbose_mmx_subprocess: true` 后重试。")
        return
    if "http 200" in lo or ("http" in lo and "error" in lo and "code" in lo):
        _warn_once(_diagnosed, "http200", "[vision_text_bridge] mmx 返回 HTTP 200 但 body 是 error JSON。\n"
            "通常：mmx CLI 过旧 / key 在该 endpoint 无权限 / key 属另一环境。\n"
            "调试：`mmx --version` / `mmx auth status` / `mmx quota` / "
            "手动 `mmx vision describe --image <本地图>`。")
        return
    if ("unauthenticated" in lo or "unauthorized" in lo
            or ("auth" in lo and ("expired" in lo or "invalid" in lo))
            or "认证失败" in err_text or "未登录" in err_text):
        _warn_once(_diagnosed, "auth", "[vision_text_bridge] mmx 认证失败。检查 minimax_api_key / "
            "`mmx auth status` / 手动 `mmx auth login --api-key <key>`。")
        return
    if ("invalid argument" in lo or "no such file" in lo or "file not found" in lo
            or "model not found" in lo or "unknown model" in lo):
        _warn_once(_diagnosed, "argument", f"[vision_text_bridge] mmx 参数/模型错误。可能：\n"
            f"  (1) 图片路径不可访问：{preview_url_fn(url)}\n"
            f"  (2) mmx 不识别该模型名。手动 `mmx vision describe --image <本地图>` 验证。")
        return
    if "timeout" in lo or "connection" in lo or "network" in lo or "eof" in lo:
        _warn_once(_diagnosed, "network", "[vision_text_bridge] mmx 网络异常。手动 `mmx quota` 验证。")
        return


def _warn_once(_diagnosed: set[str], key: str, message: str) -> None:
    """同进程每个 key 只警告一次 (避免日志被同样错误刷屏)。"""
    if key in _diagnosed:
        return
    _diagnosed.add(key)
    logger.warning(message)


# ---------------------------------------------------------------------------
# 文本处理
# ---------------------------------------------------------------------------

def truncate(text: str, config: dict) -> str:
    """按 ``max_description_length`` (默认 800) 截断, 加省略号。"""
    max_len = _safe_int(config, "max_description_length", 800)
    if max_len <= 0 or len(text) <= max_len:
        return text
    return text[:max_len] + "…"


# 预编译 markdown 清洗正则
_RE_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_RE_MD_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_RE_MD_LIST = re.compile(r"^\s*[\*\-]\s+", re.MULTILINE)
_RE_BLANK_LINES = re.compile(r"\n{3,}")


def strip_mmx_content(stdout: str, config: dict) -> str:
    """: 从 mmx vision describe 的 JSON 拏出 ``content`` 字段, 去 markdown 噪音。

    实测：典型响应 520→380 字符, 省 ~25% token (密集加粗场景能到 40%+)。

    关 ``strip_mmx_markdown`` 返原始 stdout.strip()。
    """
    if not stdout:
        return ""
    if not config.get("strip_mmx_markdown", True):
        return stdout.strip()
    # 1) 拏 content 字段
    try:
        obj = json.loads(stdout)
        text = obj["content"] if isinstance(obj, dict) and isinstance(obj.get("content"), str) else stdout
    except (ValueError, json.JSONDecodeError):
        text = stdout
    if not text:
        return ""
    # 2) 去 markdown 噪音
    text = _RE_MD_BOLD.sub(r"\1", text)
    text = _RE_MD_HEADING.sub("", text)
    text = _RE_MD_LIST.sub("• ", text)  # * / - 列表 → • (中文友好)
    text = _RE_BLANK_LINES.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 日志脱敏
# ---------------------------------------------------------------------------

_SENSITIVE = (
    re.compile(r"(sk-[A-Za-z0-9_-]{8,})"),
    re.compile(r"(?i)(token|signature|x-sign)=[^&\s]+"),
)


def redact_text(text: str) -> str:
    """把 ``sk-xxx`` / ``token=xxx`` / ``signature=xxx`` 脱敏为 ``sk-***REDACTED***``。"""
    if not text:
        return text
    for p in _SENSITIVE:
        text = p.sub(lambda m: m.group(0)[:4] + "***REDACTED***", text)
    return text


def redact_args(args: tuple[str, ...], config: dict) -> tuple[str, ...]:
    """关 ``redact_sensitive`` 时返脱敏版 args (用于 log)。"""
    if not config.get("redact_sensitive", True):
        return args
    return tuple(redact_text(a) for a in args)


def preview(text: str, limit: int, config: dict) -> str:
    """限长预览, 脱敏 + 超长加省略号。"""
    if not text:
        return ""
    s = str(text)
    if config.get("redact_sensitive", True):
        s = redact_text(s)
    return s if len(s) <= limit else s[:limit] + "…"
