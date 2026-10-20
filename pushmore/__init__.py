"""PushMore: inbound webhook gateway for Pushover."""

import os

__version__ = os.environ.get("PUSHMORE_VERSION", "0.0.0-dev")
