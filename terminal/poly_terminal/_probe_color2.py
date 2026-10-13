"""隔离 rich 在「默认 file=sys.stdout 且被重定向」时是否出 ANSI 颜色。"""
import os
import sys

from rich.console import Console
from rich.text import Text

try:
    from importlib.metadata import version
    _rv = version("rich")
except Exception:                                     # noqa: BLE001
    _rv = "?"
print("rich", _rv, "NO_COLOR=", repr(os.environ.get("NO_COLOR")), file=sys.stderr)

# A) 与 app.py 完全相同的 kwargs，但用默认 file（= sys.stdout，此处被重定向）
c1 = Console(width=40, color_system="truecolor", force_terminal=True, no_color=False, highlight=False)
print(f"A) default-file  is_terminal={c1.is_terminal} color_system={c1.color_system} no_color={c1.no_color}",
      file=sys.stderr)
c1.print(Text("HELLO_A", style="bold #00e07a"))

# B) 显式 file=sys.stdout
c2 = Console(file=sys.stdout, width=40, color_system="truecolor", force_terminal=True,
             no_color=False, highlight=False)
print(f"B) file=stdout   is_terminal={c2.is_terminal} color_system={c2.color_system}", file=sys.stderr)
c2.print(Text("HELLO_B", style="bold #00e07a"))

# C) 强制 legacy_windows / 检查 windows 分支
print(f"C) legacy_windows={c1.legacy_windows} is_dumb={c1.is_dumb_terminal}", file=sys.stderr)

sys.stdout.flush()
print("A_ESC=", sys.stdout.isatty(), file=sys.stderr)
