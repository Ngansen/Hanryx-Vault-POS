# Tablet ↔ POS server API contract

This document is the **single source of truth** for the JSON shapes the
tablet (Expo APK) exchanges with the Main Pi (`http://192.168.86.36:8080`).
The tablet code lives in a separate repo, so when you change a route here
update this file in the same commit.

---

## 1. Card scan — `POST /card/scan/recognizer/image`

Multipart form upload of a single card photo. Proxied to the recognizer at
`recognizer:8081/recognize/image`.

**Request**

```
POST /card/scan/recognizer/image
Content-Type: multipart/form-data

image=@card.jpg
```

**Response (200)**

```json
{
  "count": 3,
  "results": [
    {
      "source":      "kr",
      "card_id":     "kr-sv1k-025",
      "set_code":    "SV1K",
      "card_number": "025/198",
      "name":        "피카츄",
      "language":    "kr",
      "image_url":   "https://...",
      "method":      "ocr_number",
      "score":       0.99,
      "confidence":  0.99
    },
    {
      "source": "multi:Magic",
      "card_id": "mtg-...",
      "method": "phash",
      "score": 0.78,
      "...": "..."
    }
  ]
}
```

`method` is one of:
- `"ocr_number"` — Tesseract read the printed card number cleanly and the
  card was found in the DB by that number. Treat as effectively certain.
- `"phash"` — Match came from perceptual-hash KNN over the artwork. Score
  is `1 - (hammingDistance / 32)`.
- `"fallback"` — Recognizer couldn't decide; show the operator the full
  list and ask them to pick.

**UI guidance**

Show a colored confidence badge on each candidate:

| Method        | Score range  | Badge         | Behavior                       |
|---------------|-------------:|---------------|---------------------------------|
| `ocr_number`  |       any    | green "exact" | auto-pick top-1 after 1.5 s     |
| `phash`       |     ≥ 0.85   | green         | auto-pick top-1 after 2 s       |
| `phash`       | 0.70 – 0.85  | yellow        | require operator tap            |
| `phash`       |     < 0.70   | orange        | require operator tap, suggest re-scan |
| `fallback`    |       any    | red           | always require operator tap     |

---

## 2. Operator pick logging — `POST /card/scan/log_pick`

Call this **every time** the operator finishes interacting with the
candidate list. This is the labeled-data pipeline that drives accuracy
improvements over time.

**Request**

```json
{
  "ts":         1745176800000,
  "operator":   "kim",
  "device":     "tablet-01",
  "image_sha":  "sha256:abc123…",

  "ocr_tokens": ["025/198", "025/198"],
  "candidates": [
    { "source": "kr", "card_id": "kr-sv1k-025",
      "method": "ocr_number", "score": 0.99, "name": "피카츄" },
    { "source": "multi:Pokemon", "card_id": "mt-...",
      "method": "phash", "score": 0.81, "name": "Pikachu" }
  ],

  "picked_index": 0,
  "action":       "accepted",
  "notes":        ""
}
```

| Field          | Meaning                                                                 |
|----------------|--------------------------------------------------------------------------|
| `ts`           | Client timestamp in **milliseconds**. Server fills in if omitted.        |
| `operator`     | Logged-in operator code/name. Free text.                                 |
| `device`       | Stable per-tablet ID — used to correlate device-specific accuracy.       |
| `image_sha`    | SHA-256 of the uploaded image bytes. Lets us dedupe scans of the same card. |
| `ocr_tokens`   | Same array the recognizer returned (for retraining ground-truth).        |
| `candidates`   | The full ordered list the recognizer returned.                           |
| `picked_index` | Zero-based index into `candidates`. Use `-1` when the operator typed manually or rejected all. |
| `action`       | `"accepted"` (took top-1), `"overridden"` (took a different one), `"rejected"` (none usable, no manual entry yet), `"manual"` (operator typed the card by hand). If omitted, the server infers from `picked_index`. |
| `notes`        | Optional free text (≤ 500 chars).                                        |

**Response (200)** `{ "ok": true, "id": 12345 }`

---

## 3. Price quote — `POST /card/price/v2`

Multi-source price quote with eBay-sold + trimmed median + condition
multipliers. **Always prefer this over the legacy `/card/price`.**

**Request**

```json
{
  "query":       "Pikachu 25/198 Scarlet Violet",
  "game":        "Pokemon",
  "condition":   "NM",
  "card_id":     "kr-sv1k-025",
  "source":      "kr",
  "max_age_sec": 21600,
  "force_refresh": false
}
```

| Field           | Default      | Notes                                              |
|-----------------|--------------|----------------------------------------------------|
| `query`         | **required** | The marketplace search string.                     |
| `game`          | `"Pokemon"`  | One of `Pokemon` / `Magic` / `Lorcana` / `OnePiece` / `DBS`. Used by Cardmarket. |
| `condition`     | `"NM"`       | `NM` / `LP` / `MP` / `HP` / `DMG` / `PSA9` / `PSA10`. |
| `card_id`       | `null`       | Strongly recommended when known — sharper cache key. |
| `source`        | `null`       | Hint for the cache key (`kr`/`chs`/`jpn`/`multi:<game>`). |
| `max_age_sec`   | `21600` (6h) | Cache TTL. Lower for thin markets, higher for stable cards. |
| `force_refresh` | `false`      | Bypass the cache and re-scrape now.                 |

**Response (200)**

```json
{
  "median_usd":          12.34,
  "nm_median_usd":       12.34,
  "p25_usd":              9.50,
  "p75_usd":             15.10,

  "sample_count":        42,
  "source_count":         3,
  "sources_used":        ["ebay_sold", "tcgkorea", "cardmarket"],

  "volatility":           0.31,
  "volatile_flag":        false,

  "condition":           "NM",
  "condition_multiplier": 1.0,

  "from_cache":           true,
  "fetched_at":           1745176800000,

  "query": "Pikachu 25/198 Scarlet Violet",
  "game":  "Pokemon",

  "listings_sample": [
    { "source": "ebay_sold", "title": "...", "price": 11.00,
      "currency": "USD", "price_usd": 11.00,
      "url": "https://...", "sold_at": 1745000000000 }
  ]
}
```

`median_usd` is **already condition-adjusted** — display it as the asking
price. `nm_median_usd` is preserved so you can show "NM equivalent" too.

`volatile_flag = true` (IQR / median > 0.45) means the market is thin or
inconsistent — show a small warning so the operator can sanity-check
manually.

---

## 4. Admin endpoints (web UI only, not tablet)

| Method | Path                                | Purpose                              |
|--------|-------------------------------------|--------------------------------------|
| GET    | `/admin/scan-overrides/stats`       | Accuracy by method × source × action |
| GET    | `/admin/scan-overrides/export`      | CSV dump of every operator pick      |
| GET    | `/card/scan/recognizer/status`      | Recognizer health + hash-index size  |

Both override endpoints accept `?since_ms=<unix-ms>` to scope to a window.
