from enum import Enum
from typing import Any, Literal
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import secrets

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, status, Depends, Query
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from jose import jwt, JWTError
from passlib.context import CryptContext
from pathlib import Path
from starlette.middleware import Middleware

from vnpy.rpc import RpcClient
from vnpy.trader.object import (
    AccountData,
    BarData,
    ContractData,
    HistoryRequest,
    OrderData,
    OrderRequest,
    PositionData,
    SubscribeRequest,
    CancelRequest,
    TickData,
    TradeData
)
from vnpy.trader.constant import (
    Exchange,
    Direction,
    Interval,
    OrderType,
    Offset,
)
from vnpy.trader.database import DB_TZ, get_database
from vnpy.trader.datafeed import get_datafeed
from vnpy.trader.utility import BarGenerator, extract_vt_symbol, load_json, get_file_path

from vnpy_webtrader.mcp_audit import McpAuditLogger
from vnpy_webtrader.mcp_auth import McpBearerAuthMiddleware, load_mcp_auth_config
from vnpy_webtrader.mcp_server import WebTraderMcpCallbacks, create_webtrader_mcp_server


# Web服务运行配置
SETTING_FILENAME = "web_trader_setting.json"
SETTING_FILEPATH = get_file_path(SETTING_FILENAME)

setting: dict = load_json(SETTING_FILEPATH)
USERNAME = setting["username"]              # 用户名
PASSWORD = setting["password"]              # 密码
REQ_ADDRESS = setting["req_address"]        # 请求服务地址
SUB_ADDRESS = setting["sub_address"]        # 订阅服务地址


MCP_AUTH_CONFIG = load_mcp_auth_config(setting)
MCP_AUDIT_LOG = setting.get("mcp_audit_log", "web_trader_mcp_audit.jsonl")


SECRET_KEY = "test"                     # 数据加密密钥
ALGORITHM = "HS256"                     # 加密算法
ACCESS_TOKEN_EXPIRE_MINUTES = 30        # 令牌超时（分钟）


# 实例化CryptContext用于处理哈希密码
pwd_context: CryptContext = CryptContext(schemes=["sha256_crypt"], deprecated="auto")

# FastAPI密码鉴权工具
oauth2_scheme: OAuth2PasswordBearer = OAuth2PasswordBearer(tokenUrl="token")

# RPC客户端
rpc_client: RpcClient = None


def to_dict(o: object) -> dict:
    """将对象转换为字典"""
    data: dict = {}
    for k, v in o.__dict__.items():
        if isinstance(v, Enum):
            data[k] = v.value
        elif isinstance(v, datetime):
            data[k] = str(v)
        else:
            data[k] = v
    return data


class Token(BaseModel):
    """令牌数据"""
    access_token: str
    token_type: str


class OrderRequestModel(BaseModel):
    """委托请求模型"""
    symbol: str
    exchange: Exchange
    direction: Direction
    type: OrderType
    volume: float
    price: float = 0
    offset: Offset = Offset.NONE
    reference: str = ""


class HistorySource(str, Enum):
    """历史数据来源"""
    AUTO = "auto"
    DATABASE = "database"
    DATAFEED = "datafeed"


HISTORY_INTERVAL_ALIASES: dict[str, str] = {
    "1m": "1m",
    "1min": "1m",
    "minute": "1m",
    "5m": "5m",
    "5min": "5m",
    "15m": "15m",
    "15min": "15m",
    "30m": "30m",
    "30min": "30m",
    "1h": "1h",
    "60m": "1h",
    "hour": "1h",
    "d": "d",
    "1d": "d",
    "day": "d",
    "daily": "d",
    "w": "w",
    "1w": "w",
    "week": "w",
    "weekly": "w",
}


DIRECT_DB_INTERVALS: dict[str, Interval] = {
    "1m": Interval.MINUTE,
    "1h": Interval.HOUR,
    "d": Interval.DAILY,
    "w": Interval.WEEKLY,
}


