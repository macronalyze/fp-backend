import csv
import io
import logging
import os
import re
import zipfile
from datetime import date, datetime

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pymongo import UpdateOne
from pymongo.errors import PyMongoError

from db import get_db
from models import (
    BhavDownloadAccepted,
    ExchangeData,
    IndustryPerformance,
    LatestStockResponse,
    SearchItem,
    SearchResponse,
    SectorPerformance,
    SectorPerformanceResponse,
    SectorStockPerformance,
    SectorStockPerformanceResponse,
    StockDataResponse,
    StockEntry,
)
from routers._isin_utils import fetch_isin_details

logger = logging.getLogger(__name__)

_NSE_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv.zip"
_BSE_URL = "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{date}_F_0000.CSV"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

router = APIRouter()

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


def _map_entry(entry: dict) -> dict:
    dt = entry["dt"]
    if isinstance(dt, datetime):
        dt = dt.strftime("%Y-%m-%d")
    return {
        "date": dt,
        "symbol": entry["sym"],
        "open": entry["o"],
        "high": entry["h"],
        "low": entry["l"],
        "close": entry["c"],
        "last": entry["la"],
        "prev_close": entry["pc"],
        "total_traded_qty": entry["tq"],
        "total_traded_val": entry["tv"],
        "total_trades": entry["tt"],
    }


# ── Search ──────────────────────────────────────────────────────────────────


@router.get("/search", response_model=SearchResponse)
def search(
    q: str = Query(..., min_length=2),
    limit: int = Query(10, ge=1, le=50),
):
    db = get_db()
    regex = {"$regex": f".*{re.escape(q)}.*", "$options": "i"}
    or_conditions: list[dict] = [{"COMPANY_NAME": regex}, {"NSE_SYMBOL": regex}]
    if q.isdigit():
        or_conditions.append({"BSE_CODE": int(q)})
    cursor = (
        db["isin"]
        .find(
            {"$or": or_conditions},
            {
                "_id": 1,
                "COMPANY_NAME": 1,
                "NSE_SYMBOL": 1,
                "BSE_CODE": 1,
                "industry": 1,
                "sector": 1,
                "free_float_shares": 1,
            },
        )
        .limit(limit)
    )
    results = [
        SearchItem(
            isin=doc["_id"],
            name=doc["COMPANY_NAME"],
            nse_symbol=doc.get("NSE_SYMBOL"),
            bse_code=doc.get("BSE_CODE"),
            industry=doc.get("industry"),
            sector=doc.get("sector"),
            free_float_shares=doc.get("free_float_shares"),
        )
        for doc in cursor
    ]
    return SearchResponse(results=results, count=len(results))


# ── Historical data ─────────────────────────────────────────────────────────


@router.get("/stocks/{isin}", response_model=StockDataResponse)
def get_stock_data(
    isin: str,
    start_date: str | None = None,
    end_date: str | None = None,
):
    if not _ISIN_RE.match(isin):
        logger.warning(f"Invalid ISIN format in request: {isin}")
        raise HTTPException(400, "Invalid ISIN format")

    try:
        start = datetime.strptime(start_date, "%Y-%m-%d") if start_date else None
        end = datetime.strptime(end_date, "%Y-%m-%d") if end_date else None
    except ValueError:
        raise HTTPException(400, "Invalid date format, use YYYY-MM-DD")

    start_month = start.strftime("%Y-%m") if start else "0000-00"
    end_month = end.strftime("%Y-%m") if end else "9999-99"

    pipeline: list[dict] = [
        {"$match": {"_id": {"$gte": f"{isin}_{start_month}", "$lte": f"{isin}_{end_month}"}}},
        {"$unwind": "$d"},
    ]

    dt_filter: dict = {}
    if start:
        dt_filter["d.dt"] = {"$gte": start}
    if end:
        dt_filter.setdefault("d.dt", {})["$lte"] = end
    if dt_filter:
        pipeline.append({"$match": dt_filter})

    pipeline.append({"$sort": {"d.dt": 1}})
    pipeline.append({"$replaceRoot": {"newRoot": "$d"}})

    db = get_db()
    results = list(db["raw_bhav_data_v3"].aggregate(pipeline))

    if not results:
        raise HTTPException(404, f"No data found for ISIN {isin}")

    nse_data = [_map_entry(doc) for doc in results if doc["ex"] == "nse"]
    bse_data = [_map_entry(doc) for doc in results if doc["ex"] == "bse"]

    return StockDataResponse(
        isin=isin,
        **fetch_isin_details(isin),
        nse=ExchangeData(count=len(nse_data), data=nse_data) if nse_data else None,
        bse=ExchangeData(count=len(bse_data), data=bse_data) if bse_data else None,
    )


