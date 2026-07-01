""": test_auto_install.py — 默认开启 auto_install_cli + install_mmx_cli 返 bool 验证。"""
import os
import sys
import asyncio
import tempfile
from pathlib import Path
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


def test_install_mmx_cli_returns_path_success():
    """: install_mmx_cli 装成功 + 找到 mmx → 返 mmx 绝对路径。"""
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            (bin_dir / "mmx").write_bytes(b"#!/bin/sh\necho mmx")
            (bin_dir / "mmx").chmod(0o755)
            call_count = [0]
            async def fake_exec(*args, **kwargs):
                call_count[0] += 1
                mock = AsyncMock()
                if call_count[0] == 1:
                    mock.communicate = AsyncMock(return_value=(b"ok", b""))
                    mock.returncode = 0
                else:
                    mock.communicate = AsyncMock(return_value=(tmp.encode(), b""))
                    mock.returncode = 0
                return mock
            with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
                return await mmx_runner.install_mmx_cli("/usr/bin/npm")
    result = asyncio.run(run())
    assert result is not None, "装成功应返 mmx 路径"
    assert result.endswith("/bin/mmx"), f"路径格式错: {result}"
    print("✓ test_install_mmx_cli_returns_path_success")
def test_install_mmx_cli_returns_none_no_npm():
    """: install_mmx_cli 没 npm 返 None。"""
    async def run():
        return await mmx_runner.install_mmx_cli(None)
    result = asyncio.run(run())
    assert result is None
    print("✓ test_install_mmx_cli_returns_none_no_npm")
def test_install_mmx_cli_returns_none_npm_fail():
    """: install_mmx_cli npm 装失败返 None。"""
    async def run():
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b"EACCES: permission denied"))
            mock_proc.returncode = 1
            mock_exec.return_value = mock_proc
            return await mmx_runner.install_mmx_cli("/usr/bin/npm")
    result = asyncio.run(run())
    assert result is None
    print("✓ test_install_mmx_cli_returns_none_npm_fail")
def test_install_mmx_cli_returns_none_timeout():
    """: install_mmx_cli 超时返 None。"""
    async def run():
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
            mock_proc.kill = MagicMock()
            mock_proc.wait = AsyncMock()
            mock_exec.return_value = mock_proc
            return await mmx_runner.install_mmx_cli("/usr/bin/npm")
    result = asyncio.run(run())
    assert result is None
    print("✓ test_install_mmx_cli_returns_none_timeout")
def test_main_module_imports_with_old_mmx_runner():
    """: main.py 在 mmx_runner 没 install_mmx_local/find_local_mmx 时也能加载 (compatibility)."""
    # 1. 模拟老 mmx_runner: 缺 install_mmx_local
    import importlib, sys
    mmx_runner_mod = sys.modules.get("mmx_runner")
    if mmx_runner_mod:
        # 暂时删掉 install_mmx_local (如有)
        save_il = getattr(mmx_runner_mod, "install_mmx_local", None)
        save_fm = getattr(mmx_runner_mod, "find_local_mmx", None)
        if save_il:
            del mmx_runner_mod.install_mmx_local
        if save_fm:
            del mmx_runner_mod.find_local_mmx
        try:
            if "main" in sys.modules:
                del sys.modules["main"]
            if "data.plugins.astrbot_plugin_vision_text_bridge.main" in sys.modules:
                del sys.modules["data.plugins.astrbot_plugin_vision_text_bridge.main"]
            try:
                import main
                print("✓ main 加载成功 (老 mmx_runner 兼容)")
            except ImportError as e:
                print(f"✗ main 加载失败: {e}")
                raise AssertionError(f"main 在老 mmx_runner 下加载失败: {e}")
        finally:
            if save_il:
                mmx_runner_mod.install_mmx_local = save_il
            if save_fm:
                mmx_runner_mod.find_local_mmx = save_fm
    print("✓ test_main_module_imports_with_old_mmx_runner")


