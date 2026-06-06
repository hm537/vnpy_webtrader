from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from vnpy.trader.database import DB_TZ


class McpAuditLogger:
    """MCP交易审计日志"""

    def __init__(self, log_path: Path | None) -> None:
        self.log_path = log_path

    def write(
        self,
        agent: str,
        tool: str,
        request: dict,
        result: Any,
        ok: bool
    ) -> None:
        """写入一条JSONL审计记录"""
        if not self.log_path:
            return

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        record: dict = {
            "time": datetime.now(DB_TZ).isoformat(),
            "agent": agent,
            "tool": tool,
            "request": request,
            "result": result,
            "ok": ok,
        }

        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str))
            f.write("\n")