DIRECT_DATAFEED_INTERVALS: dict[str, Interval] = {
    "1m": Interval.MINUTE,
    "d": Interval.DAILY,
}


SUPPORTED_HISTORY_INTERVALS: tuple[str, ...] = ("1m", "5m", "15m", "30m", "1h", "d", "w")


def normalize_history_interval(raw_interval: str) -> str:
    """标准化历史数据周期参数"""
    interval_key: str = HISTORY_INTERVAL_ALIASES.get(raw_interval.strip().lower(), "")
    if not interval_key:
        supported: str = ", ".join(SUPPORTED_HISTORY_INTERVALS)
        raise ValueError(f"不支持的K线周期：{raw_interval}，仅支持：{supported}")
    return interval_key


def normalize_rpc_client_address(address: str) -> str:
    """将通配监听地址转换为客户端可连接的回环地址"""
    normalized: str = address.strip()

    wildcard_hosts: tuple[str, ...] = (
        "tcp://0.0.0.0:",
        "tcp://*:",
        "tcp://[::]:",
        "tcp://:::",
    )
    for prefix in wildcard_hosts:
        if normalized.startswith(prefix):
            return normalized.replace(prefix, "tcp://127.0.0.1:", 1)

    return normalized


def normalize_query_datetime(dt: datetime) -> datetime:
    """统一到数据库时区"""
    if dt.tzinfo:
        dt = dt.astimezone(DB_TZ)
    else:
        dt = dt.replace(tzinfo=DB_TZ)
    return dt.replace(microsecond=0)


def parse_query_datetime(raw_value: str, is_end: bool = False) -> datetime:
    """解析查询参数中的时间"""
    text: str = raw_value.strip()
    if not text:
        raise ValueError("时间参数不能为空")

    date_only: bool = all(marker not in text for marker in ("T", " ", ":"))
    normalized: str = text.replace("Z", "+00:00")

    try:
        dt: datetime = datetime.fromisoformat(normalized)
    except ValueError:
        dt = None

    if dt is None:
        formats: tuple[str, ...] = (
            "%Y%m%d%H%M%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%dT%H:%M",
            "%Y%m%d",
            "%Y-%m-%d",
        )
        for fmt in formats:
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue

    if dt is None:
        raise ValueError(f"无法解析时间参数：{raw_value}")

    dt = normalize_query_datetime(dt)

    if date_only and is_end:
        dt = dt + timedelta(days=1) - timedelta(seconds=1)

    return dt


def align_history_start(start: datetime, interval_key: str) -> datetime:
    """按查询周期向前对齐开始时间"""
    if interval_key in {"1m", "5m", "15m", "30m"}:
        minute: int = start.minute
        if interval_key == "1m":
            minute = minute
        else:
            window: int = int(interval_key[:-1])
            minute = minute - minute % window
        return start.replace(minute=minute, second=0, microsecond=0)

    if interval_key == "1h":
        return start.replace(minute=0, second=0, microsecond=0)

    if interval_key == "d":
        return start.replace(hour=0, minute=0, second=0, microsecond=0)

    week_start: datetime = start - timedelta(days=start.weekday())
    return week_start.replace(hour=0, minute=0, second=0, microsecond=0)


def get_base_interval(interval_key: str) -> Interval:
    """获取目标周期对应的基础周期"""
    if interval_key in {"1m", "5m", "15m", "30m", "1h"}:
        return Interval.MINUTE
    return Interval.DAILY


def requires_aggregation(interval_key: str) -> bool:
    """判断是否需要通过基础周期聚合"""
    return interval_key in {"5m", "15m", "30m", "1h", "w"}