# ── Latest ──────────────────────────────────────────────────────────────────


@router.get("/stocks/{isin}/latest", response_model=LatestStockResponse)
def get_latest_stock(
    isin: str,
):
    if not _ISIN_RE.match(isin):
        logger.warning(f"Invalid ISIN format in request: {isin}")
        raise HTTPException(400, "Invalid ISIN format")

    db = get_db()
    doc = db["raw_bhav_data_v3"].find_one(
        {"_id": {"$gte": f"{isin}_", "$lte": f"{isin}_~"}},
        sort=[("_id", -1)],
    )

    if not doc or not doc.get("d"):
        raise HTTPException(404, f"No data found for ISIN {isin}")

    entries = doc["d"]
    entries.sort(key=lambda e: e["dt"], reverse=True)

    nse_entry = next((e for e in entries if e["ex"] == "nse"), None)
    bse_entry = next((e for e in entries if e["ex"] == "bse"), None)

    if not nse_entry and not bse_entry:
        raise HTTPException(404, f"No data found for ISIN {isin}")

    return LatestStockResponse(
        isin=isin,
        **fetch_isin_details(isin),
        nse=StockEntry(**_map_entry(nse_entry)) if nse_entry else None,
        bse=StockEntry(**_map_entry(bse_entry)) if bse_entry else None,
    )


# ── Sector / Industry Performance ───────────────────────────────────────────


