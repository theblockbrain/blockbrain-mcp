"""
Tool registry.

⭐ CUSTOMIZE HERE — to add / remove / replace tools:
    1. Create a new file under `tools/` (use one of the existing ones as template).
    2. Import the tool function below.
    3. Add it to `ALL_TOOLS`.
    4. Done — `server.py` registers everything in `ALL_TOOLS` automatically.
"""

from .read_url import read_url
from .research import research_topic
from .web_search import web_search
from .whoami import whoami

ALL_TOOLS = [
    whoami,  # demonstrates OAuth user-context — keep as a sanity check
    web_search,  # ⭐ REPLACEABLE — swap for Tavily / Perplexity / your own
    read_url,  # ⭐ REPLACEABLE — swap for Firecrawl / Jina Reader / your own
    research_topic,  # ⭐ REPLACEABLE — example composite tool
]

__all__ = ["ALL_TOOLS"]
