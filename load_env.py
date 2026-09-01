# -*- coding: utf-8 -*-
"""轻量 .env 加载器（纯标准库，零第三方依赖）。

在 import 时自动加载项目根目录的 `.env` 文件，把 KEY=VALUE 写入 os.environ。
规则：
  - 不覆盖已存在的环境变量（环境变量优先级 > .env）
  - 支持 `#` 注释、空行、`export ` 前缀、单/双引号包裹的值
  - 缺失 .env 时静默跳过（不影响无配置运行）

用法：
  import load_env            # 导入即加载（launcher.py 已接入）
  load_env.load_env()        # 或显式调用，返回本次加载的变量数
"""
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def _find_env_file():
    """返回项目根目录的 .env（优先 .env，其次 .env.local）；无则 None。"""
    for name in (".env", ".env.local"):
        p = _ROOT / name
        if p.exists():
            return p
    return None


def load_env(path=None, override=False) -> int:
    """解析 .env 并写入 os.environ，返回成功加载的变量数。

    - path:     显式指定 .env 文件路径；缺省用项目根目录的 .env
    - override: True 时 .env 覆盖已有环境变量；默认 False（环境变量优先）
    """
    env_file = Path(path) if path else _find_env_file()
    if env_file is None or not env_file.exists():
        return 0
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        return 0

    loaded = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        # 去引号（'x' / "x"）
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


# import 即加载（幂等；不覆盖已存在的环境变量）
_load_count = load_env()


if __name__ == "__main__":
    n = load_env(override=False)
    print(f"已从 .env 加载 {n} 个变量")
    for k in ("LWQUANT_CACHE_DIR", "QUANT_DATA_DIR", "QUANT_FACTORPOOL_DIR",
              "QUANT_PRIVATE_USER", "QUANT_PRIVATE_BRAND", "QUANT_PRIVATE_CODEBASE",
              "DSH_HOME", "LW_TUSHARE_TOKEN"):
        v = os.environ.get(k)
        if v:
            print(f"  {k} = {v}")
