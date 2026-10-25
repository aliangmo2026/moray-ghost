"""moray-ghost 官方 Python SDK。

用法::

    from sdk import GhostClient

    client = GhostClient(agent_id="my-agent")
    client.environment.check()
    client.action.click(1280, 720)
    client.task.run(steps=[{"kind": "wait", "ms": 300}])
"""
from .ghost_client import (
    DEFAULT_BASE_URL,
    ActionAPI,
    EnvironmentAPI,
    GhostAuthError,
    GhostClient,
    GhostConnectionError,
    GhostError,
    GhostForbiddenError,
    GhostFrozenError,
    HealthAPI,
    MacroAPI,
    SystemAPI,
    SystemNotReadableError,
    TaskAPI,
    TaskDegradedError,
    TaskTimeoutError,
    WindowAPI,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "ActionAPI",
    "EnvironmentAPI",
    "GhostAuthError",
    "GhostClient",
    "GhostConnectionError",
    "GhostError",
    "GhostForbiddenError",
    "GhostFrozenError",
    "HealthAPI",
    "MacroAPI",
    "SystemAPI",
    "SystemNotReadableError",
    "TaskAPI",
    "TaskDegradedError",
    "TaskTimeoutError",
    "WindowAPI",
]
__version__ = "0.5.0"