def database_has_bar_coverage(
    symbol: str,
    exchange: Exchange,
    interval: Interval,
    start: datetime,
    end: datetime
) -> bool:
    """判断数据库是否覆盖所需时间范围"""
    query_start: datetime = start.astimezone(DB_TZ).replace(tzinfo=None)
    query_end: datetime = end.astimezone(DB_TZ).replace(tzinfo=None)

    for overview in get_database().get_bar_overview():
        if overview.symbol != symbol:
            continue
        if overview.exchange != exchange:
            continue
        if overview.interval != interval:
            continue
        if overview.start <= query_start and overview.end >= query_end:
            return True
    return False


def filter_history_bars(bars: list[BarData], start: datetime, end: datetime) -> list[BarData]:
    """按请求时间范围过滤K线"""
    return [bar for bar in bars if start <= bar.datetime <= end]


def aggregate_minute_window_bars(base_bars: list[BarData], window: int) -> list[BarData]:
    """将1分钟K线聚合成多分钟K线"""
    history: list[BarData] = []
    generator: BarGenerator = BarGenerator(
        on_bar=lambda bar: None,
        window=window,
        on_window_bar=history.append,
        interval=Interval.MINUTE
    )
    for bar in base_bars:
        generator.update_bar(bar)
    return history


def aggregate_hour_bars(base_bars: list[BarData]) -> list[BarData]:
    """将1分钟K线聚合成1小时K线"""
    history: list[BarData] = []
    generator: BarGenerator = BarGenerator(
        on_bar=lambda bar: None,
        window=1,
        on_window_bar=history.append,
        interval=Interval.HOUR
    )
    for bar in base_bars:
        generator.update_bar(bar)

    for bar in history:
        bar.interval = Interval.HOUR

    return history


def aggregate_weekly_bars(base_bars: list[BarData]) -> list[BarData]:
    """将日线聚合成周线"""
    history: list[BarData] = []
    weekly_bar: BarData | None = None

    for bar in base_bars:
        week_start: datetime = (bar.datetime - timedelta(days=bar.datetime.weekday())).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        )

        if not weekly_bar or weekly_bar.datetime != week_start:
            if weekly_bar:
                history.append(weekly_bar)

            weekly_bar = BarData(
                symbol=bar.symbol,
                exchange=bar.exchange,
                datetime=week_start,
                interval=Interval.WEEKLY,
                volume=bar.volume,
                turnover=bar.turnover,
                open_interest=bar.open_interest,
                open_price=bar.open_price,
                high_price=bar.high_price,
                low_price=bar.low_price,
                close_price=bar.close_price,
                gateway_name=bar.gateway_name
            )
            continue

        weekly_bar.high_price = max(weekly_bar.high_price, bar.high_price)
        weekly_bar.low_price = min(weekly_bar.low_price, bar.low_price)
        weekly_bar.close_price = bar.close_price
        weekly_bar.volume += bar.volume
        weekly_bar.turnover += bar.turnover
        weekly_bar.open_interest = bar.open_interest

    if weekly_bar:
        history.append(weekly_bar)

    return history


def aggregate_history_bars(
    base_bars: list[BarData],
    interval_key: str,
    start: datetime,
    end: datetime
) -> list[BarData]:
    """根据目标周期聚合K线"""
    if not base_bars:
        return []

    if interval_key in {"5m", "15m", "30m"}:
        window: int = int(interval_key[:-1])
        history: list[BarData] = aggregate_minute_window_bars(base_bars, window)
    elif interval_key == "1h":
        history = aggregate_hour_bars(base_bars)
    elif interval_key == "w":
        history = aggregate_weekly_bars(base_bars)
    else:
        history = base_bars

    return filter_history_bars(history, start, end)


def load_bars_from_database(
    symbol: str,
    exchange: Exchange,
    interval_key: str,
    start: datetime,
    end: datetime
) -> list[BarData]:
    """从本地数据库读取历史K线"""
    database = get_database()
    query_start: datetime = align_history_start(start, interval_key)

    direct_interval: Interval | None = DIRECT_DB_INTERVALS.get(interval_key, None)
    if direct_interval:
        history: list[BarData] = database.load_bar_data(
            symbol,
            exchange,
            direct_interval,
            query_start,
            end
        )
        if history or not requires_aggregation(interval_key):
            return filter_history_bars(history, start, end)

    base_interval: Interval = get_base_interval(interval_key)
    history = database.load_bar_data(
        symbol,
        exchange,
        base_interval,
        query_start,
        end
    )
    return aggregate_history_bars(history, interval_key, start, end)


