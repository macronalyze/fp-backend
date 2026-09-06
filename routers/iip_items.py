"""Read-only endpoint backing the IIP item-level catalog.

Returns chart-ready "lines" at one of three server-computed drill levels,
each with MoM%/YoY% computed on the full history before an optional date
range is applied:

    neither nic2 nor nic5      -> level="nic2": one averaged line per NIC2 sector
    nic2 given, nic5 not given -> level="nic5": one averaged line per NIC5 group within that sector
    nic5 given (nic2 optional) -> level="item": one line per individual item in that group
"""

import re
from collections import defaultdict

from fastapi import APIRouter, HTTPException

from db import get_db
from models import IipItemsResponse, IipLine, IipSeriesEntry

router = APIRouter()

_COLLECTION = "iip_items"
_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")


def _validate_period(value: str | None, param_name: str) -> None:
    if value is not None and not _PERIOD_RE.match(value):
        raise HTTPException(400, f"Invalid {param_name!r}: expected 'YYYY-MM', got {value!r}")


def _shift_period(period: str, months: int) -> str:
    """'2023-01' shifted by -1 -> '2022-12'; by -12 -> '2022-01'."""
    year, month = (int(p) for p in period.split("-"))
    total = year * 12 + (month - 1) - months
    new_year, new_month = divmod(total, 12)
    return f"{new_year:04d}-{new_month + 1:02d}"


def _pct_change(current: float, reference: float | None) -> float | None:
    if reference is None or reference == 0:
        return None
    return round((current - reference) / reference * 100, 2)


def _series_from_values(by_period: dict[str, float], period_labels: dict[str, str]) -> list[IipSeriesEntry]:
    """Build a full-history IipSeriesEntry list (with MoM/YoY) from a
    {period: value} map, sorted chronologically."""
    entries = []
    for period in sorted(by_period):
        value = by_period[period]
        mom_ref = by_period.get(_shift_period(period, 1))
        yoy_ref = by_period.get(_shift_period(period, 12))
        entries.append(
            IipSeriesEntry(
                period=period,
                label=period_labels[period],
                value=value,
                momPercent=_pct_change(value, mom_ref),
                yoyPercent=_pct_change(value, yoy_ref),
            )
        )
    return entries


def _clip(series: list[IipSeriesEntry], start_date: str | None, end_date: str | None) -> list[IipSeriesEntry]:
    return [
        e for e in series
        if (start_date is None or e.period >= start_date)
        and (end_date is None or e.period <= end_date)
    ]


def _dataset_bounds(db) -> tuple[str | None, str | None]:
    """Earliest/latest period across the WHOLE collection, independent of
    the current nic2/nic5 filter."""
    pipeline = [
        {"$project": {"firstPeriod": {"$arrayElemAt": ["$series.period", 0]}, "lastPeriod": "$latestPeriod"}},
        {"$group": {"_id": None, "minPeriod": {"$min": "$firstPeriod"}, "maxPeriod": {"$max": "$lastPeriod"}}},
    ]
    result = list(db[_COLLECTION].aggregate(pipeline))
    if not result:
        return None, None
    return result[0].get("minPeriod"), result[0].get("maxPeriod")


def _item_line(doc: dict) -> IipLine:
    raw_series = doc.get("series", [])
    by_period = {e["period"]: e["value"] for e in raw_series}
    labels = {e["period"]: e["label"] for e in raw_series}
    provisional_periods = {e["period"] for e in raw_series if e.get("provisional")}

    series = _series_from_values(by_period, labels)
    for entry in series:
        if entry.period in provisional_periods:
            entry.provisional = True

    return IipLine(
        id=doc["itemId"],
        label=doc["name"],
        nic2=doc["nic2"],
        nic2Name=doc["nic2Name"],
        nic5=doc.get("nic5"),
        itemCount=1,
        unit=doc.get("unit"),
        series=series,
    )


def _averaged_line(group_id: str, label: str, nic2: str, nic2_name: str, nic5: int | None, docs: list[dict]) -> IipLine:
    sums: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    labels: dict[str, str] = {}

    for doc in docs:
        for entry in doc.get("series", []):
            period = entry["period"]
            sums[period] += entry["value"]
            counts[period] += 1
            labels[period] = entry["label"]

    by_period = {period: sums[period] / counts[period] for period in sums}
    series = _series_from_values(by_period, labels)

    return IipLine(
        id=group_id,
        label=label,
        nic2=nic2,
        nic2Name=nic2_name,
        nic5=nic5,
        itemCount=len(docs),
        unit=None,
        series=series,
    )


@router.get("/iip-items", response_model=IipItemsResponse)
def get_iip_items(
    nic2: str | None = None,
    nic5: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> IipItemsResponse:
    _validate_period(start_date, "start_date")
    _validate_period(end_date, "end_date")

    db = get_db()
    earliest_period, latest_period = _dataset_bounds(db)

    query: dict = {}
    if nic2 is not None:
        query["nic2"] = nic2
    if nic5 is not None:
        query["nic5"] = nic5

    docs = list(db[_COLLECTION].find(query))

    if nic5 is not None:
        level = "item"
        lines = [_item_line(doc) for doc in docs]
    elif nic2 is not None:
        level = "nic5"
        by_nic5: dict[int | None, list[dict]] = defaultdict(list)
        for doc in docs:
            by_nic5[doc.get("nic5")].append(doc)
        lines = [
            _averaged_line(
                group_id=f"{nic2}:{group_nic5}",
                label=", ".join(sorted(d["name"] for d in group_docs)),
                nic2=nic2,
                nic2_name=group_docs[0]["nic2Name"],
                nic5=group_nic5,
                docs=group_docs,
            )
            for group_nic5, group_docs in by_nic5.items()
        ]
    else:
        level = "nic2"
        by_nic2: dict[str, list[dict]] = defaultdict(list)
        for doc in docs:
            by_nic2[doc["nic2"]].append(doc)
        lines = [
            _averaged_line(
                group_id=group_nic2,
                label=group_docs[0]["nic2Name"],
                nic2=group_nic2,
                nic2_name=group_docs[0]["nic2Name"],
                nic5=None,
                docs=group_docs,
            )
            for group_nic2, group_docs in by_nic2.items()
        ]

    for line in lines:
        line.series = _clip(line.series, start_date, end_date)

    lines.sort(key=lambda l: l.label)

    return IipItemsResponse(
        level=level,
        count=len(lines),
        earliestPeriod=earliest_period,
        latestPeriod=latest_period,
        lines=lines,
    )
