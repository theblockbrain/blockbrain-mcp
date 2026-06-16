# Adding a tool

Three steps. No framework knowledge required beyond "FastMCP turns my Python
function into an MCP tool by reading its type hints and docstring."

## 1. Create the tool file

`src/blockbrain_mcp/tools/my_tool.py`:

```python
"""
⭐ CUSTOMIZE HERE — replace with your own tool.

The docstring on `my_tool` below becomes the description the LLM sees.
Argument names and types become the tool's input schema.
The pydantic return model becomes the tool's output schema.
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from ._base import get_user_context

log = structlog.get_logger(__name__)


class MyResult(BaseModel):
    echoed: str
    user_email: str | None


async def my_tool(message: str) -> MyResult:
    """Echo a message, attributed to the authenticated user.

    Args:
        message: Anything you want echoed back.
    """
    user = get_user_context()
    log.info("my_tool.call", user_id=user.user_id, message=message)
    return MyResult(echoed=message, user_email=user.email)
```

## 2. Register it

`src/blockbrain_mcp/tools/__init__.py`:

```python
from .my_tool import my_tool
from .whoami import whoami

ALL_TOOLS = [whoami, my_tool]
```

Drop whichever of `web_search`, `read_url`, `research_topic` you do not want.

## 3. Test it

Create `tests/test_tools_my_tool.py`:

```python
import pytest

from blockbrain_mcp.tools.my_tool import my_tool


@pytest.mark.asyncio
async def test_my_tool_echoes(mock_user_context):
    result = await my_tool(message="hello")
    assert result.echoed == "hello"
    assert result.user_email == mock_user_context.email
```

Run `make test`. You are done.

---

## Patterns you'll want to follow

### External HTTP call

Use the async httpx client directly:

```python
async with httpx.AsyncClient(timeout=10.0) as client:
    resp = await client.get(url, headers={...})
    resp.raise_for_status()
    data = resp.json()
```

No wrapper classes or dependency-injection framework needed.

### Secrets / API keys

Add them to `Settings` in `config.py` and to `.env.example`. Do **not** read
`os.environ` directly from a tool — it breaks env-file loading and tests.

```python
# config.py
openai_api_key: str = Field(default="", description="OpenAI API key.")
```

```python
# tool
from ..config import get_settings
...
key = get_settings().openai_api_key
if not key:
    raise RuntimeError("OPENAI_API_KEY not configured.")
```

### Per-user rate limiting / quotas

`get_user_context().user_id` is stable across requests (Zitadel `sub` claim).
For a production deployment, store the counter in Redis keyed on
`(user_id, YYYY-MM-DD)`. See `tools/research.py` for the in-memory version.

### Composite tools

Call other tool functions directly (they are plain `async def`). This runs
inside the same request context, so the `UserContext` propagates. See
`tools/research.py` for the pattern.
