"""Claude, as an Agent Host Protocol provider for `ahp-host`."""

from ahp_host_claude.agent import from_config
from ahp_host_claude.provider import ClaudeProvider, ClaudeSession

__version__ = "0.1.0"

__all__ = ["ClaudeProvider", "ClaudeSession", "__version__", "from_config"]
