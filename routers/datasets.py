"""Read-only endpoints backing the datasets catalog and detail pages."""

import logging

from fastapi import APIRouter, HTTPException

from db import get_db
from models import (
    CountryDatasets,
    DatasetDetail,
    DatasetSummary,
    MonthlyEntry,
    Sector,
    YearlyEntry,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_DATASETS = "datasets"
_OBSERVATIONS = "dataset_observations"

# Display names per country. Kept here (not in the DB) since this is a tiny,
# UI-only concern. Add entries as new countries come online.
_COUNTRY_NAMES = {
    "india": "India",
}


def _strip_id(doc: dict) -> dict:
    doc.pop("_id", None)
    return doc


def _latest_monthly(country: str, dataset_id: str) -> dict | None:
    """Return the most recent monthly observation for a dataset, or None."""
    db = get_db()
    return db[_OBSERVATIONS].find_one(
        {"country": country, "datasetId": dataset_id, "granularity": "monthly"},
        sort=[("period", -1)],
    )


def _summary_from_meta(meta: dict, country: str) -> DatasetSummary:
    """
    Build a catalog summary by overlaying live observation data on top of
    static fields stored in the meta doc. Live data wins when present.
    """
    summary = {
        "id": meta["id"],
        "name": meta["name"],
        "shortName": meta["shortName"],
        "icon": meta["icon"],
        "latestPeriod": meta.get("latestPeriod"),
        "latestGrowth": meta.get("latestGrowth"),
        "cumulativeGrowth": meta.get("cumulativeGrowth"),
        "cumulativePeriod": meta.get("cumulativePeriod"),
        "status": meta.get("status"),
        "releaseDate": meta.get("releaseDate"),
    }

    latest = _latest_monthly(country, meta["id"])
    if latest:
        summary["latestPeriod"] = latest.get("label") or latest.get("period")
        growth = latest.get("growth") or {}
        if "overall" in growth:
            summary["latestGrowth"] = growth["overall"]
        summary["status"] = "provisional" if latest.get("provisional") else "available"

    return DatasetSummary(**summary)


@router.get("/datasets/{country}", response_model=CountryDatasets)
def get_country_datasets(country: str) -> CountryDatasets:
    db = get_db()
    # `hidden: true` marks sub-datasets that are accessed only via a parent
    # catalog tile (e.g. import-data/export-data under import-export).
    metas = list(db[_DATASETS].find({"country": country, "hidden": {"$ne": True}}))
    if not metas:
        raise HTTPException(404, f"No datasets registered for country '{country}'")

    summaries = [_summary_from_meta(meta, country) for meta in metas]
    summaries.sort(key=lambda s: s.name.lower())

    return CountryDatasets(
        country=country,
        countryName=_COUNTRY_NAMES.get(country, country.title()),
        datasets=summaries,
    )


@router.get("/datasets/{country}/{dataset_id}", response_model=DatasetDetail)
def get_dataset_detail(country: str, dataset_id: str) -> DatasetDetail:
    db = get_db()

    meta = db[_DATASETS].find_one({"_id": f"{country}:{dataset_id}"})
    if not meta:
        raise HTTPException(404, f"Dataset '{dataset_id}' not found for '{country}'")

    cursor = db[_OBSERVATIONS].find(
        {"country": country, "datasetId": dataset_id},
        sort=[("granularity", 1), ("period", 1)],
    )

    monthly: list[MonthlyEntry] = []
    yearly: list[YearlyEntry] = []
    for obs in cursor:
        provisional = obs.get("provisional") or None
        if obs.get("granularity") == "monthly":
            monthly.append(
                MonthlyEntry(
                    period=obs["period"],
                    label=obs.get("label", obs["period"]),
                    provisional=provisional,
                    index=obs.get("index"),
                    growth=obs.get("growth"),
                    values=obs.get("values"),
                )
            )
        elif obs.get("granularity") == "yearly":
            yearly.append(
                YearlyEntry(
                    year=obs["period"],
                    provisional=provisional,
                    index=obs.get("index"),
                    growth=obs.get("growth"),
                    values=obs.get("values"),
                )
            )

    sectors = [Sector(**s) for s in meta.get("sectors", [])]

    return DatasetDetail(
        id=meta["id"],
        name=meta["name"],
        shortName=meta["shortName"],
        country=meta["country"],
        baseYear=meta.get("baseYear"),
        baseValue=meta.get("baseValue"),
        source=meta.get("source"),
        releaseDate=meta.get("releaseDate"),
        nextRelease=meta.get("nextRelease"),
        description=meta.get("description"),
        sectors=sectors,
        commodities=list(meta.get("commodities", [])),
        monthly=monthly,
        yearly=yearly,
    )
