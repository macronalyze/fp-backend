import logging
import re
from datetime import datetime

from fastapi import APIRouter, HTTPException

from db import get_db
from models import LatestMcapResponse, McapDataResponse, McapEntry
from routers._isin_utils import fetch_isin_details

logger = logging.getLogger(__name__)

router = APIRouter()

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
_SOURCE = "computed"


def _ff_shares(isin: str) -> int | None:
    doc = get_db()["isin"].find_one({"_id": isin}, {"free_float_shares": 1})
    if not doc:
        return None
    ff = doc.get("free_float_shares")
    return ff if ff and ff > 0 else None


@router.get("/mcap/{isin}", response_model=McapDataResponse)
def get_mcap_data(
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

    ff = _ff_shares(isin)
    if ff is None:
        raise HTTPException(404, f"No market cap data available for ISIN {isin}")

    start_month = start.strftime("%Y-%m") if start else "0000-00"
    end_month = end.strftime("%Y-%m") if end else "9999-99"

    db = get_db()
    cursor = db["raw_bhav_data_v3"].find(
        {"_id": {"$gte": f"{isin}_{start_month}", "$lte": f"{isin}_{end_month}"}},
        {"d": 1},
    )

    # Flatten daily entries, applying date filter and NSE > BSE preference per
    # date. `by_date[YYYY-MM-DD] = (preferred_ex, close_price)`.
    by_date: dict[str, tuple[str, float]] = {}
    for doc in cursor:
        for e in doc.get("d", []):
            dt = e.get("dt")
            if not isinstance(dt, datetime):
                continue
            if start and dt < start:
                continue
            if end and dt > end:
                continue
            close = e.get("c")
            if close is None or close <= 0:
                continue
            d_str = dt.strftime("%Y-%m-%d")
            ex = e.get("ex")
            existing = by_date.get(d_str)
            if existing and existing[0] == "nse":
                continue
            by_date[d_str] = (ex, close)

    if not by_date:
        raise HTTPException(404, f"No market cap data found for ISIN {isin}")

    entries = [
        McapEntry(
            date=d,
            face_value=None,
            issue_size=None,
            market_cap=close * ff,
        )
        for d, (_ex, close) in sorted(by_date.items())
    ]

    return McapDataResponse(
        isin=isin,
        **fetch_isin_details(isin),
        source=_SOURCE,
        count=len(entries),
        data=entries,
    )


@router.get("/mcap/{isin}/latest", response_model=LatestMcapResponse)
def get_latest_mcap(isin: str):
    if not _ISIN_RE.match(isin):
        raise HTTPException(400, "Invalid ISIN format")

    ff = _ff_shares(isin)
    if ff is None:
        raise HTTPException(404, f"No market cap data available for ISIN {isin}")

    db = get_db()
    doc = db["raw_bhav_data_v3"].find_one(
        {"_id": {"$gte": f"{isin}_", "$lte": f"{isin}_~"}},
        sort=[("_id", -1)],
    )
    if not doc or not doc.get("d"):
        raise HTTPException(404, f"No market cap data found for ISIN {isin}")

    # Latest day across exchanges; on a tie, prefer NSE.
    valid = [e for e in doc["d"] if e.get("c") is not None and e["c"] > 0]
    if not valid:
        raise HTTPException(404, f"No market cap data found for ISIN {isin}")
    chosen = max(valid, key=lambda e: (e["dt"], 1 if e.get("ex") == "nse" else 0))

    return LatestMcapResponse(
        isin=isin,
        **fetch_isin_details(isin),
        source=_SOURCE,
        entry=McapEntry(
            date=chosen["dt"].strftime("%Y-%m-%d"),
            face_value=None,
            issue_size=None,
            market_cap=chosen["c"] * ff,
        ),
    )
