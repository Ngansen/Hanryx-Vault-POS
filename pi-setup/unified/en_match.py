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
    resolve_set_id(db, raw_set)        -> str
    resolve_set_id_with_source(db,     -> (str, str)   # (set_id, source_label)
                               raw_set)
    resolve_en_match(db, name, set,    -> dict | None
                     number)
    build_match_response(row,
                         confidence,
                         set_match)    -> dict

`db` is any DB-API 2.0 connection that yields a cursor with .execute()
+ .fetchone() — psycopg2 in production, a fake in tests.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional, Sequence, Tuple
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


# ── Set-id canonicaliser ─────────────────────────────────────────────────

# Source labels surfaced in the en-match response so the booth operator
# can tell *how* a set was canonicalised. Stable strings — the frontend
# tooltip and any future audit/log consumer should be able to depend on
# these exact values. New tiers must be added here too.
_SET_SOURCES = ("set_id", "name_exact", "name_like", "alias", "raw")


def _normalise_set_needle(raw: str) -> str:
    """
    Defensive normalisation of an operator-pasted set string.

    Handles the common shapes that silently bypass tier-1 / tier-2
    lookups in practice — all of which we've seen happen at the booth:

      * Hangul / kana arriving *decomposed* from clipboard (ㅍ + ㅏ + ㄹ
        instead of pre-composed 팔). NFC re-composition fixes this so
        the equality check against ref_set_mapping.name_kr actually hits.
      * NBSP (U+00A0) injected by some clipboards in place of a regular
        space — invisible to the operator but breaks exact match.
      * Fullwidth space (U+3000) from East Asian input methods.
      * Trailing / leading whitespace and runs of internal whitespace.

    Crucially does NOT use NFKC — that would also flatten compatibility
    characters (ﬁ → fi, ⅠⅡⅢ → 123, fullwidth Latin → Latin) which can
    corrupt legitimate set names that happen to contain them. NFC is
    the safe choice for a lookup key.
    """
    if not raw:
        return raw
    s = unicodedata.normalize("NFC", raw)
    # Map non-breaking + fullwidth spaces to plain space, then collapse runs.
    s = s.replace("\u00a0", " ").replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def resolve_set_id_with_source(db, raw_set: str) -> Tuple[str, str]:
    """
    Map whatever the frontend sent — a TCGdex set_id, a ptcgo code, a
    human-readable set name in any of the five languages, or a known
    alias — into the canonical `cards_master.set_id` value, and report
    *which* tier matched so the booth can surface the trust signal.

    The /admin/market page hands us `t2.set.name` (e.g. "Scarlet &
    Violet—Paldea Evolved") for the language-pricing eBay query, and
    that same string flows into our en-match call. cards_master only
    indexes by canonical set_id, so without this resolver tier-1 +
    tier-2 always miss and we silently fall through to the risky
    name-only tier-3.

    Resolution order against `ref_set_mapping` (returned source label
    matches the position):
      1. "set_id"     — literal set_id (case-insensitive).
      2. "name_exact" — exact match in name_en/kr/jp/chs/cht
                        (case-insensitive).
      3. "name_like"  — ILIKE substring across the same five columns.
      4. "alias"      — case-insensitive match against the JSONB
                        aliases array (operator-curated alternates:
                        ptcgo codes, abbreviations, legacy names).
      5. "raw"        — nothing matched. Falls back to the un-normalised
                        raw input so a fresh install with an empty
                        `ref_set_mapping` still behaves like the
                        pre-resolver code path.

    Returns (set_id, source). The cursor is opened and closed locally
    so the caller can keep reusing its own.
    """
    if not raw_set:
        return raw_set, "raw"
    needle = _normalise_set_needle(raw_set)
    if not needle:
        return raw_set, "raw"

    cur = db.cursor()
    try:
        # 1. Literal set_id — case-insensitive so 'SV2' and 'sv2' both work.
        try:
            cur.execute(
                "SELECT set_id FROM ref_set_mapping "
                " WHERE UPPER(set_id) = UPPER(%s) LIMIT 1",
                (needle,),
            )
            row = cur.fetchone()
            if row:
                return row[0], "set_id"
        except Exception:
            # ref_set_mapping might not exist on a brand-new install. Don't
            # blow up the whole match — just fall back to the raw input
            # below so we behave like the pre-resolver code path.
            return raw_set, "raw"

        # 2. Exact human-name match across all five languages first
        # (cheaper + more accurate than ILIKE), then ILIKE substring as a
        # last resort. Sets like "Paldea Evolved" appear under name_en.
        try:
            cur.execute(
                "SELECT set_id FROM ref_set_mapping "
                " WHERE UPPER(name_en)  = UPPER(%s) "
                "    OR UPPER(name_kr)  = UPPER(%s) "
                "    OR UPPER(name_jp)  = UPPER(%s) "
                "    OR UPPER(name_chs) = UPPER(%s) "
                "    OR UPPER(name_cht) = UPPER(%s) "
                " LIMIT 1",
                (needle,) * 5,
            )
            row = cur.fetchone()
            if row:
                return row[0], "name_exact"
        except Exception:
            return raw_set, "raw"

        try:
            like = f"%{needle}%"
            cur.execute(
                "SELECT set_id FROM ref_set_mapping "
                " WHERE name_en  ILIKE %s "
                "    OR name_kr  ILIKE %s "
                "    OR name_jp  ILIKE %s "
                "    OR name_chs ILIKE %s "
                "    OR name_cht ILIKE %s "
                " ORDER BY length(name_en) ASC "
                " LIMIT 1",
                (like,) * 5,
            )
            row = cur.fetchone()
            if row:
                return row[0], "name_like"
        except Exception:
            return raw_set, "raw"

        # 3. Operator-curated aliases (ptcgo codes, abbreviations).
        # `aliases` is a JSONB array of strings. We expand it with
        # jsonb_array_elements_text and compare lower(alias) to
        # lower(needle) — case-insensitive, matching the rest of the
        # resolver, and robust to existing case-mixed alias data
        # without needing a backfill. Bonus: no JSONB literal to
        # construct, so quote/backslash/unicode in the needle are
        # bound as a normal text param with zero escape risk.
        try:
            cur.execute(
                "SELECT set_id FROM ref_set_mapping "
                " WHERE EXISTS ( "
                "    SELECT 1 FROM jsonb_array_elements_text(aliases) AS a "
                "     WHERE lower(a) = lower(%s) "
                " ) LIMIT 1",
                (needle,),
            )
            row = cur.fetchone()
            if row:
                return row[0], "alias"
        except Exception:
            return raw_set, "raw"
    finally:
        try:
            cur.close()
        except Exception:
            pass

    return raw_set, "raw"