def load_bars_from_datafeed(
    symbol: str,
    exchange: Exchange,
    interval_key: str,
    start: datetime,
    end: datetime
) -> list[BarData]:
    """从数据服务读取历史K线"""
    datafeed = get_datafeed()
    query_start: datetime = align_history_start(start, interval_key)

    direct_interval: Interval | None = DIRECT_DATAFEED_INTERVALS.get(interval_key, None)
    if direct_interval:
        req: HistoryRequest = HistoryRequest(
            symbol=symbol,
            exchange=exchange,
            interval=direct_interval,
            start=query_start,
            end=end
        )
        history: list[BarData] = datafeed.query_bar_history(req) or []
        return filter_history_bars(history, start, end)

    base_interval: Interval = get_base_interval(interval_key)
    req = HistoryRequest(
        symbol=symbol,
        exchange=exchange,
        interval=base_interval,
        start=query_start,
        end=end
    )
    history = datafeed.query_bar_history(req) or []
    return aggregate_history_bars(history, interval_key, start, end)


def can_fulfill_from_database(
    symbol: str,
    exchange: Exchange,
    interval_key: str,
    start: datetime,
    end: datetime
) -> bool:
    """判断数据库是否足够覆盖请求范围"""
    direct_interval: Interval | None = DIRECT_DB_INTERVALS.get(interval_key, None)
    if direct_interval and database_has_bar_coverage(symbol, exchange, direct_interval, start, end):
        return True

    if not requires_aggregation(interval_key):
        return False

    query_start: datetime = align_history_start(start, interval_key)
    base_interval: Interval = get_base_interval(interval_key)
    return database_has_bar_coverage(symbol, exchange, base_interval, query_start, end)


def query_history_bars(
    vt_symbol: str,
    interval_key: str,
    start: datetime,
    end: datetime,
    source: HistorySource
) -> tuple[list[BarData], str]:
    """按指定来源查询历史K线"""
    symbol, exchange = extract_vt_symbol(vt_symbol)

    if source == HistorySource.DATABASE:
        history: list[BarData] = load_bars_from_database(symbol, exchange, interval_key, start, end)
        return history, HistorySource.DATABASE.value

    if source == HistorySource.DATAFEED:
        history = load_bars_from_datafeed(symbol, exchange, interval_key, start, end)
        return history, HistorySource.DATAFEED.value

    db_history: list[BarData] = load_bars_from_database(symbol, exchange, interval_key, start, end)
    if db_history and can_fulfill_from_database(symbol, exchange, interval_key, start, end):
        return db_history, HistorySource.DATABASE.value

    datafeed_history: list[BarData] = load_bars_from_datafeed(symbol, exchange, interval_key, start, end)
    if datafeed_history:
        return datafeed_history, HistorySource.DATAFEED.value

    return db_history, HistorySource.DATABASE.value


def history_bar_to_dict(bar: BarData, interval_key: str) -> dict:
    """序列化历史K线"""
    data: dict = to_dict(bar)
    data["interval"] = interval_key
    return data


def authenticate_user(current_username: str, username: str, password: str) -> str | Literal[False]:
    """校验用户"""
    hashed_password = pwd_context.hash(PASSWORD)

    if not secrets.compare_digest(current_username, username):
        return False

    if not pwd_context.verify(password, hashed_password):
        return False

    return username


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """创建令牌"""
    to_encode: dict = data.copy()

    if expires_delta:
        expire: datetime = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)

    to_encode.update({"exp": expire})
    encoded_jwt: str = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


