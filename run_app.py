"""Launcher dla PyInstaller (i uruchamiania ręcznego: python run_app.py)."""

import sys

from app.main import main

if __name__ == "__main__":
    sys.exit(main())
