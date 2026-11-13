"""按字符串动态加载类，用于接入自定义摄像头/音频引擎，无需改动框架代码。

支持两种写法：
  - "my_package.my_module:MyClass"      （模块需在 PYTHONPATH 中）
  - "path/to/my_engine.py:MyClass"      （直接指定文件路径）
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from typing import Any


def load_class(spec: str) -> Any:
    if ":" not in spec:
        raise ValueError(f"插件格式应为 'module:Class' 或 'file.py:Class'，收到: {spec!r}")
    target, cls_name = spec.rsplit(":", 1)
    if target.endswith(".py") or os.path.sep in target or "/" in target:
        path = os.path.abspath(target)
        mod_name = "vva_plugin_" + os.path.splitext(os.path.basename(path))[0]
        spec_obj = importlib.util.spec_from_file_location(mod_name, path)
        if spec_obj is None or spec_obj.loader is None:
            raise ImportError(f"无法加载插件文件: {path}")
        module = importlib.util.module_from_spec(spec_obj)
        sys.modules[mod_name] = module
        spec_obj.loader.exec_module(module)
    else:
        module = importlib.import_module(target)
    try:
        return getattr(module, cls_name)
    except AttributeError as e:
        raise ImportError(f"{target} 中没有类 {cls_name}") from e
