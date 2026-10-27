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
    DrawAPI,
    EnvironmentAPI,
    GhostAuthError,
    GhostClient,
    GhostConnectionError,
    GhostError,
    GhostApprovalRequiredError,
    GhostForbiddenError,
    GhostFrozenError,
    HealthAPI,
    MacroAPI,
    SystemAPI,
    SystemNotReadableError,
    TaskAPI,
    TaskDegradedError,
    TaskTimeoutError,
    VisionAPI,
    WindowAPI,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "ActionAPI",
    "DrawAPI",
    "EnvironmentAPI",
    "GhostAuthError",
    "GhostClient",
    "GhostConnectionError",
    "GhostError",
    "GhostApprovalRequiredError",
    "GhostForbiddenError",
    "GhostFrozenError",
    "HealthAPI",
    "MacroAPI",
    "SystemAPI",
    "SystemNotReadableError",
    "TaskAPI",
    "TaskDegradedError",
    "TaskTimeoutError",
    "VisionAPI",
    "WindowAPI",
]
__version__ = "0.14.0"
