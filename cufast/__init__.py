"""cufast: a fast Windows computer-use agent harness.

The hot path -- screen capture, downscale, JPEG encode, and input injection -- lives
in the native `_native` extension. This package wraps it with coordinate handling,
action dispatch, and an MCP server.
"""

from cufast import _native
from cufast.config import Config
from cufast.session import ActionError, Session

__all__ = ["Config", "Session", "ActionError", "_native"]
