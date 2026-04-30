"""
EN-edition match resolver — used by /admin/cards/en-match.

Pulled out of server.py so it can be unit-tested without importing the
whole Flask app. The endpoint is the "Matched as" header strip on
/admin/market that shows the operator which English-edition card the
language-pricing chips were anchored to.

Everything resolves locally except the eBay deep-link URL, which is
built server-side but disabled by the frontend when the browser is
offline. See server.py admin_cards_en_match() for the route layer
and pi-setup/docs/USB_OFFLINE_DB.md for the offline-first design.

Public surface:
    normalise_number(raw)              -> str
    resolve_en_match(db, name, set,    -> dict | None
                     number)
    build_match_response(row,
                         set_id,
                         card_number)  -> dict

`db` is any DB-API 2.0 connection that yields a cursor with .execute()
+ .fetchone() — psycopg2 in production, a fake in tests.
"""
from __future__ import annotations

from typing import Optional, Sequence
from urllib.parse import urlencode


# ── Number normalisation ─────────────────────────────────────────────────

def normalise_number(raw: str) -> str:
    """
    Strip leading zeros on pure-int collector numbers so '008' matches
    '8' in cards_master. Alphanumerics like 'TG01' / 'SV-P-001' are
    left alone — they're real Pokémon collector formats and stripping
    digits from them would corrupt the lookup.

    '0' / '00' / '000' all collapse to '0' (never the empty string).
    """
    if not raw or not raw.isdigit():
        # Empty or non-digit ('TG01' / 'SV-P-001') — return unchanged so
        # set-specific collector formats survive the lookup.
        return raw
    stripped = raw.lstrip("0")
    # All-zero strings ('0' / '00' / '000') strip down to '' and must
    # collapse to '0' — never the empty string, or the SQL params would
    # silently match nothing.
    return stripped if stripped else "0"


# ── SQL-driven resolver ──────────────────────────────────────────────────

# Tuple shape returned by every SELECT in resolve_en_match.
# (name_en, set_id, card_number, rarity, artist, image_url)
_SELECT_COLS = (
    "SELECT name_en, set_id, card_number, rarity, artist, image_url "
    "  FROM cards_master "
)


def resolve_en_match(
    db,
    name: str,
    set_code: str,
    number: str,
) -> Optional[dict]:
    """
    Try, in priority order:

      1. exact     — set_id + card_number both supplied and matched.
                     Both raw and zero-stripped forms of `number` are
                     searched so we tolerate either storage convention.
      2. name_set  — name fuzzy-match restricted to the supplied set.
                     ILIKE across name_en/kr/jp/chs/cht so the operator
                     can hand us the card name in any language.
      3. name      — name fuzzy-match across cards_master with no set
                     filter; shortest name_en wins as a cheap "least-
                     suffixed" proxy (Charizard ex over Charizard ex VMAX).

    Returns the response dict (see build_match_response) or None when
    no row matches. All three SELECTs filter to rows where name_en is
    non-empty so we never anchor pricing to a card we don't have an
    English name for.
    """
    cur = db.cursor()
    try:
        num_norm = normalise_number(number)

        # 1. exact (set_id + card_number)
        if set_code and num_norm:
            cur.execute(
                _SELECT_COLS +
                " WHERE set_id = %s "
                "   AND (card_number = %s OR card_number = %s) "
                "   AND name_en <> '' "
                " LIMIT 1",
                (set_code, num_norm, number),
            )
            row = cur.fetchone()
            if row:
                return build_match_response(row, confidence="exact")

        # 2. name + set
        if name and set_code:
            cur.execute(
                _SELECT_COLS +
                " WHERE set_id = %s "
                "   AND name_en <> '' "
                "   AND (name_en   ILIKE %s OR name_kr ILIKE %s OR "
                "        name_jp   ILIKE %s OR name_chs ILIKE %s OR "
                "        name_cht  ILIKE %s) "
                " ORDER BY length(name_en) ASC "
                " LIMIT 1",
                (set_code,) + (f"%{name}%",) * 5,
            )
            row = cur.fetchone()
            if row:
                return build_match_response(row, confidence="name_set")

        # 3. name only
        if name:
            cur.execute(
                _SELECT_COLS +
                " WHERE name_en <> '' "
                "   AND (name_en   ILIKE %s OR name_kr ILIKE %s OR "
                "        name_jp   ILIKE %s OR name_chs ILIKE %s OR "
                "        name_cht  ILIKE %s) "
                " ORDER BY length(name_en) ASC "
                " LIMIT 1",
                (f"%{name}%",) * 5,
            )
            row = cur.fetchone()
            if row:
                return build_match_response(row, confidence="name")
    finally:
        try:
            cur.close()
        except Exception:
            pass

    return None


# ── Response builder ─────────────────────────────────────────────────────

def build_match_response(
    row: Sequence,
    confidence: str = "name",
) -> dict:
    """
    Shape the cards_master row into the JSON the frontend consumes.

    The image URL goes through /card/image so the USB mirror is
    consulted before the network — non-negotiable for offline-first.
    The eBay URL targets *sold + completed* listings (LH_Sold=1 +
    LH_Complete=1) because that's what determines trade-in pricing,
    not aspirational asks.
    """
    name_en, set_id, card_number, rarity, artist, _image_url = row

    img_q = urlencode({
        "set_id":      set_id,
        "card_number": card_number,
        "lang":        "en",
    })
    image_local_url = f"/card/image?{img_q}"

    ebay_q = urlencode({
        "_nkw":        f"{name_en} {card_number} pokemon",
        "LH_Sold":     "1",
        "LH_Complete": "1",
        "_ipg":        "60",
    })
    ebay_sold_url = f"https://www.ebay.com/sch/i.html?{ebay_q}"

    return {
        "name_en":         name_en,
        "set_id":          set_id,
        "card_number":     card_number,
        "rarity":          rarity or "",
        "artist":          artist or "",
        "image_local_url": image_local_url,
        "ebay_sold_url":   ebay_sold_url,
        "confidence":      confidence,
    }
