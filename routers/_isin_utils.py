from db import get_db

_ISIN_DETAIL_FIELDS = {
    "COMPANY_NAME": "name",
    "NSE_SYMBOL": "nse_symbol",
    "BSE_CODE": "bse_code",
    "industry": "industry",
    "sector": "sector",
    "free_float_shares": "free_float_shares",
}


def fetch_isin_details(isin: str) -> dict:
    """Return ISIN details from the `isin` collection, keyed by response field
    names. Returns an empty dict if no document is found."""
    doc = get_db()["isin"].find_one(
        {"_id": isin},
        {src: 1 for src in _ISIN_DETAIL_FIELDS},
    )
    if not doc:
        return {}
    return {dest: doc[src] for src, dest in _ISIN_DETAIL_FIELDS.items() if src in doc}
