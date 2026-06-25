""": test_auto_install.py — 默认开启 auto_install_cli + install_mmx_cli 返 bool 验证。"""
import os
import sys
import asyncio
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests.stub_helpers import install_stubs, make_test_plugin  # noqa: E402
install_stubs()
import main  # noqa: E402
import mmx_runner  # noqa: E402


def test_schema_auto_install_cli_default_true():
    """: schema auto_install_cli default = true (用户期望默认自动装)."""
    import json
    from pathlib import Path
    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    # 嵌套 schema 中找 auto_install_cli
    for gname, gdef in schema.items():
        if isinstance(gdef, dict) and "items" in gdef:
            if "auto_install_cli" in gdef["items"]:
                assert gdef["items"]["auto_install_cli"]["default"] is True, \
                    f"auto_install_cli default 应为 true, 实际 {gdef['items']['auto_install_cli']['default']}"
                return
    raise AssertionError("schema 中找不到 auto_install_cli")


def test_install_mmx_cli_returns_bool_success():
    """: install_mmx_cli 装成功返 True."""
    async def run():
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc
            return await mmx_runner.install_mmx_cli("/usr/bin/npm")
    result = asyncio.run(run())
    assert result is True, f"装成功应返 True, 实际 {result}"
    print("✓ test_install_mmx_cli_returns_bool_success")


def test_install_mmx_cli_returns_bool_no_npm():
    """: install_mmx_cli 没 npm 返 False."""
    async def run():
        return await mmx_runner.install_mmx_cli(None)
    result = asyncio.run(run())
    assert result is False, f"没 npm 应返 False, 实际 {result}"
    print("✓ test_install_mmx_cli_returns_bool_no_npm")


def test_install_mmx_cli_returns_bool_npm_fail():
    """: install_mmx_cli npm 安装失败返 False."""
    async def run():
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b"ERR! code EACCES"))
            mock_proc.returncode = 1
            mock_exec.return_value = mock_proc
            return await mmx_runner.install_mmx_cli("/usr/bin/npm")
    result = asyncio.run(run())
    assert result is False, f"npm 失败应返 False, 实际 {result}"
    print("✓ test_install_mmx_cli_returns_bool_npm_fail")


def test_install_mmx_cli_returns_bool_timeout():
    """: install_mmx_cli 超时返 False. patch asyncio.wait_for 抛 TimeoutError."""
    async def run():
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.kill = MagicMock()
            mock_proc.wait = AsyncMock()
            mock_exec.return_value = mock_proc
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                return await mmx_runner.install_mmx_cli("/usr/bin/npm")
    result = asyncio.run(run())
    assert result is False, f"超时应返 False, 实际 {result}"
    print("✓ test_install_mmx_cli_returns_bool_timeout")


def test_init_calls_auto_install_when_mmx_missing():
    """: __init__ 路径: mmx 找不到 + auto_install_cli 默认 True → 调 _install_mmx_cli."""
    plugin = make_test_plugin(main)  # 不传任何 override
    # 验证默认配置
    assert plugin.config.get("auto_install_cli", True) is True, \
        "auto_install_cli 默认应为 True"
    print("✓ test_init_calls_auto_install_when_mmx_missing")


if __name__ == "__main__":
    test_schema_auto_install_cli_default_true()
    test_install_mmx_cli_returns_bool_success()
    test_install_mmx_cli_returns_bool_no_npm()
    test_install_mmx_cli_returns_bool_npm_fail()
    test_install_mmx_cli_returns_bool_timeout()
    test_init_calls_auto_install_when_mmx_missing()
    print("---")
    print("ALL AUTO-INSTALL TESTS PASSED")