@router.get("/sector-performance", response_model=SectorPerformanceResponse)
def get_sector_performance(
    start_date: str = Query(..., description="YYYY-MM-DD"),
    end_date: str = Query(..., description="YYYY-MM-DD"),
):
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Invalid date format, use YYYY-MM-DD")
    if start > end:
        raise HTTPException(400, "start_date must be on or before end_date")

    db = get_db()

    # ── 1. Preload ISIN → (sector, industry, free_float_shares) map ─────────
    # Tiny (~1 MB for ~5k ISINs). free_float_shares lets us derive mcap from
    # price for both NSE and BSE (the `mcap` collection is NSE-only).
    isin_meta: dict[str, tuple[str, str, int]] = {}
    for doc in db["isin"].find(
        {},
        {"_id": 1, "sector": 1, "industry": 1, "free_float_shares": 1},
    ):
        ff = doc.get("free_float_shares")
        if not ff or ff <= 0:
            continue
        sector = (doc.get("sector") or "").strip() or "Unknown"
        industry = (doc.get("industry") or "").strip() or "Unknown"
        isin_meta[doc["_id"]] = (sector, industry, ff)

    # ── 2. Mongo aggregation: first/last price per (isin, exchange) ─────────
    months: list[str] = []
    cur = datetime(start.year, start.month, 1)
    last_month = datetime(end.year, end.month, 1)
    while cur <= last_month:
        months.append(cur.strftime("%Y-%m"))
        cur = datetime(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)
    month_re = "|".join(re.escape(m) for m in months)

    pipeline = [
        {"$match": {"_id": {"$regex": f"_({month_re})$"}}},
        {"$unwind": "$d"},
        {"$match": {"d.dt": {"$gte": start, "$lte": end}}},
        {"$sort": {"i": 1, "d.ex": 1, "d.dt": 1}},
        {
            "$group": {
                "_id": {"i": "$i", "ex": "$d.ex"},
                "open": {"$first": "$d.o"},
                "close": {"$last": "$d.c"},
            }
        },
        {"$match": {"open": {"$gt": 0}, "close": {"$gt": 0}}},
    ]
    cursor = db["raw_bhav_data_v3"].aggregate(pipeline, allowDiskUse=True, batchSize=500)

    # ── 3. Pick NSE in preference to BSE, per ISIN ──────────────────────────
    chosen: dict[str, tuple[float, float]] = {}  # isin -> (open, close)
    chosen_ex: dict[str, str] = {}
    for row in cursor:
        isin = row["_id"]["i"]
        ex = row["_id"]["ex"]
        existing = chosen_ex.get(isin)
        if existing == "nse":
            continue
        if existing == "bse" and ex != "nse":
            continue
        chosen[isin] = (row["open"], row["close"])
        chosen_ex[isin] = ex

    if not chosen:
        raise HTTPException(404, "No stock data found in the given date range")

    # ── 4. Compute mcap growth pct per stock, accumulate per industry ───────
    # mcap = price * free_float_shares. Since ff_shares is constant across the
    # window, the ratio simplifies to (close_price/open_price) - 1, matching
    # price-based growth. Computing mcap explicitly keeps the semantics clear
    # and lets us plug in time-varying share counts later if needed.
    ind_acc: dict[tuple[str, str], list[float]] = {}
    for isin, (open_p, close_p) in chosen.items():
        meta = isin_meta.get(isin)
        if not meta:
            continue
        sector, industry, ff = meta
        open_mcap = open_p * ff
        close_mcap = close_p * ff
        pct = (close_mcap - open_mcap) / open_mcap * 100.0
        bucket = ind_acc.setdefault((sector, industry), [0.0, 0])
        bucket[0] += pct
        bucket[1] += 1

    if not ind_acc:
        raise HTTPException(404, "No stock data found in the given date range")

    # ── 5. Industry means, then sector mean over industry means ─────────────
    # sector_map[sector] = list[IndustryPerformance]
    sector_map: dict[str, list[IndustryPerformance]] = {}
    total_stocks = 0
    for (sector, industry), (sum_pct, count) in ind_acc.items():
        ind_mean = sum_pct / count
        sector_map.setdefault(sector, []).append(
            IndustryPerformance(
                industry=industry,
                growth_pct=round(ind_mean, 4),
                stock_count=count,
            )
        )
        total_stocks += count

    sectors: list[SectorPerformance] = []
    for sector, industries in sector_map.items():
        sector_mean = sum(i.growth_pct for i in industries) / len(industries)
        industries.sort(key=lambda i: i.growth_pct, reverse=True)
        sectors.append(
            SectorPerformance(
                sector=sector,
                growth_pct=round(sector_mean, 4),
                industry_count=len(industries),
                stock_count=sum(i.stock_count for i in industries),
                industries=industries,
            )
        )
    sectors.sort(key=lambda s: s.growth_pct, reverse=True)

    return SectorPerformanceResponse(
        start_date=start_date,
        end_date=end_date,
        stock_count=total_stocks,
        sectors=sectors,
    )


