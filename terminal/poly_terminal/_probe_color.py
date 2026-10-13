"""排查 --color 不生效：Console.print(Layout) 在 force_terminal=True 下是否真的出 ANSI。"""
import io
import os
import sys

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text

print("NO_COLOR in env =", os.environ.get("NO_COLOR"))
print("TERM =", repr(os.environ.get("TERM")))


def probe(label, renderable, **kw):
    buf = io.StringIO()
    c = Console(file=buf, width=60, highlight=False, **kw)
    c.print(renderable)
    raw = buf.getvalue()
    # 数 ESC
    esc = raw.count("\x1b")
    print(f"  {label:46s} ESC={esc:3d}  truecolor={'38;2;' in raw}")
    return raw


text = Text("HELLO", style="bold #00e07a")
panel = Panel(Text("BODY", style="#00e07a"), title="// T", border_style="#00e07a")

lay = Layout()
lay.split_column(Layout(name="a", size=1), Layout(name="b", ratio=1))
lay["a"].update(Text("TOP", style="bold #00e07a"))
lay["b"].update(panel)

combos = [
    ("plain Text, force_terminal+truecolor", text,
     dict(color_system="truecolor", force_terminal=True, no_color=False)),
    ("Panel,      force_terminal+truecolor", panel,
     dict(color_system="truecolor", force_terminal=True, no_color=False)),
    ("Layout,     force_terminal+truecolor", lay,
     dict(color_system="truecolor", force_terminal=True, no_color=False)),
    ("Layout,     no_color unset (None)", lay,
     dict(color_system="truecolor", force_terminal=True)),
]
print("--- isolated (StringIO) ---")
for label, r, kw in combos:
    probe(label, r, **kw)

print("--- real app path: stdout redirected to file ---")
for flag in ([], ["--color"]):
    import subprocess
    out = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"),
         "--once", "--rows", "2"] + flag,
        capture_output=True)
    esc = out.stdout.count(b"\x1b")
    print(f"  app.py --once --rows 2 {' '.join(flag):8s} -> ESC={esc:3d} truecolor={b'38;2;' in out.stdout} "
          f"rc={out.returncode} stderr={out.stderr[:80]!r}")