async def get_access(token: str = Depends(oauth2_scheme)) -> bool:
    """REST鉴权"""
    credentials_exception: HTTPException = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload: dict = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username_value = payload.get("sub")
        if username_value is None:
            raise credentials_exception
        username: str = username_value
    except JWTError as err:
        raise credentials_exception from err

    if not secrets.compare_digest(USERNAME, username):
        raise credentials_exception

    return True


def parse_enum_value(enum_type: type[Enum], raw_value: str | Enum) -> Enum:
    """按枚举名称或枚举值解析参数"""
    if isinstance(raw_value, enum_type):
        return raw_value

    text: str = str(raw_value).strip()
    for item in enum_type:
        if text == item.value or text.upper() == item.name:
            return item

    raise ValueError(f"无法解析{enum_type.__name__}参数：{raw_value}")


def ensure_rpc_client() -> RpcClient:
    """获取已启动的RPC客户端"""
    if rpc_client is None:
        raise RuntimeError("RPC客户端尚未启动")
    return rpc_client


def query_contract(vt_symbol: str) -> ContractData | None:
    """查询单个合约"""
    return ensure_rpc_client().get_contract(vt_symbol)


def subscribe_tick(vt_symbol: str) -> None:
    """订阅行情"""
    contract: ContractData | None = query_contract(vt_symbol)
    if not contract:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"找不到合约{vt_symbol}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    req: SubscribeRequest = SubscribeRequest(contract.symbol, contract.exchange)
    ensure_rpc_client().subscribe(req, contract.gateway_name)


def query_all_ticks() -> list:
    """查询全部行情信息"""
    ticks: list[TickData] = ensure_rpc_client().get_all_ticks()
    return [to_dict(tick) for tick in ticks]


def query_all_orders() -> list:
    """查询全部委托"""
    orders: list[OrderData] = ensure_rpc_client().get_all_orders()
    return [to_dict(order) for order in orders]


def query_all_trades() -> list:
    """查询全部成交"""
    trades: list[TradeData] = ensure_rpc_client().get_all_trades()
    return [to_dict(trade) for trade in trades]


def query_all_positions() -> list:
    """查询全部持仓"""
    positions: list[PositionData] = ensure_rpc_client().get_all_positions()
    return [to_dict(position) for position in positions]


def query_all_accounts() -> list:
    """查询全部账户资金"""
    accounts: list[AccountData] = ensure_rpc_client().get_all_accounts()
    return [to_dict(account) for account in accounts]


def query_all_contracts() -> list:
    """查询全部合约"""
    contracts: list[ContractData] = ensure_rpc_client().get_all_contracts()
    return [to_dict(contract) for contract in contracts]


def place_order(model: OrderRequestModel) -> str:
    """发送委托"""
    req: OrderRequest = OrderRequest(**model.__dict__)

    contract: ContractData | None = query_contract(req.vt_symbol)
    if not contract:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"找不到合约{req.symbol} {req.exchange.value}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    vt_orderid: str = ensure_rpc_client().send_order(req, contract.gateway_name)
    return vt_orderid


def place_order_from_payload(payload: dict) -> str:
    """按字典参数发送委托"""
    model = OrderRequestModel(
        symbol=str(payload["symbol"]),
        exchange=parse_enum_value(Exchange, payload["exchange"]),
        direction=parse_enum_value(Direction, payload["direction"]),
        type=parse_enum_value(OrderType, payload["type"]),
        volume=float(payload["volume"]),
        price=float(payload.get("price", 0)),
        offset=parse_enum_value(Offset, payload.get("offset", "")),
        reference=str(payload.get("reference", "")),
    )
    return place_order(model)


def cancel_existing_order(vt_orderid: str) -> None:
    """撤销委托"""
    order: OrderData | None = ensure_rpc_client().get_order(vt_orderid)
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"找不到委托{vt_orderid}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    req: CancelRequest = order.create_cancel_request()
    ensure_rpc_client().cancel_order(req, order.gateway_name)