@router.get("/sector-performance/stocks-by-industries", response_model=SectorStockPerformanceResponse)
def get_sector_stocks_performance(
    sector: str = Query(..., min_length=1),
    industry: str = Query(..., min_length=1),
    start_date: str = Query(..., description="YYYY-MM-DD"),
    end_date: str = Query(..., description="YYYY-MM-DD"),
):
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Invalid date format, use YYYY-MM-DD")
    if start > end:
        raise HTTPException(400, "start_date must be on or before end_date")

    db = get_db()

    # ── 1. Resolve target ISINs from sector + industry filter ───────────────
    target_meta: dict[str, dict] = {}
    for doc in db["isin"].find(
        {"sector": sector, "industry": industry},
        {
            "_id": 1,
            "COMPANY_NAME": 1,
            "NSE_SYMBOL": 1,
            "BSE_CODE": 1,
            "industry": 1,
            "free_float_shares": 1,
        },
    ):
        ff = doc.get("free_float_shares")
        if not ff or ff <= 0:
            continue
        target_meta[doc["_id"]] = {
            "name": doc.get("COMPANY_NAME"),
            "nse_symbol": doc.get("NSE_SYMBOL"),
            "bse_code": doc.get("BSE_CODE"),
            "industry": doc.get("industry") or industry,
            "ff": ff,
        }

    if not target_meta:
        raise HTTPException(
            404,
            f"No stocks found for sector '{sector}' industry '{industry}'",
        )

    # ── 2. Build exact _id list (isin × month) for indexed $in match ────────
    months: list[str] = []
    cur = datetime(start.year, start.month, 1)
    last_month = datetime(end.year, end.month, 1)
    while cur <= last_month:
        months.append(cur.strftime("%Y-%m"))
        cur = datetime(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)
    doc_ids = [f"{isin}_{m}" for isin in target_meta for m in months]

    # ── 3. Mongo aggregation: first/last price per (isin, exchange) ─────────
    pipeline = [
        {"$match": {"_id": {"$in": doc_ids}}},
        {"$unwind": "$d"},
        {"$match": {"d.dt": {"$gte": start, "$lte": end}}},
        {"$sort": {"i": 1, "d.ex": 1, "d.dt": 1}},
        {
            "$group": {
                "_id": {"i": "$i", "ex": "$d.ex"},
                "open": {"$first": "$d.o"},
                "close": {"$last": "$d.c"},
            }
        },
        {"$match": {"open": {"$gt": 0}, "close": {"$gt": 0}}},
    ]
    cursor = db["raw_bhav_data_v3"].aggregate(pipeline, allowDiskUse=True, batchSize=500)

    # ── 4. NSE > BSE preference per ISIN ────────────────────────────────────
    chosen: dict[str, tuple[str, float, float]] = {}  # isin -> (ex, open, close)
    for row in cursor:
        isin = row["_id"]["i"]
        ex = row["_id"]["ex"]
        existing = chosen.get(isin)
        if existing and existing[0] == "nse":
            continue
        if existing and existing[0] == "bse" and ex != "nse":
            continue
        chosen[isin] = (ex, row["open"], row["close"])

    if not chosen:
        raise HTTPException(404, "No stock data found in the given date range")

    # ── 5. Build per-stock entries with mcap & growth pct ───────────────────
    stocks: list[SectorStockPerformance] = []
    for isin, (ex, open_p, close_p) in chosen.items():
        meta = target_meta[isin]
        ff = meta["ff"]
        open_mcap = open_p * ff
        close_mcap = close_p * ff
        pct = (close_mcap - open_mcap) / open_mcap * 100.0
        stocks.append(
            SectorStockPerformance(
                isin=isin,
                name=meta["name"],
                nse_symbol=meta["nse_symbol"],
                bse_code=meta["bse_code"],
                industry=meta["industry"],
                exchange=ex,
                open_price=round(open_p, 4),
                close_price=round(close_p, 4),
                open_mcap=round(open_mcap, 2),
                close_mcap=round(close_mcap, 2),
                growth_pct=round(pct, 4),
            )
        )

    stocks.sort(key=lambda s: s.growth_pct, reverse=True)

    return SectorStockPerformanceResponse(
        sector=sector,
        industry=industry,
        start_date=start_date,
        end_date=end_date,
        stock_count=len(stocks),
        stocks=stocks,
    )


# ── Bhav Download ───────────────────────────────────────────────────────────


def _notify_slack(message: str):
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("SLACK_WEBHOOK_URL not set, skipping notification")
        return
    try:
        httpx.post(webhook_url, json={"text": message}, timeout=10)
    except Exception as e:
        logger.error(f"Slack notification failed: {e}")


def _parse_float(val: str) -> float | None:
    try:
        return float(val) if val.strip() else None
    except (ValueError, TypeError):
        return None