def resolve_set_id(db, raw_set: str) -> str:
    """
    Backwards-compatible thin wrapper around resolve_set_id_with_source
    for callers that don't care about the source label. New code should
    prefer the tuple-returning version so the booth can surface the
    trust signal.
    """
    return resolve_set_id_with_source(db, raw_set)[0]


# ── SQL-driven resolver ──────────────────────────────────────────────────

# Tuple shape returned by every SELECT in resolve_en_match.
# (name_en, set_id, card_number, rarity, artist, image_url)
_SELECT_COLS = (
    "SELECT name_en, set_id, card_number, rarity, artist, image_url "
    "  FROM cards_master "
)

# How many candidate rows we pull per tier so the operator can be
# warned about ambiguity. We don't need the full set — just enough to
# know "is this 1 of 1, 1 of a few, or 1 of many?". 11 means we can
# faithfully say "1 of 10" and use ">10" semantics for anything
# beyond that. The chosen row is always the first one returned (the
# ORDER BY pins which row that is); the rest are counted for the
# `candidate_count` field on the response.
_CANDIDATE_FETCH_CAP = 11


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

    `set_code` is canonicalised through ref_set_mapping first so that
    callers can pass in any of {set_id, ptcgo code, human set name in
    any language} and the tier-1/tier-2 SQL still hits. The matched
    set-resolution tier is surfaced as `set_match` on the response so
    the booth can render a trust tooltip ("matched via alias", etc.).
    """
    # Canonicalise the set identifier BEFORE we open the main cursor so
    # the cursor we use for the SELECTs only sees a clean set_id. The
    # resolver opens its own short-lived cursor internally. We capture
    # the source label here even when set_code is empty so the response
    # is consistent across callers.
    if set_code:
        set_code, set_match = resolve_set_id_with_source(db, set_code)
    else:
        set_match = "raw"

    cur = db.cursor()
    try:
        num_norm = normalise_number(number)

        # 1. exact (set_id + card_number)
        # Note: cards_master is UNIQUE(set_id, card_number, variant_code)
        # so this CAN return more than one row when the same printed
        # number ships in multiple variants (holo, reverse holo, master
        # ball pattern, etc.). The candidate_count surfaces that so the
        # operator can disambiguate before pricing.
        if set_code and num_norm:
            cur.execute(
                _SELECT_COLS +
                " WHERE set_id = %s "
                "   AND (card_number = %s OR card_number = %s) "
                "   AND name_en <> '' "
                " LIMIT %s",
                (set_code, num_norm, number, _CANDIDATE_FETCH_CAP),
            )
            rows = cur.fetchall()
            if rows:
                return build_match_response(
                    rows[0], confidence="exact",
                    set_match=set_match,
                    candidate_count=len(rows))

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
                " LIMIT %s",
                (set_code,) + (f"%{name}%",) * 5 + (_CANDIDATE_FETCH_CAP,),
            )
            rows = cur.fetchall()
            if rows:
                return build_match_response(
                    rows[0], confidence="name_set",
                    set_match=set_match,
                    candidate_count=len(rows))

        # 3. name only
        if name:
            cur.execute(
                _SELECT_COLS +
                " WHERE name_en <> '' "
                "   AND (name_en   ILIKE %s OR name_kr ILIKE %s OR "
                "        name_jp   ILIKE %s OR name_chs ILIKE %s OR "
                "        name_cht  ILIKE %s) "
                " ORDER BY length(name_en) ASC "
                " LIMIT %s",
                (f"%{name}%",) * 5 + (_CANDIDATE_FETCH_CAP,),
            )
            rows = cur.fetchall()
            if rows:
                # Tier-3 ignored set_code entirely, so the set-resolution
                # tier is moot — flag it explicitly to avoid implying a
                # trust signal we didn't earn.
                return build_match_response(
                    rows[0], confidence="name",
                    set_match="raw",
                    candidate_count=len(rows))
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
    set_match: str = "raw",
    candidate_count: int = 1,
) -> dict:
    """
    Shape the cards_master row into the JSON the frontend consumes.

    The image URL goes through /card/image so the USB mirror is
    consulted before the network — non-negotiable for offline-first.
    The eBay URL targets *sold + completed* listings (LH_Sold=1 +
    LH_Complete=1) because that's what determines trade-in pricing,
    not aspirational asks.

    `set_match` is the resolution tier from resolve_set_id_with_source
    ("set_id" / "name_exact" / "name_like" / "alias" / "raw"). The
    booth surfaces it as a tooltip so the operator can tell at a
    glance whether the set was canonicalised confidently or whether
    it fell through to the raw input.

    `candidate_count` is how many cards_master rows the resolver tier
    actually matched (capped at _CANDIDATE_FETCH_CAP = 11, so any
    value of 11 should be read as "11 or more"). The booth renders
    "1 of N" alongside the confidence chip whenever this is > 1 so
    the operator knows to disambiguate before locking in a price.
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
        "set_match":       set_match,
        "candidate_count": candidate_count,
    }
