"""
workers/language_helper.py — multilingual enrichment helper.

For every cards_master row, this worker derives:

  romaji_jp     —  pykakasi  (ja → Latin Hepburn)         lazy-imported
  pinyin_chs    —  pypinyin  (zh-Hans → Pinyin w/ tone #) lazy-imported
  hangul_roman  —  built-in  (ko → Revised Romanization)  pure Python

…and stores them in `card_language_extra`. It also CROSS-FILLS empty
name fields from `ref_pokedex_species` when one language has the
canonical name but the card row doesn't, recording which fields were
filled (and from where) in `backfilled_fields`.

Why each script gets its own helper rather than one mega-library:
  * Korean is built-in pure Python — coverage is guaranteed even on a
    fresh Pi with zero pip installs. The Revised Romanization rules
    are a stable Unicode arithmetic problem.
  * Japanese needs a dictionary lookup (kanji → kana → romaji) so we
    rely on pykakasi.  If the library isn't installed we record
    'JP_LIB_MISSING' so the admin sees what to install.
  * Chinese pinyin similarly needs a dict (pypinyin); same fallback.

The whole thing is a per-card task on the shared bg_task_queue so it
runs alongside image_health and the future CLIP / OCR helpers without
special scheduling.

Trade-show value:
  An EN-speaking walk-up customer asks "do you have 피카츄?" — the
  receipt printer can show 'Pikachu (피카츄 / pikachyu)' so both staff
  and customer agree on the card. Search box accepts 'pikachyu' and
  finds Korean-only listings. JP-only cards become pronounceable for
  KR staff via romaji.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from .base import Worker, WorkerError

log = logging.getLogger("workers.language_helper")

# ── Korean: pure-Python Revised Romanization ─────────────────────
#
# Algorithm: every precomposed Hangul syllable lives at code point
#   0xAC00 + (initial × 588) + (medial × 28) + final
# where 0 <= initial < 19, 0 <= medial < 21, 0 <= final < 28
# (final == 0 means "no final consonant"). So decomposing is just
# arithmetic — no dictionary needed.
#
# Tables follow the official 2000 Revised Romanization of Korean.
# Liaison rules between syllables (e.g. 신라 → 'silla', not 'sinla')
# are intentionally NOT applied for v1 — staff understanding the
# syllable-by-syllable form is acceptable for card-name lookup, and
# avoiding context rules keeps this dependency-free and predictable.

_HANGUL_INITIALS = (
    "g","kk","n","d","tt","r","m","b","pp","s",
    "ss","","j","jj","ch","k","t","p","h",
)
_HANGUL_MEDIALS = (
    "a","ae","ya","yae","eo","e","yeo","ye","o","wa",
    "wae","oe","yo","u","wo","we","wi","yu","eu","ui","i",
)
_HANGUL_FINALS = (
    "","k","k","k","n","n","n","t","l","k",
    "m","l","l","l","p","l","m","p","p","t",
    "t","ng","t","t","k","t","p","t",
)
_HANGUL_BASE = 0xAC00
_HANGUL_LAST = 0xD7A3


def romanise_hangul(text: str) -> str:
    """Convert Hangul syllables in `text` to Revised Romanization
    (lowercase ASCII). Non-Hangul characters pass through unchanged
    (so 'Pikachu V' or '피카츄 ex' stay readable). Returns the
    transformed string."""
    if not text:
        return ""
    out: list[str] = []
    for ch in text:
        cp = ord(ch)
        if _HANGUL_BASE <= cp <= _HANGUL_LAST:
            idx = cp - _HANGUL_BASE
            initial = idx // 588
            medial  = (idx % 588) // 28
            final   = idx % 28
            out.append(_HANGUL_INITIALS[initial])
            out.append(_HANGUL_MEDIALS[medial])
            out.append(_HANGUL_FINALS[final])
        else:
            out.append(ch.lower() if ch.isascii() else ch)
    return "".join(out)


# ── Japanese: lazy pykakasi ──────────────────────────────────────
_PYKAKASI_TRIED = False
_PYKAKASI_KKS: Any = None
_PYKAKASI_VERSION: str = ""


def _load_pykakasi() -> Any:
    global _PYKAKASI_TRIED, _PYKAKASI_KKS, _PYKAKASI_VERSION
    if _PYKAKASI_TRIED:
        return _PYKAKASI_KKS
    _PYKAKASI_TRIED = True
    try:
        import pykakasi  # type: ignore
        _PYKAKASI_KKS = pykakasi.kakasi()
        _PYKAKASI_VERSION = getattr(pykakasi, "__version__", "?")
    except ImportError:
        log.info("[language_helper] pykakasi not installed — Japanese "
                 "romaji will be marked JP_LIB_MISSING. "
                 "`pip install pykakasi` to enable.")
        _PYKAKASI_KKS = None
    return _PYKAKASI_KKS


def romaji_japanese(text: str) -> tuple[str, str]:
    """Returns (romaji, status). status is 'OK', 'EMPTY_INPUT',
    'JP_LIB_MISSING', or 'ERROR:<reason>'."""
    if not text or not text.strip():
        return "", "EMPTY_INPUT"
    kks = _load_pykakasi()
    if kks is None:
        return "", "JP_LIB_MISSING"
    try:
        chunks = kks.convert(text)
    except Exception as e:  # noqa: BLE001
        return "", f"ERROR:{type(e).__name__}"
    out = " ".join(c.get("hepburn", "") for c in chunks if c.get("hepburn"))
    return out.strip().lower(), "OK"


# ── Chinese: lazy pypinyin ───────────────────────────────────────
_PYPINYIN_TRIED = False
_PYPINYIN_FN: Any = None
_PYPINYIN_STYLE: Any = None
_PYPINYIN_VERSION: str = ""


def _load_pypinyin() -> Any:
    global _PYPINYIN_TRIED, _PYPINYIN_FN, _PYPINYIN_STYLE, _PYPINYIN_VERSION
    if _PYPINYIN_TRIED:
        return _PYPINYIN_FN
    _PYPINYIN_TRIED = True
    try:
        import pypinyin  # type: ignore
        _PYPINYIN_FN = pypinyin.pinyin
        _PYPINYIN_STYLE = pypinyin.Style.TONE3   # tone numbers, ASCII
        _PYPINYIN_VERSION = getattr(pypinyin, "__version__", "?")
    except ImportError:
        log.info("[language_helper] pypinyin not installed — Chinese "
                 "pinyin will be marked CN_LIB_MISSING. "
                 "`pip install pypinyin` to enable.")
        _PYPINYIN_FN = None
    return _PYPINYIN_FN


def pinyin_chinese(text: str) -> tuple[str, str]:
    """Returns (pinyin, status). status is 'OK', 'EMPTY_INPUT',
    'CN_LIB_MISSING', or 'ERROR:<reason>'."""
    if not text or not text.strip():
        return "", "EMPTY_INPUT"
    fn = _load_pypinyin()
    if fn is None:
        return "", "CN_LIB_MISSING"
    try:
        groups = fn(text, style=_PYPINYIN_STYLE)
    except Exception as e:  # noqa: BLE001
        return "", f"ERROR:{type(e).__name__}"
    out = " ".join("".join(g) for g in groups if g and g[0])
    return out.strip().lower(), "OK"


# ── Worker ───────────────────────────────────────────────────────


class LanguageEnrichWorker(Worker):
    TASK_TYPE = "lang_enrich"
    BATCH_SIZE = 200       # Pure-CPU; very fast per card.
    IDLE_SLEEP_S = 60.0
    CLAIM_TIMEOUT_S = 600
    DEFAULT_RECHECK_AFTER_S = 30 * 86400   # Re-romanise monthly so
                                           # newly-installed libraries
                                           # eventually get applied.

    def __init__(self, conn, *, recheck_after_s: int | None = None, **kw):
        super().__init__(conn, **kw)
        self.recheck_after_s = recheck_after_s \
            if recheck_after_s is not None \
            else self.DEFAULT_RECHECK_AFTER_S

    def seed(self) -> int:
        """Enqueue every cards_master row that either has no row in
        card_language_extra yet, or whose existing entry is older
        than `recheck_after_s`. Idempotent via UNIQUE
        (task_type, task_key)."""
        cutoff = int(time.time()) - self.recheck_after_s
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO bg_task_queue
                (task_type, task_key, payload, status, created_at)
            SELECT 'lang_enrich',
                   c.set_id || '/' || c.card_number,
                   jsonb_build_object('set_id',      c.set_id,
                                      'card_number', c.card_number),
                   'PENDING',
                   %s
              FROM cards_master c
         LEFT JOIN card_language_extra e
                ON e.set_id = c.set_id
               AND e.card_number = c.card_number
             WHERE e.set_id IS NULL
                OR COALESCE(e.enriched_at, 0) < %s
            ON CONFLICT (task_type, task_key) DO NOTHING
        """, (int(time.time()), cutoff))
        n = cur.rowcount or 0
        self.conn.commit()
        log.info("[language_helper] seed enqueued %d task(s)", n)
        return n

    def process(self, task: dict) -> dict:
        payload = task.get("payload") or {}
        sid = (payload.get("set_id")     or "").strip()
        num = (payload.get("card_number") or "").strip()
        if not sid or not num:
            raise WorkerError(
                f"lang_enrich task {task['task_id']} missing "
                f"set_id/card_number in payload: {payload!r}"
            )

        cur = self.conn.cursor()
        cur.execute("""
            SELECT name_en, name_kr, name_jp, name_chs, pokedex_id
              FROM cards_master
             WHERE set_id = %s AND card_number = %s
        """, (sid, num))
        row = cur.fetchone()
        if row is None:
            raise WorkerError(f"cards_master row {sid}/{num} no longer exists")

        if isinstance(row, dict):
            name_en   = row.get("name_en")   or ""
            name_kr   = row.get("name_kr")   or ""
            name_jp   = row.get("name_jp")   or ""
            name_chs  = row.get("name_chs")  or ""
            pokedex_id = row.get("pokedex_id")
        else:
            name_en, name_kr, name_jp, name_chs, pokedex_id = row

        # Cross-language backfills from ref_pokedex_species. Only
        # apply when the row is empty AND the species table actually
        # has the alternative — this is read-only here, recorded in
        # backfilled_fields for the admin to apply via a separate
        # consolidator pass if desired (we don't mutate cards_master
        # from a worker — that's the consolidator's job).
        backfilled: dict[str, str] = {}
        if pokedex_id is not None:
            cur.execute("""
                SELECT name_en, name_kr, name_jp, name_chs
                  FROM ref_pokedex_species
                 WHERE pokedex_id = %s
                 LIMIT 1
            """, (pokedex_id,))
            sp = cur.fetchone()
            if sp is not None:
                if isinstance(sp, dict):
                    sp_en  = sp.get("name_en")  or ""
                    sp_kr  = sp.get("name_kr")  or ""
                    sp_jp  = sp.get("name_jp")  or ""
                    sp_chs = sp.get("name_chs") or ""
                else:
                    sp_en, sp_kr, sp_jp, sp_chs = sp
                # Only RECORD a suggested backfill — we do not mutate
                # cards_master here (single-writer discipline).
                if not name_en  and sp_en:  backfilled["name_en"]  = sp_en
                if not name_kr  and sp_kr:  backfilled["name_kr"]  = sp_kr
                if not name_jp  and sp_jp:  backfilled["name_jp"]  = sp_jp
                if not name_chs and sp_chs: backfilled["name_chs"] = sp_chs

        # Romanisation. Use whatever we have for each script (prefer
        # the card's own name; fall back to species suggestion if the
        # card is empty for that script).
        jp_src   = name_jp  or backfilled.get("name_jp",  "")
        chs_src  = name_chs or backfilled.get("name_chs", "")
        kr_src   = name_kr  or backfilled.get("name_kr",  "")

        romaji,  romaji_status  = romaji_japanese(jp_src)
        pinyin,  pinyin_status  = pinyin_chinese(chs_src)
        hangul   = romanise_hangul(kr_src)
        hangul_status = "OK" if hangul else "EMPTY_INPUT"

        lib_versions = {
            "pykakasi": _PYKAKASI_VERSION,
            "pypinyin": _PYPINYIN_VERSION,
            "hangul":   "builtin-rr-1",   # bump if the rules table changes
        }

        cur.execute("""
            INSERT INTO card_language_extra
                (set_id, card_number,
                 romaji_jp, romaji_jp_status,
                 pinyin_chs, pinyin_chs_status,
                 hangul_roman, hangul_roman_status,
                 backfilled_fields, library_versions, enriched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
            ON CONFLICT (set_id, card_number) DO UPDATE SET
                romaji_jp           = EXCLUDED.romaji_jp,
                romaji_jp_status    = EXCLUDED.romaji_jp_status,
                pinyin_chs          = EXCLUDED.pinyin_chs,
                pinyin_chs_status   = EXCLUDED.pinyin_chs_status,
                hangul_roman        = EXCLUDED.hangul_roman,
                hangul_roman_status = EXCLUDED.hangul_roman_status,
                backfilled_fields   = EXCLUDED.backfilled_fields,
                library_versions    = EXCLUDED.library_versions,
                enriched_at         = EXCLUDED.enriched_at
        """, (
            sid, num,
            romaji, romaji_status,
            pinyin, pinyin_status,
            hangul, hangul_status,
            json.dumps(backfilled, ensure_ascii=False),
            json.dumps(lib_versions, ensure_ascii=False),
            int(time.time()),
        ))
        self.conn.commit()

        return {
            "romaji_jp_status":    romaji_status,
            "pinyin_chs_status":   pinyin_status,
            "hangul_roman_status": hangul_status,
            "backfilled_count":    len(backfilled),
        }