def _parse_int(val: str) -> int | None:
    try:
        return int(float(val)) if val.strip() else None
    except (ValueError, TypeError):
        return None


def _download_nse_csv(target_date: date) -> str | None:
    url = _NSE_URL.format(date=target_date.strftime("%Y%m%d"))
    logger.info(f"Downloading NSE bhav: {url}")
    try:
        resp = httpx.get(url, headers=_HEADERS, follow_redirects=True, timeout=30)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.error(f"NSE download failed: {e}")
        return None
    logger.info(f"NSE download complete, size={len(resp.content)} bytes")
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        csv_name = next((n for n in zf.namelist() if n.endswith(".csv")), None)
        if not csv_name:
            logger.error("No CSV found in NSE zip")
            return None
        logger.info(f"Extracted CSV from zip: {csv_name}")
        return zf.read(csv_name).decode("utf-8")


def _download_bse_csv(target_date: date) -> str | None:
    url = _BSE_URL.format(date=target_date.strftime("%Y%m%d"))
    logger.info(f"Downloading BSE bhav: {url}")
    try:
        resp = httpx.get(url, headers=_HEADERS, follow_redirects=True, timeout=30)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.error(f"BSE download failed: {e}")
        return None
    logger.info(f"BSE download complete, size={len(resp.content)} bytes")
    return resp.text


def _parse_and_upsert(csv_text: str, exchange: str) -> tuple[int, list[str]]:
    """Parse CSV text and upsert into DB. Returns (row_count, new_isins)."""
    db = get_db()
    collection = db["raw_bhav_data_v3"]
    isin_collection = db["isin"]

    existing_isins = set(
        doc["_id"] for doc in isin_collection.find({}, {"_id": 1})
    )

    reader = csv.DictReader(io.StringIO(csv_text))
    new_isins = set()
    entries_by_doc: dict[str, list[dict]] = {}
    total_rows = 0
    skipped_invalid_isin = 0
    skipped_row_errors = 0

    for row in reader:
        total_rows += 1
        try:
            isin = row.get("ISIN", "").strip()
            if not isin or not isin.startswith("INE"):
                skipped_invalid_isin += 1
                continue

            trade_dt_raw = row.get("TradDt", "").strip()
            if not trade_dt_raw:
                raise ValueError("TradDt is empty")
            trade_dt = datetime.strptime(trade_dt_raw, "%Y-%m-%d")
            doc_id = f"{isin}_{trade_dt.strftime('%Y-%m')}"

            entry = {
                "dt": trade_dt,
                "ex": exchange,
                "sym": row.get("TckrSymb", "").strip(),
                "o": _parse_float(row.get("OpnPric", "")),
                "h": _parse_float(row.get("HghPric", "")),
                "l": _parse_float(row.get("LwPric", "")),
                "c": _parse_float(row.get("ClsPric", "")),
                "la": _parse_float(row.get("LastPric", "")),
                "pc": _parse_float(row.get("PrvsClsgPric", "")),
                "tq": _parse_int(row.get("TtlTradgVol", "")),
                "tv": _parse_float(row.get("TtlTrfVal", "")),
                "tt": _parse_int(row.get("TtlNbOfTxsExctd", "")),
                "sr": row.get("SctySrs", "").strip() or None,
            }
            if exchange == "bse":
                sc = row.get("FinInstrmId", "").strip()
                if sc:
                    entry["sc"] = sc

            entries_by_doc.setdefault(doc_id, []).append(entry)

            if isin not in existing_isins:
                new_isins.add(isin)
        except Exception as e:
            skipped_row_errors += 1
            if skipped_row_errors <= 5:
                logger.warning(
                    f"Skipping malformed {exchange} row: err={e}, "
                    f"isin={row.get('ISIN')}, sym={row.get('TckrSymb')}, tradDt={row.get('TradDt')}"
                )
            continue

    if not entries_by_doc:
        logger.warning(
            f"No INE records found for {exchange}. "
            f"total_rows={total_rows}, skipped_invalid_isin={skipped_invalid_isin}, skipped_row_errors={skipped_row_errors}"
        )
        return 0, []

    count = sum(len(entries) for entries in entries_by_doc.values())
    logger.info(f"Parsed {count} records for {exchange} across {len(entries_by_doc)} documents")
    if skipped_invalid_isin or skipped_row_errors:
        logger.info(
            f"{exchange} parse summary: total_rows={total_rows}, "
            f"skipped_invalid_isin={skipped_invalid_isin}, skipped_row_errors={skipped_row_errors}"
        )

    # Bulk push: add all entries grouped by doc
    push_ops = [
        UpdateOne(
            {"_id": doc_id},
            {
                "$push": {"d": {"$each": entries}},
                "$setOnInsert": {"i": doc_id.rsplit("_", 1)[0]},
            },
            upsert=True,
        )
        for doc_id, entries in entries_by_doc.items()
    ]
    try:
        result = collection.bulk_write(push_ops, ordered=False)
    except PyMongoError:
        logger.exception(f"DB write failed for {exchange}")
        raise
    logger.info(
        f"DB write complete for {exchange}: "
        f"matched={result.matched_count}, upserted={result.upserted_count}, modified={result.modified_count}"
    )

    if new_isins:
        logger.info(f"New ISINs detected for {exchange}: {len(new_isins)}")

    return count, sorted(new_isins)