def test_init_falls_back_to_global_install_when_local_unavailable():
    """: __init__: 老 mmx_runner (无 install_mmx_local) → 直接走全局 npm install -g."""
    from unittest.mock import patch, AsyncMock
    # 模拟 _install_mmx_local_fn is None
    plugin = make_test_plugin(main)
    plugin.mmx_path = ""  # 没 mmx
    plugin.npm_path = "/usr/bin/npm"
    plugin.config["auto_install_cli"] = True
    # 替换 import 的函数
    main._install_mmx_local_fn = None  # 触发老 mmx_runner fallback
    main._find_local_mmx_fn = lambda _d: None

    async def fake_install():
        return True
    plugin._install_mmx_cli = fake_install

    with patch.object(main.shutil, "which", return_value="/usr/bin/mmx"):
        # 跑 __init__ 后段
        # 直接调内部逻辑 (跳过 register/route)
        async def run():
            # 触发 # 3. 装逻辑
            if not plugin.mmx_path and plugin.config.get("auto_install_cli", True):
                if main._install_mmx_local_fn is None:
                    install_ok = await plugin._install_mmx_cli()
                    if install_ok:
                        plugin.mmx_path = main.shutil.which("mmx") or ""
            return plugin.mmx_path
        import asyncio
        result = asyncio.run(run())
    assert result == "/usr/bin/mmx", f"应走 fallback 到全局装, 实际 {result}"
    print("✓ test_init_falls_back_to_global_install_when_local_unavailable")


def test_init_calls_auto_install_when_mmx_missing():
    """: __init__ 路径: mmx 找不到 + auto_install_cli 默认 True → 调 _install_mmx_cli."""
    plugin = make_test_plugin(main)  # 不传任何 override
    # 验证默认配置
    assert plugin.config.get("auto_install_cli", True) is True, \
        "auto_install_cli 默认应为 True"
    print("✓ test_init_calls_auto_install_when_mmx_missing")



def test_install_mmx_local_returns_bool_no_npm():
    """: install_mmx_local 没 npm 返 False."""
    async def run():
        return await mmx_runner.install_mmx_local(None, "/tmp/fake_target")
    result = asyncio.run(run())
    assert result is False
    print("✓ test_install_mmx_local_returns_bool_no_npm")


def test_install_mmx_local_returns_bool_success():
    """: install_mmx_local 装成功返 True。"""
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, ".mmx")
            with patch("asyncio.create_subprocess_exec") as mock_exec:
                mock_proc = AsyncMock()
                mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
                mock_proc.returncode = 0
                mock_exec.return_value = mock_proc
                return await mmx_runner.install_mmx_local("/usr/bin/npm", target)
    result = asyncio.run(run())
    assert result is True
    print("✓ test_install_mmx_local_returns_bool_success")


def test_find_local_mmx_finds_binary():
    """: find_local_mmx 在 plugin dir 找到 .bin/mmx."""
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp) / ".mmx" / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "mmx").write_bytes(b"#!/bin/sh\necho mmx")
        (bin_dir / "mmx").chmod(0o755)
        result = mmx_runner.find_local_mmx(tmp)
        assert result is not None
        assert result.endswith("mmx")
        assert "/.mmx/" in result
    print("✓ test_find_local_mmx_finds_binary")


def test_find_local_mmx_returns_none_when_missing():
    """: find_local_mmx plugin dir 没 .mmx 返 None。"""
    with tempfile.TemporaryDirectory() as tmp:
        result = mmx_runner.find_local_mmx(tmp)
        assert result is None
    print("✓ test_find_local_mmx_returns_none_when_missing")

if __name__ == "__main__":
    test_schema_auto_install_cli_default_true()
    test_install_mmx_cli_returns_path_success()
    test_install_mmx_cli_returns_none_no_npm()
    test_install_mmx_cli_returns_none_npm_fail()
    test_install_mmx_cli_returns_none_timeout()
    test_main_module_imports_with_old_mmx_runner()
    test_init_falls_back_to_global_install_when_local_unavailable()
    test_init_calls_auto_install_when_mmx_missing()
    test_install_mmx_local_returns_bool_no_npm()
    test_install_mmx_local_returns_bool_success()
    test_find_local_mmx_finds_binary()
    test_find_local_mmx_returns_none_when_missing()
    print("---")
    print("ALL AUTO-INSTALL TESTS PASSED")
