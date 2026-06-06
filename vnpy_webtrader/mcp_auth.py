from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import secrets
from typing import Any

from fastmcp.server.dependencies import get_http_request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


MCP_AGENT_SCOPE_KEY = "vnpy_webtrader_mcp_agent"


@dataclass(frozen=True)
class McpAgent:
    """MCP访问主体"""

    name: str
    can_trade: bool = False


@dataclass(frozen=True)
class McpTokenRecord:
    """MCP token配置"""

    agent: McpAgent
    token_sha256: str


@dataclass(frozen=True)
class McpAuthConfig:
    """MCP鉴权配置"""

    enabled: bool
    enable_trading: bool
    tokens: tuple[McpTokenRecord, ...]


def load_mcp_auth_config(setting: dict) -> McpAuthConfig:
    """从WebTrader配置加载MCP鉴权配置"""
    raw_tokens: dict[str, Any] = setting.get("mcp_tokens") or {}
    token_records: list[McpTokenRecord] = []

    for agent_name, raw_record in raw_tokens.items():
        if not isinstance(raw_record, dict):
            continue

        token_hash: str = str(raw_record.get("token_sha256", "")).strip().lower()
        if not token_hash:
            continue

        agent = McpAgent(
            name=str(agent_name),
            can_trade=bool(raw_record.get("can_trade", False))
        )
        token_records.append(McpTokenRecord(agent=agent, token_sha256=token_hash))

    return McpAuthConfig(
        enabled=bool(setting.get("mcp_enabled", False)),
        enable_trading=bool(setting.get("mcp_enable_trading", False)),
        tokens=tuple(token_records),
    )


def hash_mcp_token(token: str) -> str:
    """计算MCP token的SHA256"""
    return sha256(token.encode("utf-8")).hexdigest()


def verify_mcp_token(config: McpAuthConfig, token: str) -> McpAgent | None:
    """校验MCP token并返回对应agent"""
    token_hash: str = hash_mcp_token(token)

    for record in config.tokens:
        if secrets.compare_digest(token_hash, record.token_sha256):
            return record.agent

    return None


def parse_bearer_token(header_value: str | None) -> str | None:
    """解析Authorization Bearer token"""
    if not header_value:
        return None

    scheme, _, token = header_value.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    return token.strip()


def get_current_mcp_agent() -> McpAgent:
    """从当前FastMCP HTTP请求中获取agent"""
    request = get_http_request()
    agent = request.scope.get(MCP_AGENT_SCOPE_KEY)
    if not isinstance(agent, McpAgent):
        raise RuntimeError("MCP请求未通过鉴权")
    return agent


class McpBearerAuthMiddleware:
    """保护/mcp endpoint的Bearer token鉴权中间件"""

    def __init__(self, app: ASGIApp, auth_config: McpAuthConfig) -> None:
        self.app = app
        self.auth_config = auth_config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path: str = scope.get("path", "")
        if scope["type"] != "http" or path.rstrip("/") != "/mcp":
            await self.app(scope, receive, send)
            return

        if not self.auth_config.enabled:
            response = JSONResponse(
                {"detail": "MCP endpoint is disabled"},
                status_code=404,
            )
            await response(scope, receive, send)
            return

        headers: dict[bytes, bytes] = dict(scope.get("headers") or [])
        token = parse_bearer_token(headers.get(b"authorization", b"").decode("latin1"))
        agent = verify_mcp_token(self.auth_config, token or "")

        if not agent:
            response = JSONResponse(
                {"detail": "Could not validate MCP bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        scope[MCP_AGENT_SCOPE_KEY] = agent
        await self.app(scope, receive, send)
