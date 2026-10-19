from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent
subprocess.Popen(
    [sys.executable, str(root / "bot.py")],
    cwd=str(root),
    creationflags=subprocess.CREATE_NO_WINDOW,
)