def build_history_bar_response(
    vt_symbol: str,
    interval: str,
    start: str,
    end: str | None = None,
    source: str | HistorySource = HistorySource.AUTO,
    limit: int | None = None
) -> dict:
    """构造历史K线查询响应"""
    try:
        interval_key: str = normalize_history_interval(interval)
        start_dt: datetime = parse_query_datetime(start)
        end_dt: datetime = parse_query_datetime(end, is_end=True) if end else datetime.now(DB_TZ)
        symbol, exchange = extract_vt_symbol(vt_symbol)
        source_enum: HistorySource = source if isinstance(source, HistorySource) else HistorySource(str(source))
    except ValueError as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(err),
            headers={"WWW-Authenticate": "Bearer"},
        ) from err

    if limit is not None and limit < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="limit必须大于等于1",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if end_dt < start_dt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="结束时间必须大于或等于开始时间",
            headers={"WWW-Authenticate": "Bearer"},
        )

    history, actual_source = query_history_bars(
        vt_symbol,
        interval_key,
        start_dt,
        end_dt,
        source_enum
    )

    if limit:
        history = history[-limit:]

    return {
        "vt_symbol": vt_symbol,
        "symbol": symbol,
        "exchange": exchange.value,
        "interval": interval_key,
        "source": actual_source,
        "count": len(history),
        "start": str(start_dt),
        "end": str(end_dt),
        "data": [history_bar_to_dict(bar, interval_key) for bar in history],
    }


def mcp_health_check() -> dict:
    """查询MCP服务健康状态"""
    return {
        "rpc_client_started": rpc_client is not None,
        "mcp_enabled": MCP_AUTH_CONFIG.enabled,
        "mcp_enable_trading": MCP_AUTH_CONFIG.enable_trading,
        "mcp_agent_count": len(MCP_AUTH_CONFIG.tokens),
        "supported_history_intervals": SUPPORTED_HISTORY_INTERVALS,
    }


def resolve_mcp_audit_log_path(raw_path: str | None) -> Path | None:
    """解析MCP审计日志路径"""
    if not raw_path:
        return None

    path = Path(raw_path)
    if path.is_absolute():
        return path

    return get_file_path(raw_path)


def start_rpc_client() -> None:
    """启动RPC客户端"""
    global event_loop, rpc_client
    event_loop = asyncio.get_running_loop()
    rpc_client = RpcClient()
    rpc_client.callback = rpc_callback
    rpc_client.subscribe_topic("")
    req_address: str = normalize_rpc_client_address(REQ_ADDRESS)
    sub_address: str = normalize_rpc_client_address(SUB_ADDRESS)
    rpc_client.start(req_address, sub_address)


def stop_rpc_client() -> None:
    """停止RPC客户端"""
    if rpc_client:
        rpc_client.stop()


mcp_audit_logger = McpAuditLogger(resolve_mcp_audit_log_path(MCP_AUDIT_LOG))
mcp_server = create_webtrader_mcp_server(
    WebTraderMcpCallbacks(
        health_check=mcp_health_check,
        get_accounts=query_all_accounts,
        get_positions=query_all_positions,
        get_orders=query_all_orders,
        get_trades=query_all_trades,
        get_contracts=query_all_contracts,
        get_ticks=query_all_ticks,
        subscribe_tick=subscribe_tick,
        get_history_bars=build_history_bar_response,
        place_order=place_order_from_payload,
        cancel_order=cancel_existing_order,
    ),
    MCP_AUTH_CONFIG,
    mcp_audit_logger,
)
mcp_http_app = mcp_server.http_app(path="/mcp", transport="streamable-http")


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    """组合WebTrader RPC和FastMCP HTTP生命周期"""
    start_rpc_client()
    async with mcp_http_app.lifespan(mcp_http_app):
        yield
    stop_rpc_client()


# 创建FastAPI应用
app: FastAPI = FastAPI(lifespan=app_lifespan)
app.add_middleware(McpBearerAuthMiddleware, auth_config=MCP_AUTH_CONFIG)
app.router.routes.extend(mcp_http_app.router.routes)


