#!/usr/bin/env python3
"""JieLi (杰理) Cross-Platform Firmware Flashing Tool.

Supports Windows, Linux, and macOS via USB (Mass Storage BOT) & Serial (UART).
Clean-room reimplementation of JieLi's Windows `download.exe` / `isd_download.exe`.
"""

import sys
from pathlib import Path

# Add directory to sys.path so modules can be imported directly
MODULE_DIR = Path(__file__).resolve().parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from jieli_flasher.cli import main

if __name__ == "__main__":
    sys.exit(main())
