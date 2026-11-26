from __future__ import annotations

import os
import sys
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    sdk_path = parent / "mn-python-sdk"
    if (sdk_path / "mn_sdk" / "client.py").exists() and str(sdk_path) not in sys.path:
        sys.path.insert(0, str(sdk_path))
        break

# The API starts blueprint pre-launch hooks and validators after the SDK gRPC
# client has been used. On macOS, gRPC's fork handlers can corrupt stdio in
# those child processes, so keep fork support disabled unless explicitly set.
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")
