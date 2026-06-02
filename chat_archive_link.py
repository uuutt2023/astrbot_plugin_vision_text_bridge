"""
chat_archive_link
=================

检测 [astrbot_plugin_chat_archive](https://github.com/YukiNo420/astrbot_plugin_chat_archive)
是否安装。如果安装，定位它的 ``web_cache`` 目录，复用其已下载的图片。

联动策略：

- Chat Archive 缓存图片文件到 ``<data_dir>/web_cache/<hash>.<ext>``
- 本插件调用 mmx vision describe 时，如果图片 URL 已经在 Chat Archive 的
  web_cache 里有对应文件，**优先用本地路径**调用 mmx（避免重新下载/再次经过
  AstrBot 临时文件链路）。
- Chat Archive 的 web_cache 文件名通常 = URL 的 hash，**我们不直接反查**，
  而是通过 ``get_image_cache_path(url)`` 接口（如果 Chat Archive 暴露）。
  如果不暴露，则**本插件只共用它的 data 目录布局**，按需扩展。
- **描述缓存仍各自存**：图片文件共享、描述内容独立，互不干扰。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from astrbot.api import logger


class ChatArchiveLink:
    """轻量级 Chat Archive 联动探测。"""

    PLUGIN_NAME = "astrbot_plugin_chat_archive"
    CACHE_SUBDIR = "web_cache"

    def __init__(self, plugin_data_dir: str | os.PathLike | None = None):
        # 自身 data dir，由调用方传入（通过 StarTools.get_data_dir）
        self._self_data_dir = (
            Path(plugin_data_dir) if plugin_data_dir else None
        )
        self._chat_archive_data_dir: Path | None = None
        self._chat_archive_web_cache_dir: Path | None = None
        self._available: bool = False
        self._checked: bool = False

    def _detect(self) -> None:
        """执行一次检测。结果缓存到实例属性。"""
        if self._checked:
            return
        self._checked = True

        if self._self_data_dir is None:
            return

        # 推断 Chat Archive 的 data dir：
        # AstrBot 插件 data 目录结构：<AstrBot root>/data/plugins/<plugin>/data/
        # 那么兄弟插件的 data dir 是同级的另一个子目录。
        my_plugins_root = self._self_data_dir.parent.parent  # data_dir -> <plugin> -> plugins
        if not my_plugins_root.exists():
            return

        candidate = my_plugins_root / self.PLUGIN_NAME / "data"
        if not candidate.exists():
            return

        self._chat_archive_data_dir = candidate
        web_cache = candidate / self.CACHE_SUBDIR
        if web_cache.exists() and web_cache.is_dir():
            self._chat_archive_web_cache_dir = web_cache
            self._available = True
            logger.info(
                "[vision_text_bridge] 检测到 astrbot_plugin_chat_archive，"
                "已启用联动: %s",
                web_cache,
            )
        else:
            # data 目录在但 web_cache 还没创建（首次启动）— 仍记为已安装
            self._available = True
            logger.info(
                "[vision_text_bridge] 检测到 astrbot_plugin_chat_archive，"
                "但其 web_cache 目录尚未创建: %s",
                web_cache,
            )

    def refresh(self) -> None:
        """强制重新检测（例如 web_cache 目录刚被创建）。"""
        self._checked = False
        self._available = False
        self._chat_archive_data_dir = None
        self._chat_archive_web_cache_dir = None
        self._detect()

    @property
    def available(self) -> bool:
        if not self._checked:
            self._detect()
        return self._available

    @property
    def web_cache_dir(self) -> Path | None:
        if not self._checked:
            self._detect()
        return self._chat_archive_web_cache_dir

    @property
    def chat_archive_data_dir(self) -> Path | None:
        if not self._checked:
            self._detect()
        return self._chat_archive_data_dir

    def list_cached_files(self) -> list[Path]:
        """列出 web_cache 目录中所有已下载的图片文件。"""
        d = self.web_cache_dir
        if d is None or not d.exists():
            return []
        return [p for p in d.iterdir() if p.is_file()]

    def get_cached_file_by_url(self, url: str) -> Path | None:
        """根据 URL 反查 web_cache 里是否已有本地文件。

        Chat Archive 命名规则（基于其 media_cache.py + 实际日志）:
        - 文件名通常是 hash（md5/sha1），扩展名按 content-type 推断
        - 暂时不做精确反查（不知道 hash 算法），仅按文件名匹配
        """
        # 实际 Chat Archive 缓存命名不是直接 URL 哈希，无法精确反查。
        # 这里只暴露目录路径供调用方做更细的处理。
        return None

    def is_url_likely_cached(self, url: str) -> bool:
        """启发式判断 URL 是否已经被 Chat Archive 缓存。

        没有 Chat Archive 的反向索引 API，只能靠 URL hash + 文件存在性猜测。
        这里直接返回 False（保守），调用方会走自己的下载流程。
        """
        return False
