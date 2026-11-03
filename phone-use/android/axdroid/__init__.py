"""axdroid — axuse phone-use for Android.

Drive a real Android phone from any AI agent through the phone's own
accessibility tree — text first, a guarded screenshot fallback, no ADB for consumers.
The phone app *is* the hub: `com.axuse.axdroid` exposes a token-guarded RPC
server on the device; this package is the thin, zero-dependency host side
(render/diff engine, MCP server, CLI, simulator, hardware gates).
"""

__version__ = "0.1.0"
