from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("APP_SECRET_KEY", "Puia5DzYecJtxba6OZPYxLT5EbJAZgm-7NvI1IDcJqI")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pikpak-test-"))
os.environ.setdefault(
    "ARIA2C_PATH", str(ROOT / "tools" / "aria2-1.37.0-win-64bit-build1" / "aria2c.exe")
)
os.environ.setdefault("ARIA2_RPC_PORT", "16810")
os.environ.setdefault("ANON_API_QPS", "200")
os.environ.setdefault("SHARE_PROBE_QPS", "200")
os.environ.setdefault("WINDOW_PROBE_QPS", "200")
os.environ.setdefault("DOWNLOAD_MAX_CONCURRENCY", "2")
os.environ.setdefault("PIKPAK_DRIVE_BASE_URL", "http://127.0.0.1:19000")
os.environ.setdefault("PIKPAK_USER_BASE_URL", "http://127.0.0.1:19000")
