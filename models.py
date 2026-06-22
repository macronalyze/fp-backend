from pydantic import BaseModel


class SearchItem(BaseModel):
    isin: str
    name: str
    nse_symbol: str | None = None
    bse_code: int | None = None
    industry: str | None = None
    sector: str | None = None
    free_float_shares: int | None = None


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
    name: str | None = None
    nse_symbol: str | None = None
    bse_code: int | None = None
    industry: str | None = None
    sector: str | None = None
    free_float_shares: int | None = None
    nse: ExchangeData | None = None
    bse: ExchangeData | None = None


class LatestStockResponse(BaseModel):
    isin: str
    name: str | None = None
    nse_symbol: str | None = None
    bse_code: int | None = None
    industry: str | None = None
    sector: str | None = None
    free_float_shares: int | None = None
    nse: StockEntry | None = None
    bse: StockEntry | None = None


class BhavDownloadAccepted(BaseModel):
    message: str
    date: str


class IndustryPerformance(BaseModel):
    industry: str
    growth_pct: float
    stock_count: int


class SectorPerformance(BaseModel):
    sector: str
    growth_pct: float
    industry_count: int
    stock_count: int
    industries: list[IndustryPerformance]


class SectorPerformanceResponse(BaseModel):
    start_date: str
    end_date: str
    stock_count: int
    sectors: list[SectorPerformance]


class SectorStockPerformance(BaseModel):
    isin: str
    name: str | None = None
    nse_symbol: str | None = None
    bse_code: int | None = None
    industry: str
    exchange: str
    open_price: float
    close_price: float
    open_mcap: float
    close_mcap: float
    growth_pct: float


class SectorStockPerformanceResponse(BaseModel):
    sector: str
    industry: str
    start_date: str
    end_date: str
    stock_count: int
    stocks: list[SectorStockPerformance]


class McapEntry(BaseModel):
    date: str
    face_value: float | None = None
    issue_size: int | None = None
    market_cap: float | None = None


class McapDataResponse(BaseModel):
    isin: str
    name: str | None = None
    nse_symbol: str | None = None
    bse_code: int | None = None
    industry: str | None = None
    sector: str | None = None
    free_float_shares: int | None = None
    source: str
    count: int
    data: list[McapEntry]


class LatestMcapResponse(BaseModel):
    isin: str
    name: str | None = None
    nse_symbol: str | None = None
    bse_code: int | None = None
    industry: str | None = None
    sector: str | None = None
    free_float_shares: int | None = None
    source: str
    entry: McapEntry | None = None


# ── Datasets ────────────────────────────────────────────────────────────────


class Sector(BaseModel):
    id: str
    name: str
    weight: float | None = None


class MonthlyEntry(BaseModel):
    period: str
    label: str
    provisional: bool | None = None
    index: dict[str, float] | None = None
    growth: dict[str, float] | None = None
    values: dict[str, float] | None = None


class YearlyEntry(BaseModel):
    year: str
    provisional: bool | None = None
    index: dict[str, float] | None = None
    growth: dict[str, float] | None = None
    values: dict[str, float] | None = None


class DatasetSummary(BaseModel):
    id: str
    name: str
    shortName: str
    icon: str
    latestPeriod: str | None = None
    latestGrowth: float | None = None
    cumulativeGrowth: float | None = None
    cumulativePeriod: str | None = None
    status: str | None = None
    releaseDate: str | None = None


class CountryDatasets(BaseModel):
    country: str
    countryName: str
    datasets: list[DatasetSummary]


class DatasetDetail(BaseModel):
    id: str
    name: str
    shortName: str
    country: str
    baseYear: str | None = None
    baseValue: float | None = None
    source: str | None = None
    releaseDate: str | None = None
    nextRelease: str | None = None
    description: str | None = None
    sectors: list[Sector] = []
    commodities: list[str] = []
    monthly: list[MonthlyEntry] = []
    yearly: list[YearlyEntry] = []
