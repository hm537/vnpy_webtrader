import sys
from pathlib import Path
from hashlib import sha256

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import QtWidgets, QtCore
from vnpy.trader.utility import load_json, save_json, get_file_path

from ..engine import APP_NAME, WebEngine


class WebManager(QtWidgets.QWidget):
    """网页服务器管理界面"""

    setting_filename: str = "web_trader_setting.json"
    setting_filepath: Path = get_file_path(setting_filename)

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """"""
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.event_engine: EventEngine = event_engine
        self.web_engine: WebEngine = main_engine.get_engine(APP_NAME)

        self.init_ui()

    def init_ui(self) -> None:
        """初始化界面"""
        self.setWindowTitle("Web服务")

        setting: dict = load_json(self.setting_filepath)
        username: str = setting.get("username", "vnpy")
        password: str = setting.get("password", "vnpy")
        req_address: str = setting.get("req_address", "tcp://127.0.0.1:2014")
        sub_address: str = setting.get("sub_address", "tcp://127.0.0.1:4102")
        host: str = setting.get("host", "127.0.0.1")
        port: str = setting.get("port", "8000")
        mcp_enabled: bool = bool(setting.get("mcp_enabled", False))
        mcp_enable_trading: bool = bool(setting.get("mcp_enable_trading", False))
        mcp_tokens: dict = setting.get("mcp_tokens") or {}
        mcp_agent_name: str = next(iter(mcp_tokens), "agent-01")
        mcp_token_record: dict = mcp_tokens.get(mcp_agent_name, {})
        mcp_can_trade: bool = bool(mcp_token_record.get("can_trade", False))
        mcp_audit_log: str = setting.get("mcp_audit_log", "web_trader_mcp_audit.jsonl")

        self.username_line: QtWidgets.QLineEdit = QtWidgets.QLineEdit(username)
        self.password_line: QtWidgets.QLineEdit = QtWidgets.QLineEdit(password)
        self.req_line: QtWidgets.QLineEdit = QtWidgets.QLineEdit(req_address)
        self.sub_line: QtWidgets.QLineEdit = QtWidgets.QLineEdit(sub_address)
        self.host_line: QtWidgets.QLineEdit = QtWidgets.QLineEdit(host)
        self.port_line: QtWidgets.QLineEdit = QtWidgets.QLineEdit(port)
        self.mcp_enabled_checkbox: QtWidgets.QCheckBox = QtWidgets.QCheckBox()
        self.mcp_enabled_checkbox.setChecked(mcp_enabled)
        self.mcp_trading_checkbox: QtWidgets.QCheckBox = QtWidgets.QCheckBox()
        self.mcp_trading_checkbox.setChecked(mcp_enable_trading)
        self.mcp_agent_line: QtWidgets.QLineEdit = QtWidgets.QLineEdit(mcp_agent_name)
        self.mcp_token_line: QtWidgets.QLineEdit = QtWidgets.QLineEdit()
        self.mcp_token_line.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.mcp_token_line.setPlaceholderText("留空则保留已有token")
        self.mcp_token_trade_checkbox: QtWidgets.QCheckBox = QtWidgets.QCheckBox()
        self.mcp_token_trade_checkbox.setChecked(mcp_can_trade)
        self.mcp_audit_line: QtWidgets.QLineEdit = QtWidgets.QLineEdit(mcp_audit_log)

        self.start_button: QtWidgets.QPushButton = QtWidgets.QPushButton("启动")
        self.start_button.clicked.connect(self.start)

        self.end_button: QtWidgets.QPushButton = QtWidgets.QPushButton("停止")
        self.end_button.clicked.connect(self.end)

        self.text_edit: QtWidgets.QTextEdit = QtWidgets.QTextEdit()
        self.text_edit.setReadOnly(True)

        form: QtWidgets.QFormLayout = QtWidgets.QFormLayout()
        form.addRow("用户名", self.username_line)
        form.addRow("密码", self.password_line)
        form.addRow("请求地址", self.req_line)
        form.addRow("订阅地址", self.sub_line)
        form.addRow("监听地址", self.host_line)
        form.addRow("监听端口", self.port_line)
        form.addRow("启用MCP", self.mcp_enabled_checkbox)
        form.addRow("启用MCP交易", self.mcp_trading_checkbox)
        form.addRow("MCP Agent", self.mcp_agent_line)
        form.addRow("MCP Token", self.mcp_token_line)
        form.addRow("MCP Token交易", self.mcp_token_trade_checkbox)
        form.addRow("MCP审计日志", self.mcp_audit_line)
        form.addRow(self.start_button)
        form.addRow(self.end_button)

        self.end_button.setEnabled(False)

        hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox.addLayout(form)
        hbox.addWidget(self.text_edit)
        self.setLayout(hbox)

        self.resize(1000, 500)

    def start(self) -> None:
        """启动引擎"""
        username: str = self.username_line.text()
        password: str = self.password_line.text()
        req_address: str = self.req_line.text()
        sub_address: str = self.sub_line.text()
        host: str = self.host_line.text()
        port: str = self.port_line.text()
        mcp_enabled: bool = self.mcp_enabled_checkbox.isChecked()
        mcp_enable_trading: bool = self.mcp_trading_checkbox.isChecked()
        mcp_agent_name: str = self.mcp_agent_line.text().strip() or "agent-01"
        mcp_token: str = self.mcp_token_line.text().strip()
        mcp_can_trade: bool = self.mcp_token_trade_checkbox.isChecked()
        mcp_audit_log: str = self.mcp_audit_line.text().strip() or "web_trader_mcp_audit.jsonl"

        # 保存配置
        old_setting: dict = load_json(self.setting_filepath)
        mcp_tokens: dict = dict(old_setting.get("mcp_tokens") or {})
        old_token_record: dict = dict(mcp_tokens.get(mcp_agent_name, {}))
        if mcp_token:
            old_token_record["token_sha256"] = sha256(mcp_token.encode("utf-8")).hexdigest()
        old_token_record["can_trade"] = mcp_can_trade
        mcp_tokens[mcp_agent_name] = old_token_record

        setting: dict = {
            **old_setting,
            "username": username,
            "password": password,
            "req_address": req_address,
            "sub_address": sub_address,
            "host": host,
            "port": port,
            "mcp_enabled": mcp_enabled,
            "mcp_tokens": mcp_tokens,
            "mcp_enable_trading": mcp_enable_trading,
            "mcp_audit_log": mcp_audit_log
        }
        save_json(self.setting_filepath, setting)

        # 启动RPC
        self.web_engine.start_server(req_address, sub_address)
        self.start_button.setDisabled(True)

        # 初始化Web服务子进程
        self.process: QtCore.QProcess = QtCore.QProcess(self)
        self.process.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)

        self.process.readyReadStandardOutput.connect(self.data_ready)
        self.process.readyReadStandardError.connect(self.data_ready)
        self.process.started.connect(self.web_started)
        self.process.finished.connect(self.web_finished)

        # 启动子进程
        cmd: list = [
            "-m",
            "uvicorn",
            "vnpy_webtrader.web:app",
            f"--host={host}",
            f"--port={port}"
        ]
        self.process.start(sys.executable, cmd)

    def end(self) -> None:
        """终止引擎"""
        self.process.kill()

    def web_started(self) -> None:
        """Web进程启动"""
        self.text_edit.append("Web服务器启动")

        for w in [
            self.username_line,
            self.password_line,
            self.req_line,
            self.sub_line,
            self.host_line,
            self.port_line,
            self.mcp_enabled_checkbox,
            self.mcp_trading_checkbox,
            self.mcp_agent_line,
            self.mcp_token_line,
            self.mcp_token_trade_checkbox,
            self.mcp_audit_line,
            self.start_button
        ]:
            w.setEnabled(False)

        self.end_button.setEnabled(True)

    def web_finished(self) -> None:
        """Web进程结束"""
        self.text_edit.append("Web服务器停止")

        for w in [
            self.username_line,
            self.password_line,
            self.req_line,
            self.sub_line,
            self.host_line,
            self.port_line,
            self.mcp_enabled_checkbox,
            self.mcp_trading_checkbox,
            self.mcp_agent_line,
            self.mcp_token_line,
            self.mcp_token_trade_checkbox,
            self.mcp_audit_line,
            self.start_button
        ]:
            w.setEnabled(True)

        self.end_button.setEnabled(False)

    def data_ready(self) -> None:
        """更新进程有数据可读"""
        _bytes: bytes = bytes(self.process.readAll())

        try:
            text: str = _bytes.decode("UTF8")
        except UnicodeDecodeError:
            text = _bytes.decode("GBK")

        self.text_edit.append(text)
