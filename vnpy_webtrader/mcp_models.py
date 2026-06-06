from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ToolErrorPayload:
    """MCP工具统一错误结构"""

    type: str
    message: str
    status_code: int | None = None


def tool_success(data: Any, agent: str | None = None, **meta: Any) -> dict:
    """构造MCP工具成功返回"""
    result_meta: dict = {
        "agent": agent,
        "source": "vnpy_webtrader",
        "endpoint": "mcp",
    }
    result_meta.update(meta)

    return {
        "ok": True,
        "data": data,
        "error": None,
        "meta": result_meta,
    }


def tool_failure(
    error_type: str,
    message: str,
    status_code: int | None = None,
    agent: str | None = None,
    **meta: Any
) -> dict:
    """构造MCP工具失败返回"""
    result_meta: dict = {
        "agent": agent,
        "source": "vnpy_webtrader",
        "endpoint": "mcp",
    }
    result_meta.update(meta)

    error = ToolErrorPayload(error_type, message, status_code)

    return {
        "ok": False,
        "data": None,
        "error": asdict(error),
        "meta": result_meta,
    }
