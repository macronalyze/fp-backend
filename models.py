from pydantic import BaseModel


class SearchItem(BaseModel):
    isin: str
    name: str
    nse_symbol: str | None = None
    bse_code: int | None = None


class SearchResponse(BaseModel):
    results: list[SearchItem]
    count: int


class StockEntry(BaseModel):
    date: str
    symbol: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    last: float | None = None
    prev_close: float | None = None
    total_traded_qty: int | None = None
    total_traded_val: float | None = None
    total_trades: int | None = None


class ExchangeData(BaseModel):
    count: int
    data: list[StockEntry]


class StockDataResponse(BaseModel):
    isin: str
    nse: ExchangeData | None = None
    bse: ExchangeData | None = None


class LatestStockResponse(BaseModel):
    isin: str
    nse: StockEntry | None = None
    bse: StockEntry | None = None


class BhavDownloadAccepted(BaseModel):
    message: str
    date: str
