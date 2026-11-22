"""pytest 配置：把 support.py 里的公用 fixture 暴露给所有测试。"""

from support import stubs  # noqa: F401  —— 只是转出口，fixture 本体在 support.py