@app.get("/")
def index() -> HTMLResponse:
    """获取主页面"""
    index_path: Path = Path(__file__).parent.joinpath("static/index.html")
    with open(index_path) as f:
        content: str = f.read()

    return HTMLResponse(content)


@app.post("/token", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends()) -> dict:  # noqa: B008
    """用户登录"""
    auth_result = authenticate_user(USERNAME, form_data.username, form_data.password)
    if not auth_result:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires: timedelta = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token: str = create_access_token(
        data={"sub": auth_result}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}


@app.post("/tick/{vt_symbol}")
def subscribe(vt_symbol: str, access: bool = Depends(get_access)) -> None:
    """订阅行情"""
    subscribe_tick(vt_symbol)


@app.get("/tick")
def get_all_ticks(access: bool = Depends(get_access)) -> list:
    """查询行情信息"""
    return query_all_ticks()


@app.post("/order")
def send_order(model: OrderRequestModel, access: bool = Depends(get_access)) -> str:
    """委托下单"""
    return place_order(model)


@app.delete("/order/{vt_orderid}")
def cancel_order(vt_orderid: str, access: bool = Depends(get_access)) -> None:
    """委托撤单"""
    cancel_existing_order(vt_orderid)


@app.get("/order")
def get_all_orders(access: bool = Depends(get_access)) -> list:
    """查询委托信息"""
    return query_all_orders()


@app.get("/trade")
def get_all_trades(access: bool = Depends(get_access)) -> list:
    """查询成交信息"""
    return query_all_trades()


@app.get("/position")
def get_all_positions(access: bool = Depends(get_access)) -> list:
    """查询持仓信息"""
    return query_all_positions()


@app.get("/account")
def get_all_accounts(access: bool = Depends(get_access)) -> list:
    """查询账户资金"""
    return query_all_accounts()


@app.get("/contract")
def get_all_contracts(access: bool = Depends(get_access)) -> list:
    """查询合约信息"""
    return query_all_contracts()


@app.get("/history/bar/{vt_symbol}")
def get_bar_history(
    vt_symbol: str,
    interval: str,
    start: str,
    end: str | None = None,
    source: HistorySource = Query(HistorySource.AUTO),
    limit: int | None = Query(default=None, ge=1),
    access: bool = Depends(get_access)
) -> dict:
    """查询指定标的历史K线"""
    return build_history_bar_response(vt_symbol, interval, start, end, source, limit)


# 活动状态的Websocket连接
active_websockets: list[WebSocket] = []

# 全局事件循环
event_loop: asyncio.AbstractEventLoop | None = None


async def get_websocket_access(
    websocket: WebSocket,
    token: str | None = Query(None)
) -> bool:
    """Websocket鉴权"""
    credentials_exception: HTTPException = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if token is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        raise credentials_exception
    else:
        payload: dict = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username_value = payload.get("sub")
        if username_value is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            raise credentials_exception
        username: str = username_value
        if not secrets.compare_digest(USERNAME, username):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            raise credentials_exception

    return True


# websocket传递数据
@app.websocket("/ws/")
async def websocket_endpoint(websocket: WebSocket, access: bool = Depends(get_websocket_access)) -> None:
    """Weboskcet连接处理"""
    await websocket.accept()
    active_websockets.append(websocket)

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_websockets.remove(websocket)


async def websocket_broadcast(msg: str) -> None:
    """Websocket数据广播"""
    for websocket in active_websockets:
        await websocket.send_text(msg)


def rpc_callback(topic: str, data: Any) -> None:
    """RPC回调函数"""
    if not active_websockets or not event_loop:
        return

    message_data: dict = {
        "topic": topic,
        "data": to_dict(data)
    }
    msg: str = json.dumps(message_data, ensure_ascii=False)
    asyncio.run_coroutine_threadsafe(websocket_broadcast(msg), event_loop)
