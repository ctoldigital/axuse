"""axdroid ↔ google-research/android_world adapter.

Runs the official AndroidWorld 116-task suite with axdroid as the agent's
only window into the device:

  - benchmark infrastructure (task setup, rewards, app installs, datetime
    control) uses AndroidWorld's own adb paths, executed by `adb_bridge`
    against a standard rootable emulator (Pixel 6, API 33, google_apis);
  - the *agent* observes and acts exclusively through the axdroid app's
    accessibility bridge (semantic performAction, no screenshots, no
    uiautomator), exposed to any MCP client by the phone app itself.
"""
