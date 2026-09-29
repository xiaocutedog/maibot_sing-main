"""插件版本号。

MaiBot 宿主把 ``plugin.py`` 当合成包加载（``spec_from_file_location`` +
``submodule_search_locations=插件目录``），``__init__.py`` 不会被执行，
所以版本号不能放在 ``__init__.py`` 里让 ``plugin.py`` 用 ``from . import`` 取——
那个相对导入会去找包属性 ``__version__``，必然失败。
本模块作为包内子模块被导入，宿主加载与常规包导入两种方式都成立。
"""

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

__all__ = ["__version__"]