def _run_bhav_download(target_date: date):
    date_str = target_date.strftime("%Y-%m-%d")
    logger.info(f"=== Bhav download started for {date_str} ===")
    _notify_slack(f"⏳ Starting bhav download for *{date_str}*")

    errors = []
    nse_count = 0
    bse_count = 0
    all_new_isins = []

    # NSE
    nse_csv = _download_nse_csv(target_date)
    if nse_csv:
        try:
            nse_count, nse_new = _parse_and_upsert(nse_csv, "nse")
            all_new_isins.extend(nse_new)
        except Exception as e:
            logger.exception("NSE parse/upsert failed")
            errors.append(f"NSE parse/upsert failed: {e}")
    else:
        errors.append("NSE download failed")

    # BSE
    bse_csv = _download_bse_csv(target_date)
    if bse_csv:
        try:
            bse_count, bse_new = _parse_and_upsert(bse_csv, "bse")
            all_new_isins.extend(i for i in bse_new if i not in all_new_isins)
        except Exception as e:
            logger.exception("BSE parse/upsert failed")
            errors.append(f"BSE parse/upsert failed: {e}")
    else:
        errors.append("BSE download failed")

    # Slack summary
    logger.info(f"=== Bhav download finished for {date_str}: NSE={nse_count}, BSE={bse_count}, errors={errors} ===")
    if errors and nse_count == 0 and bse_count == 0:
        _notify_slack(f"❌ Bhav download failed for *{date_str}*\nErrors: {', '.join(errors)}")
    else:
        parts = [f"✅ Bhav download complete for *{date_str}*"]
        parts.append(f"• NSE: {nse_count} records")
        parts.append(f"• BSE: {bse_count} records")
        if errors:
            parts.append(f"• Partial failure: {', '.join(errors)}")
        if all_new_isins:
            parts.append(f"• New ISINs ({len(all_new_isins)}): {', '.join(all_new_isins[:20])}")
            if len(all_new_isins) > 20:
                parts.append(f"  ... and {len(all_new_isins) - 20} more")
        _notify_slack("\n".join(parts))


@router.post("/bhav/download", response_model=BhavDownloadAccepted, status_code=202)
def download_bhav(
    background_tasks: BackgroundTasks,
    target_date: date | None = Query(None, alias="date"),
):
    if target_date is None:
        target_date = date.today()

    logger.info(f"Bhav download requested for date={target_date.isoformat()}")
    background_tasks.add_task(_run_bhav_download, target_date)

    return BhavDownloadAccepted(
        message="Bhav download started",
        date=target_date.isoformat(),
    )
