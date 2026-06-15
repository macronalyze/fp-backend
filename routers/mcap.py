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
    LatestMcapResponse,
    McapDataResponse,
    McapDownloadAccepted,
    McapEntry,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
_MCAP_COLLECTION = "mcap"
_SOURCE = "NSE"

_PR_URL = "https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{ddmmyy}.zip"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}


# ── Helpers ─────────────────────────────────────────────────────────────────


def _date_from_id(doc_id: str) -> str:
    """`{isin}_{YYYY-MM-DD}` -> `YYYY-MM-DD`."""
    return doc_id.rsplit("_", 1)[-1]


def _date_to_str(val) -> str:
    """Coerce a stored trade_date (str or datetime) to `YYYY-MM-DD`."""
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d")
    return str(val) if val is not None else ""


def _doc_to_entry(doc: dict) -> McapEntry:
    return McapEntry(
        date=_date_to_str(doc.get("trade_date")) or _date_from_id(doc["_id"]),
        face_value=doc.get("face_value"),
        issue_size=doc.get("issue_size"),
        market_cap=doc.get("market_cap"),
    )


def _parse_float(val) -> float | None:
    if val is None:
        return None
    try:
        s = str(val).strip().replace(",", "")
        return float(s) if s else None
    except (ValueError, TypeError):
        return None


def _parse_int(val) -> int | None:
    if val is None:
        return None
    try:
        s = str(val).strip().replace(",", "")
        return int(float(s)) if s else None
    except (ValueError, TypeError):
        return None


