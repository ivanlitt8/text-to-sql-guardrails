"""
Launcher CLI desde la raíz del repo.

Uso:
  python main.py "¿Cuáles son los 5 vuelos más baratos?"
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

_SRC_MAIN = Path(__file__).resolve().parent / "src" / "main.py"


def main() -> None:
    sys.argv[0] = str(_SRC_MAIN)
    runpy.run_path(str(_SRC_MAIN), run_name="__main__")


if __name__ == "__main__":
    main()
