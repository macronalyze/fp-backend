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

from db import get_db
from models import (
    BhavDownloadAccepted,
    ExchangeData,
    LatestStockResponse,
    SearchItem,
    SearchResponse,
    StockDataResponse,
    StockEntry,
)

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
    cursor = (
        db["isin"]
        .find(
            {"COMPANY_NAME": regex},
            {"_id": 1, "COMPANY_NAME": 1, "NSE_SYMBOL": 1, "BSE_CODE": 1},
        )
        .limit(limit)
    )
    results = [
        SearchItem(
            isin=doc["_id"],
            name=doc["COMPANY_NAME"],
            nse_symbol=doc.get("NSE_SYMBOL"),
            bse_code=doc.get("BSE_CODE"),
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
        nse=ExchangeData(count=len(nse_data), data=nse_data) if nse_data else None,
        bse=ExchangeData(count=len(bse_data), data=bse_data) if bse_data else None,
    )


# ── Latest ──────────────────────────────────────────────────────────────────


@router.get("/stocks/{isin}/latest", response_model=LatestStockResponse)
def get_latest_stock(
    isin: str,
):
    if not _ISIN_RE.match(isin):
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
        nse=StockEntry(**_map_entry(nse_entry)) if nse_entry else None,
        bse=StockEntry(**_map_entry(bse_entry)) if bse_entry else None,
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

    for row in reader:
        isin = row.get("ISIN", "").strip()
        if not isin or not isin.startswith("INE"):
            continue

        trade_dt = datetime.strptime(row["TradDt"].strip(), "%Y-%m-%d")
        doc_id = f"{isin}_{trade_dt.strftime('%Y-%m')}"

        entry = {
            "dt": trade_dt,
            "ex": exchange,
            "sym": row["TckrSymb"].strip(),
            "o": _parse_float(row["OpnPric"]),
            "h": _parse_float(row["HghPric"]),
            "l": _parse_float(row["LwPric"]),
            "c": _parse_float(row["ClsPric"]),
            "la": _parse_float(row["LastPric"]),
            "pc": _parse_float(row["PrvsClsgPric"]),
            "tq": _parse_int(row["TtlTradgVol"]),
            "tv": _parse_float(row["TtlTrfVal"]),
            "tt": _parse_int(row["TtlNbOfTxsExctd"]),
            "sr": row.get("SctySrs", "").strip() or None,
        }
        if exchange == "bse":
            sc = row.get("FinInstrmId", "").strip()
            if sc:
                entry["sc"] = sc

        entries_by_doc.setdefault(doc_id, []).append(entry)

        if isin not in existing_isins:
            new_isins.add(isin)

    if not entries_by_doc:
        logger.warning(f"No INE records found for {exchange}")
        return 0, []

    count = sum(len(entries) for entries in entries_by_doc.values())
    logger.info(f"Parsed {count} records for {exchange} across {len(entries_by_doc)} documents")

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
    result = collection.bulk_write(push_ops, ordered=False)
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
        nse_count, nse_new = _parse_and_upsert(nse_csv, "nse")
        all_new_isins.extend(nse_new)
    else:
        errors.append("NSE download failed")

    # BSE
    bse_csv = _download_bse_csv(target_date)
    if bse_csv:
        bse_count, bse_new = _parse_and_upsert(bse_csv, "bse")
        all_new_isins.extend(i for i in bse_new if i not in all_new_isins)
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
