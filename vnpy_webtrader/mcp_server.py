from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import HTTPException
from fastmcp import FastMCP

from .mcp_audit import McpAuditLogger
from .mcp_auth import McpAuthConfig, get_current_mcp_agent
from .mcp_models import tool_failure, tool_success


@dataclass(frozen=True)
class WebTraderMcpCallbacks:
    """WebTrader MCP工具回调集合"""

    health_check: Callable[[], dict]
    get_accounts: Callable[[], list]
    get_positions: Callable[[], list]
    get_orders: Callable[[], list]
    get_trades: Callable[[], list]
    get_contracts: Callable[[], list]
    get_ticks: Callable[[], list]
    subscribe_tick: Callable[[str], None]
    get_history_bars: Callable[[str, str, str, str | None, str, int | None], dict]
    place_order: Callable[[dict], str]
    cancel_order: Callable[[str], None]


def convert_tool_exception(err: Exception) -> tuple[str, str, int | None]:
    """将内部异常转换为统一MCP错误"""
    if isinstance(err, HTTPException):
        error_type = "validation_error" if err.status_code < 500 else "webtrader_error"
        return error_type, str(err.detail), err.status_code

    if isinstance(err, ValueError):
        return "validation_error", str(err), None

    return "webtrader_error", str(err), None


def create_webtrader_mcp_server(
    callbacks: WebTraderMcpCallbacks,
    auth_config: McpAuthConfig,
    audit_logger: McpAuditLogger
) -> FastMCP:
    """创建WebTrader MCP server"""
    mcp = FastMCP("vnpy-webtrader")

    def execute(
        tool_name: str,
        operation: Callable[[], Any],
        request: dict | None = None,
        requires_trade: bool = False
    ) -> dict:
        try:
            agent = get_current_mcp_agent()
        except Exception as err:
            return tool_failure("auth_error", str(err))

        request = request or {}

        if requires_trade and not auth_config.enable_trading:
            result = tool_failure(
                "trading_disabled",
                "MCP交易工具已被全局配置禁用",
                agent=agent.name,
            )
            audit_logger.write(agent.name, tool_name, request, result, False)
            return result

        if requires_trade and not agent.can_trade:
            result = tool_failure(
                "permission_denied",
                "当前MCP token无交易权限",
                agent=agent.name,
            )
            audit_logger.write(agent.name, tool_name, request, result, False)
            return result

        try:
            data = operation()
            result = tool_success(data, agent=agent.name)
            if requires_trade:
                audit_logger.write(agent.name, tool_name, request, result, True)
            return result
        except Exception as err:
            error_type, message, status_code = convert_tool_exception(err)
            result = tool_failure(error_type, message, status_code, agent=agent.name)
            if requires_trade:
                audit_logger.write(agent.name, tool_name, request, result, False)
            return result

    @mcp.tool()
    def webtrader_health_check() -> dict:
        """检查WebTrader MCP服务状态"""
        return execute("webtrader_health_check", callbacks.health_check)

    @mcp.tool()
    def webtrader_get_accounts() -> dict:
        """查询账户资金"""
        return execute("webtrader_get_accounts", callbacks.get_accounts)

    @mcp.tool()
    def webtrader_get_positions() -> dict:
        """查询持仓"""
        return execute("webtrader_get_positions", callbacks.get_positions)

    @mcp.tool()
    def webtrader_get_orders() -> dict:
        """查询委托"""
        return execute("webtrader_get_orders", callbacks.get_orders)

    @mcp.tool()
    def webtrader_get_trades() -> dict:
        """查询成交"""
        return execute("webtrader_get_trades", callbacks.get_trades)

    @mcp.tool()
    def webtrader_get_contracts() -> dict:
        """查询合约"""
        return execute("webtrader_get_contracts", callbacks.get_contracts)

    @mcp.tool()
    def webtrader_get_ticks() -> dict:
        """查询行情快照"""
        return execute("webtrader_get_ticks", callbacks.get_ticks)

    @mcp.tool()
    def webtrader_subscribe_tick(vt_symbol: str) -> dict:
        """订阅指定标的行情"""
        request = {"vt_symbol": vt_symbol}
        return execute(
            "webtrader_subscribe_tick",
            lambda: callbacks.subscribe_tick(vt_symbol),
            request,
        )

    @mcp.tool()
    def webtrader_get_history_bars(
        vt_symbol: str,
        interval: str,
        start: str,
        end: str | None = None,
        source: str = "auto",
        limit: int | None = None
    ) -> dict:
        """查询指定标的历史K线"""
        request = {
            "vt_symbol": vt_symbol,
            "interval": interval,
            "start": start,
            "end": end,
            "source": source,
            "limit": limit,
        }
        return execute(
            "webtrader_get_history_bars",
            lambda: callbacks.get_history_bars(vt_symbol, interval, start, end, source, limit),
            request,
        )

    @mcp.tool()
    def webtrader_place_order(
        symbol: str,
        exchange: str,
        direction: str,
        order_type: str,
        volume: float,
        price: float = 0,
        offset: str = "",
        reference: str = ""
    ) -> dict:
        """委托下单"""
        request = {
            "symbol": symbol,
            "exchange": exchange,
            "direction": direction,
            "type": order_type,
            "volume": volume,
            "price": price,
            "offset": offset,
            "reference": reference,
        }
        return execute(
            "webtrader_place_order",
            lambda: callbacks.place_order(request),
            request,
            requires_trade=True,
        )

    @mcp.tool()
    def webtrader_cancel_order(vt_orderid: str) -> dict:
        """委托撤单"""
        request = {"vt_orderid": vt_orderid}
        return execute(
            "webtrader_cancel_order",
            lambda: callbacks.cancel_order(vt_orderid),
            request,
            requires_trade=True,
        )

    return mcp