def _parse_trade_date(val: str | None) -> str | None:
    """`12 JUN 2026` -> `2026-06-12`."""
    s = (val or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d %b %Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _normalize_keys(row: dict) -> dict:
    return {(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}


# ── Read endpoints ──────────────────────────────────────────────────────────


@router.get("/mcap/{isin}", response_model=McapDataResponse)
def get_mcap_data(
    isin: str,
    start_date: str | None = None,
    end_date: str | None = None,
):
    if not _ISIN_RE.match(isin):
        raise HTTPException(400, "Invalid ISIN format")

    # Validate dates if provided.
    for d in (start_date, end_date):
        if d:
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(400, "Invalid date format, use YYYY-MM-DD")

    # _id is `{isin}_{YYYY-MM-DD}` so a lexicographic range over _id is the
    # natural date range filter — no separate trade_date index needed.
    lo = f"{isin}_{start_date or '0000-00-00'}"
    hi = f"{isin}_{end_date or '9999-99-99'}"

    db = get_db()
    cursor = db[_MCAP_COLLECTION].find(
        {"_id": {"$gte": lo, "$lte": hi}},
    ).sort("_id", 1)

    entries = [_doc_to_entry(doc) for doc in cursor]
    if not entries:
        raise HTTPException(404, f"No market cap data found for ISIN {isin}")

    return McapDataResponse(
        isin=isin,
        source=_SOURCE,
        count=len(entries),
        data=entries,
    )


@router.get("/mcap/{isin}/latest", response_model=LatestMcapResponse)
def get_latest_mcap(isin: str):
    if not _ISIN_RE.match(isin):
        raise HTTPException(400, "Invalid ISIN format")

    db = get_db()
    doc = db[_MCAP_COLLECTION].find_one(
        {"_id": {"$gte": f"{isin}_", "$lte": f"{isin}_~"}},
        sort=[("_id", -1)],
    )
    if not doc:
        raise HTTPException(404, f"No market cap data found for ISIN {isin}")

    return LatestMcapResponse(
        isin=isin,
        source=_SOURCE,
        entry=_doc_to_entry(doc),
    )


# ── Daily download ──────────────────────────────────────────────────────────


def _notify_slack(message: str):
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("SLACK_WEBHOOK_URL not set, skipping notification")
        return
    try:
        httpx.post(webhook_url, json={"text": message}, timeout=10)
    except Exception as e:
        logger.error(f"Slack notification failed: {e}")


def _download_pr_zip(target_date: date) -> bytes | None:
    url = _PR_URL.format(ddmmyy=target_date.strftime("%d%m%y"))
    logger.info(f"Downloading PR zip: {url}")
    try:
        resp = httpx.get(url, headers=_HEADERS, follow_redirects=True, timeout=60)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.error(f"PR download failed: {e}")
        return None
    logger.info(f"PR download complete, size={len(resp.content)} bytes")
    return resp.content


def _extract_mcap_csv(zip_bytes: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            mcap_name = next(
                (n for n in zf.namelist() if n.lower().startswith("mcap") and n.lower().endswith(".csv")),
                None,
            )
            if not mcap_name:
                logger.error(f"No mcap*.csv in PR zip. Members: {zf.namelist()}")
                return None
            logger.info(f"Extracted from zip: {mcap_name}")
            return zf.read(mcap_name).decode("utf-8", errors="replace")
    except zipfile.BadZipFile as e:
        logger.error(f"Bad zip file: {e}")
        return None


def _load_nse_isin_lookup(db) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for doc in db["isin"].find({}, {"_id": 1, "NSE_SYMBOL": 1}):
        sym = (doc.get("NSE_SYMBOL") or "").strip().upper()
        if sym:
            lookup[sym] = doc["_id"]
    logger.info(f"Loaded {len(lookup)} NSE_SYMBOL -> ISIN mappings")
    return lookup


def _parse_and_upsert_mcap(csv_text: str) -> tuple[int, int, int]:
    """Returns (matched, skipped_non_eq, skipped_no_isin)."""
    db = get_db()
    isin_lookup = _load_nse_isin_lookup(db)

    reader = csv.DictReader(io.StringIO(csv_text))
    ops: list[UpdateOne] = []
    matched = 0
    skipped_non_eq = 0
    skipped_no_isin = 0

    for raw in reader:
        row = _normalize_keys(raw)
        series = (row.get("Series") or "").upper()
        if series != "EQ":
            skipped_non_eq += 1
            continue

        td = _parse_trade_date(row.get("Trade Date"))
        if not td:
            continue

        symbol = (row.get("Symbol") or "").upper()
        isin = isin_lookup.get(symbol)
        if not isin:
            skipped_no_isin += 1
            continue

        doc_id = f"{isin}_{td}"
        ops.append(
            UpdateOne(
                {"_id": doc_id},
                {
                    "$set": {
                        "isin": isin,
                        "source": _SOURCE,
                        "trade_date": td,
                        "face_value": _parse_float(row.get("Face Value(Rs.)")),
                        "issue_size": _parse_int(row.get("Issue Size")),
                        "market_cap": _parse_float(row.get("Market Cap(Rs.)")),
                    }
                },
                upsert=True,
            )
        )
        matched += 1

    if not ops:
        logger.warning("No EQ rows with mapped ISINs found in mcap csv")
        return 0, skipped_non_eq, skipped_no_isin

    result = db[_MCAP_COLLECTION].bulk_write(ops, ordered=False)
    logger.info(
        f"Mcap DB write complete: matched={result.matched_count} "
        f"upserted={result.upserted_count} modified={result.modified_count}"
    )
    return matched, skipped_non_eq, skipped_no_isin


def _run_mcap_download(target_date: date):
    date_str = target_date.strftime("%Y-%m-%d")
    logger.info(f"=== Mcap download started for {date_str} ===")
    _notify_slack(f"⏳ Starting mcap download for *{date_str}*")

    zip_bytes = _download_pr_zip(target_date)
    if not zip_bytes:
        _notify_slack(f"❌ Mcap download failed for *{date_str}* (download error)")
        return

    csv_text = _extract_mcap_csv(zip_bytes)
    if not csv_text:
        _notify_slack(f"❌ Mcap download failed for *{date_str}* (no mcap csv in zip)")
        return

    try:
        matched, non_eq, no_isin = _parse_and_upsert_mcap(csv_text)
    except Exception as e:
        logger.exception("Mcap ingest failed")
        _notify_slack(f"❌ Mcap ingest failed for *{date_str}*: {e}")
        return

    logger.info(
        f"=== Mcap download finished for {date_str}: "
        f"matched={matched} non_eq_skipped={non_eq} no_isin_skipped={no_isin} ==="
    )
    parts = [f"✅ Mcap download complete for *{date_str}*"]
    parts.append(f"• Records upserted: {matched}")
    parts.append(f"• Skipped (non-EQ): {non_eq}")
    if no_isin:
        parts.append(f"• Skipped (no ISIN match): {no_isin}")
    _notify_slack("\n".join(parts))


@router.post("/mcap/download", response_model=McapDownloadAccepted, status_code=202)
def download_mcap(
    background_tasks: BackgroundTasks,
    target_date: date | None = Query(None, alias="date"),
):
    if target_date is None:
        target_date = date.today()

    logger.info(f"Mcap download requested for date={target_date.isoformat()}")
    background_tasks.add_task(_run_mcap_download, target_date)

    return McapDownloadAccepted(
        message="Mcap download started",
        date=target_date.isoformat(),
    )
