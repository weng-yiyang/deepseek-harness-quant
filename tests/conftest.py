# -*- coding: utf-8 -*-
"""测试公共配置：把仓库根加入 sys.path，便于 import data/ factors/ risk/ 等包。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
