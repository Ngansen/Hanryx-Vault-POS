"""eBay Finding API — sold listings (ground-truth realised prices).

Requires the EBAY_APP_ID env var. Without it, search_ebay_sold() returns []
gracefully so callers don't break. Free tier: 5000 calls/day. Register an
app at https://developer.ebay.com/my/keys (the production "App ID" key).

Plug into price_scrapers.py:
    from ebay_sold import search_ebay_sold
    SCRAPERS["ebay_sold"] = search_ebay_sold

Recommended only for cards with USD market price >= $50 — wastes quota
on bulk commons otherwise. price_scrapers can guard with:
    if scraper == "ebay_sold" and (market_usd or 0) < 50: continue
"""
from __future__ import annotations
import os
from typing import Any
import requests

EBAY_APP_ID  = os.environ.get("EBAY_APP_ID", "").strip()
EBAY_TIMEOUT = float(os.environ.get("EBAY_TIMEOUT", "8"))
EBAY_ENDPOINT = "https://svcs.ebay.com/services/search/FindingService/v1"
GLOBAL_ID = os.environ.get("EBAY_GLOBAL_ID", "EBAY-US")


def search_ebay_sold(query: str, limit: int = 20) -> list[dict[str, Any]]:
    if not EBAY_APP_ID or not query:
        return []
    params = {
        "OPERATION-NAME": "findCompletedItems",
        "SERVICE-VERSION": "1.13.0",
        "SECURITY-APPNAME": EBAY_APP_ID,
        "GLOBAL-ID": GLOBAL_ID,
        "RESPONSE-DATA-FORMAT": "JSON",
        "REST-PAYLOAD": "",
        "keywords": query,
        "paginationInput.entriesPerPage": str(min(max(limit, 1), 100)),
        "itemFilter(0).name": "SoldItemsOnly",
        "itemFilter(0).value": "true",
        "sortOrder": "EndTimeSoonest",
    }
    try:
        r = requests.get(EBAY_ENDPOINT, params=params, timeout=EBAY_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return []

    try:
        items = (data["findCompletedItemsResponse"][0]
                     ["searchResult"][0].get("item", []))
    except (KeyError, IndexError, TypeError):
        return []

    out: list[dict[str, Any]] = []
    for it in items:
        try:
            sp = it["sellingStatus"][0]["currentPrice"][0]
            price = float(sp["__value__"]); currency = sp["@currencyId"]
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        out.append({
            "title": it.get("title", [None])[0],
            "price": price,
            "price_native": price,
            "currency": currency,
            "native_currency": currency,
            "sold_at": (it.get("listingInfo", [{}])[0].get("endTime", [None])[0]),
            "listing_url": (it.get("viewItemURL", [None])[0]),
            "image_url":   (it.get("galleryURL", [None])[0]),
            "condition":   ((it.get("condition", [{}])[0].get("conditionDisplayName", [None]))[0]),
            "source": "ebay_sold",
        })
    return out


if __name__ == "__main__":
    import json, sys
    q = " ".join(sys.argv[1:]) or "Charizard ex 199/197"
    print(json.dumps(search_ebay_sold(q, 5), indent=2, default=str))
