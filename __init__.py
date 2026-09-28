"""MaiBot 翻唱语音插件。"""

from __future__ import annotations

import json
from pathlib import Path

_MANIFEST_PATH = Path(__file__).with_name("_manifest.json")

# 版本号以 _manifest.json 为唯一来源，避免代码与清单两处对不上
try:
    with open(_MANIFEST_PATH, encoding="utf-8") as _f:
        __version__ = str(json.load(_f)["version"])
except Exception:  # 清单缺失或损坏时退回内置版本
    __version__ = "1.1.0"
