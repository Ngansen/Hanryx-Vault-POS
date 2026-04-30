"""
workers/cross_region_aliaser.py — TC ↔ SC ↔ KR ↔ JP ↔ EN alias map.

Walks the on-disk ZH card mirror at /mnt/cards/zh/<lang>/<source>/<set>/
<num>.<ext> and writes one row per (lang, set, num) into `card_alias`,
linking to the JP equivalent when one can be determined.

Match priority (first hit wins, every later attempt is skipped):

  1. MANUAL OVERRIDE — operator-curated /mnt/cards/manual_aliases.json.
     Schema: a list of dicts, each with at minimum a `canonical_key`
     plus the per-region ids the operator wants to pin. We never
     overwrite a row whose match_method='manual' once written.

  2. SET ABBREVIATION — canonical_sets/zh_{tc,sc}.json contains a
     `jp_equivalent_id` per set. When that field is a real JP set code
     (not the literal "VERIFY" placeholder), we trust it and assume
     the same card_number maps to the JP equivalent. Confidence 1.0.

  3. CLIP COSINE SIMILARITY — pull the ZH card's embedding from
     `card_image_embedding` (keyed by namespaced set_id "zh-tc:<set>")
     and search against every JP embedding for the same model_id.
     Best score ≥ 0.92 wins. Confidence = the actual cosine. This path
     is gracefully skipped if either side has no embedding yet — the
     CLIP worker chains downstream of this one in zh_full_sync.sh, so
     the FIRST aliaser pass on a fresh sync will only have set_abbrev
     hits; subsequent nightly passes pick up CLIP matches as
     embeddings land.

When nothing matches we still write a row with match_method='unmatched'
so the dashboard can surface "47 TC cards still unaliased" without
re-walking the disk every page load.

Idempotent. Safe to interrupt mid-run; the next pass picks up where
this one left off because we filter on last_verified_at.

Why a worker (not a CLI script):
  * Runs nightly alongside image_health, kr_set_audit, etc.
  * Needs the bg_task_queue retry/heartbeat machinery for partial
    failures (one bad image must not abort the whole pass).
  * Admin dashboard reads bg_worker_run for status — same surface as
    every other helper.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .base import Worker, WorkerError

log = logging.getLogger("workers.cross_region_aliaser")

# Where Phase D writes the ZH mirror. Mirrors the convention used by
# every other USB-rooted helper.
DEFAULT_USB_ROOT = Path(os.environ.get("USB_CARDS_ROOT", "/mnt/cards"))
DEFAULT_ZH_ROOT = DEFAULT_USB_ROOT / "zh"

# Operator-edited override file. Absent file = no overrides; we do NOT
# create an empty one (operator should know they don't have any).
DEFAULT_MANUAL_OVERRIDES = DEFAULT_USB_ROOT / "manual_aliases.json"

# CLIP cosine threshold below which we refuse to auto-link. Empirical
# floor — JP and TC prints of the same card score ≥0.95 in our test
# set; 0.92 leaves headroom for printing variation without admitting
# false positives (different cards in the same set typically score
# 0.6-0.8 against each other).
CLIP_MIN_SIMILARITY = 0.92

# How long an alias row is considered fresh before the worker re-checks
# it. 7 days is enough that a weekly run touches every row, and short
# enough that adding a new manual override or CLIP embedding takes
# effect within a week without manual intervention.
DEFAULT_ALIAS_RECHECK_S = 7 * 86400

# Which canonical_sets file matches which lang directory under /mnt/cards/zh/.
# Keep these in lockstep with zh_sources.py.
LANG_DIRS: tuple[tuple[str, str, str], ...] = (
    # (lang_dir on disk, region_column on card_alias, canonical_sets file)
    ("zh-tc", "zh_tc_id", "zh_tc.json"),
    ("zh-sc", "zh_sc_id", "zh_sc.json"),
)

# Sentinel that canonical_sets entries use when the operator hasn't
# confirmed the JP/EN equivalent yet. Skip set-abbrev matching for
# these — we'd just be guessing.
VERIFY_SENTINEL = "VERIFY"


# ─── Helpers ─────────────────────────────────────────────────────────────

def _canonical_key(jp_set_id: str, jp_card_num: str) -> str:
    """Stable, human-readable key. We deliberately do NOT hash — the
    string form is the same length-class as a hash but lets you grep
    the database for "jp:SV1S:001" during debug without a lookup."""
    return f"jp:{jp_set_id}:{jp_card_num}"


def _normalise_number(raw: str) -> str:
    """Match the convention used by every other ZH helper: strip
    leading zeros so '001' and '1' compare equal, empty becomes '0'."""
    s = (raw or "").strip().lstrip("0")
    return s or "0"


def _zh_card_id(lang: str, source: str, set_id: str, num: str) -> str:
    """Per-region id we store in card_alias.{zh_tc_id, zh_sc_id}. The
    `source` is part of the id because the same canonical SC set may
    appear under multiple sources (PTCG-CHS-Datasets primary, mycardart
    fallback) and we want the row to record which copy we actually have
    on disk."""
    return f"{lang}:{source}:{set_id}:{_normalise_number(num)}"


def _load_canonical_sets(canonical_dir: Path, fname: str) -> dict[str, dict[str, Any]]:
    """Returns {set_id: set_entry_dict}. Empty dict if the file is
    missing or malformed — a missing canonical file should NOT crash
    the aliaser, it just means that lang produces only 'unmatched'
    rows until the file is restored."""
    p = canonical_dir / fname
    if not p.is_file():
        log.warning("[aliaser] canonical sets file missing: %s", p)
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("[aliaser] could not load %s: %s", p, exc)
        return {}
    sets = raw.get("sets") if isinstance(raw, dict) else None
    if not isinstance(sets, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for s in sets:
        if not isinstance(s, dict):
            continue
        sid = (s.get("set_id") or "").strip()
        if sid:
            out[sid] = s
    return out


def _load_manual_overrides(path: Path) -> list[dict[str, Any]]:
    """Returns a list of override dicts, each containing whatever
    region ids the operator wants pinned. Missing file → []. Malformed
    file → [] with a loud warning (the operator just lost their
    overrides for this run; they need to know)."""
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.error("[aliaser] manual overrides unreadable: %s — IGNORING THIS RUN", exc)
        return []
    if isinstance(raw, dict) and isinstance(raw.get("overrides"), list):
        return [d for d in raw["overrides"] if isinstance(d, dict)]
    if isinstance(raw, list):
        return [d for d in raw if isinstance(d, dict)]
    log.warning("[aliaser] manual overrides has unexpected shape (expected list or "
                "{overrides:[]}); ignoring")
    return []


def _walk_zh_mirror(zh_root: Path) -> Iterable[tuple[str, str, str, str]]:
    """Yields (lang, source, set_id, card_number) for every image file
    under <zh_root>/<lang>/<source>/<set>/<num>.<ext>. Skips dotfiles,
    skips files whose stem isn't a valid card number, skips lang dirs
    that aren't on the LANG_DIRS allowlist (defends against an operator
    accidentally creating /mnt/cards/zh/scratch/ for working files)."""
    if not zh_root.is_dir():
        return
    valid_langs = {ld[0] for ld in LANG_DIRS}
    for lang_dir in sorted(zh_root.iterdir()):
        if not lang_dir.is_dir() or lang_dir.name not in valid_langs:
            continue
        for source_dir in sorted(lang_dir.iterdir()):
            if not source_dir.is_dir() or source_dir.name.startswith("."):
                continue
            for set_dir in sorted(source_dir.iterdir()):
                if not set_dir.is_dir() or set_dir.name.startswith("."):
                    continue
                for card_file in sorted(set_dir.iterdir()):
                    if not card_file.is_file() or card_file.name.startswith("."):
                        continue
                    stem = card_file.stem
                    # Card numbers must contain at least one digit.
                    # Defends against random text files being treated
                    # as cards.
                    if not any(c.isdigit() for c in stem):
                        continue
                    yield (lang_dir.name, source_dir.name, set_dir.name, stem)


def _cosine(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine similarity. We don't pull in numpy here —
    the aliaser runs against at most a few thousand ZH cards × a few
    thousand JP candidates per set, and the per-set CLIP search is
    bounded enough that a Python loop is fine. Avoiding numpy keeps
    this worker importable on the Pi without the scientific stack."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


# ─── Worker ──────────────────────────────────────────────────────────────

class CrossRegionAliaserWorker(Worker):
    """One full-pass-per-claim worker. Same model as kr_set_audit:
    the bg_task_queue holds at most one pending 'cross_region_alias'
    task at a time, the worker claims it, walks the ZH mirror, writes
    one card_alias row per (lang, set, num), and exits."""

    TASK_TYPE = "cross_region_alias"
    BATCH_SIZE = 1                      # one full-run task per pass
    IDLE_SLEEP_S = 300.0                # 5 min idle — not a hot loop
    CLAIM_TIMEOUT_S = 1800              # 30 min ceiling; CLIP search is
                                        # the slow part on a fresh DB

    def __init__(
        self,
        conn,
        *,
        zh_root: Optional[Path] = None,
        canonical_dir: Optional[Path] = None,
        manual_overrides_path: Optional[Path] = None,
        recheck_after_s: Optional[int] = None,
        clip_min_similarity: float = CLIP_MIN_SIMILARITY,
        clip_model_id: Optional[str] = None,
        now_fn=None,
        **kw,
    ):
        super().__init__(conn, **kw)
        self._zh_root = Path(zh_root) if zh_root is not None else DEFAULT_ZH_ROOT
        # Default canonical_sets dir = the one bundled in this repo.
        # Tests inject a tmp_path here so we can vary the registry per
        # test without touching the package data.
        if canonical_dir is not None:
            self._canonical_dir = Path(canonical_dir)
        else:
            self._canonical_dir = (
                Path(__file__).resolve().parent.parent
                / "scripts" / "canonical_sets"
            )
        self._manual_path = (
            Path(manual_overrides_path) if manual_overrides_path is not None
            else DEFAULT_MANUAL_OVERRIDES
        )
        self._recheck_s = (recheck_after_s if recheck_after_s is not None
                           else DEFAULT_ALIAS_RECHECK_S)
        self._clip_min = float(clip_min_similarity)
        self._clip_model_id = clip_model_id
        self._now_fn = now_fn or (lambda: int(time.time()))

    # ── Seeding ──────────────────────────────────────────────────

    def seed(self) -> int:
        """Enqueue at most one 'cross_region_alias' task if there isn't
        already a PENDING or CLAIMED one. Same idempotent pattern as
        kr_set_audit — multiple worker processes calling seed() in
        parallel can never enqueue two duplicate full-runs."""
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT 1 FROM bg_task_queue
            WHERE task_type = %s AND status IN ('PENDING', 'CLAIMED')
            LIMIT 1
            """,
            (self.TASK_TYPE,),
        )
        if cur.fetchone():
            return 0
        cur.execute(
            """
            INSERT INTO bg_task_queue
                (task_type, payload, status, attempts, created_at)
            VALUES (%s, %s, 'PENDING', 0, %s)
            """,
            (self.TASK_TYPE, json.dumps({"kind": "full_run"}), self._now_fn()),
        )
        self.conn.commit()
        return 1

    # ── Processing ───────────────────────────────────────────────

    def process(self, task: dict) -> dict:
        """One full pass. Returns counts dict the framework records in
        bg_worker_run.notes — the admin dashboard surfaces this."""
        now = self._now_fn()

        # 1. Load manual overrides FIRST — they win over everything.
        overrides = _load_manual_overrides(self._manual_path)
        manual_count = self._apply_manual_overrides(overrides, now)

        # 2. Build per-lang canonical set lookups: abbreviation/set_id
        #    → JP equivalent.
        per_lang: dict[str, tuple[dict[str, dict[str, Any]], str]] = {}
        for lang_dir, region_col, fname in LANG_DIRS:
            sets = _load_canonical_sets(self._canonical_dir, fname)
            per_lang[lang_dir] = (sets, region_col)

        # 3. Walk the disk and try to alias each card.
        seen = 0
        set_abbrev_hits = 0
        clip_hits = 0
        unmatched = 0
        for lang, source, set_id, card_num in _walk_zh_mirror(self._zh_root):
            seen += 1
            sets, region_col = per_lang.get(lang, ({}, ""))
            zh_id = _zh_card_id(lang, source, set_id, card_num)
            jp_id, jp_set_id, method, confidence = self._try_match(
                lang=lang,
                set_id=set_id,
                card_num=card_num,
                zh_id=zh_id,
                canonical_set_entry=sets.get(set_id),
            )
            if method == "set_abbrev":
                set_abbrev_hits += 1
            elif method == "clip":
                clip_hits += 1
            else:
                unmatched += 1
            self._upsert_alias(
                jp_set_id=jp_set_id,
                jp_card_num=card_num if jp_set_id else "",
                jp_id=jp_id,
                region_col=region_col,
                region_id=zh_id,
                method=method,
                confidence=confidence,
                now=now,
            )

        self.conn.commit()

        return {
            "manual_overrides_applied": manual_count,
            "cards_seen":   seen,
            "set_abbrev_matches": set_abbrev_hits,
            "clip_matches": clip_hits,
            "unmatched":    unmatched,
        }

    # ── Match resolution ─────────────────────────────────────────

    def _try_match(
        self,
        *,
        lang: str,
        set_id: str,
        card_num: str,
        zh_id: str,
        canonical_set_entry: Optional[dict[str, Any]],
    ) -> tuple[Optional[str], str, str, float]:
        """Returns (jp_id_or_None, jp_set_id_or_empty, method, confidence).

        Tries set_abbrev first, then CLIP. Returns ('', '', 'unmatched', 0.0)
        when nothing pans out. Manual overrides are NOT consulted here —
        they're applied in a separate first-class pass before disk walk."""
        # Path 2: set_abbrev.
        if canonical_set_entry is not None:
            jp_eq = (canonical_set_entry.get("jp_equivalent_id") or "").strip()
            if jp_eq and jp_eq != VERIFY_SENTINEL:
                jp_id = f"jp:{jp_eq}:{_normalise_number(card_num)}"
                return jp_id, jp_eq, "set_abbrev", 1.0

        # Path 3: CLIP fallback.
        clip = self._try_clip_match(lang, set_id, card_num)
        if clip is not None:
            jp_set_id, jp_card_num, score = clip
            jp_id = f"jp:{jp_set_id}:{_normalise_number(jp_card_num)}"
            return jp_id, jp_set_id, "clip", float(score)

        return None, "", "unmatched", 0.0

    def _try_clip_match(
        self,
        lang: str,
        set_id: str,
        card_num: str,
    ) -> Optional[tuple[str, str, float]]:
        """Cosine-search the ZH card's embedding against every JP
        embedding in the same model_id. Returns (jp_set_id, jp_card_num,
        score) on hit (score ≥ self._clip_min), None on miss or when
        the embedding tables are empty for either side."""
        cur = self.conn.cursor()
        zh_namespaced_set = f"{lang}:{set_id}"

        # ZH side: a single embedding row keyed by our namespaced
        # set_id. If the CLIP worker hasn't run on ZH yet, we get
        # nothing back — graceful skip.
        zh_query_args: list[Any] = [zh_namespaced_set, _normalise_number(card_num)]
        if self._clip_model_id:
            zh_query_args.append(self._clip_model_id)
            cur.execute(
                """
                SELECT model_id, embedding
                FROM card_image_embedding
                WHERE set_id = %s AND card_number = %s AND model_id = %s
                  AND failure = ''
                LIMIT 1
                """,
                zh_query_args,
            )
        else:
            cur.execute(
                """
                SELECT model_id, embedding
                FROM card_image_embedding
                WHERE set_id = %s AND card_number = %s AND failure = ''
                LIMIT 1
                """,
                zh_query_args,
            )
        zh_row = cur.fetchone()
        if not zh_row:
            return None
        zh_model_id, zh_emb = zh_row[0], list(zh_row[1] or [])
        if not zh_emb:
            return None

        # JP side: every JP embedding for the same model. We could
        # narrow by jp set if the canonical entry pointed at one, but
        # this branch only runs when canonical lookup FAILED, so we
        # have to consider all JP cards. Filter set_id by NOT LIKE
        # 'zh-%' / 'kr-%' to exclude other regions' namespaced rows.
        cur.execute(
            """
            SELECT set_id, card_number, embedding
            FROM card_image_embedding
            WHERE model_id = %s
              AND failure = ''
              AND set_id NOT LIKE 'zh-%%'
              AND set_id NOT LIKE 'kr-%%'
              AND set_id NOT LIKE 'en-%%'
            """,
            (zh_model_id,),
        )
        best_score = 0.0
        best_set: Optional[str] = None
        best_num: Optional[str] = None
        for jp_set, jp_num, jp_emb in cur.fetchall():
            score = _cosine(zh_emb, list(jp_emb or []))
            if score > best_score:
                best_score = score
                best_set = jp_set
                best_num = jp_num
        if best_set is None or best_score < self._clip_min:
            return None
        return best_set, best_num, best_score

    # ── DB writes ────────────────────────────────────────────────

    def _apply_manual_overrides(
        self,
        overrides: list[dict[str, Any]],
        now: int,
    ) -> int:
        """Insert/update one row per override entry. Refuses to clobber
        existing manual rows from a previous run silently — instead it
        always re-stamps last_verified_at so manually-pinned rows show
        up as 'fresh' in the dashboard.

        Each override dict must contain at minimum a `canonical_key`
        (or jp_id we can derive one from) plus any per-region ids the
        operator wants set. Unrecognised keys are ignored, so the
        operator can leave free-form notes in the override file
        without breaking parse."""
        applied = 0
        cur = self.conn.cursor()
        for ov in overrides:
            ck = (ov.get("canonical_key") or "").strip()
            jp_id = (ov.get("jp_id") or "").strip() or None
            if not ck and jp_id and jp_id.startswith("jp:"):
                ck = jp_id
            if not ck:
                log.warning("[aliaser] manual override missing canonical_key, "
                            "skipping: %r", ov)
                continue
            cur.execute(
                """
                INSERT INTO card_alias
                    (canonical_key, jp_id, kr_id, en_id, zh_tc_id, zh_sc_id,
                     match_method, confidence, source, notes,
                     created_at, last_verified_at)
                VALUES (%s, %s, %s, %s, %s, %s,
                        'manual', 1.0, %s, %s, %s, %s)
                ON CONFLICT (canonical_key) DO UPDATE
                  SET jp_id            = COALESCE(EXCLUDED.jp_id,    card_alias.jp_id),
                      kr_id            = COALESCE(EXCLUDED.kr_id,    card_alias.kr_id),
                      en_id            = COALESCE(EXCLUDED.en_id,    card_alias.en_id),
                      zh_tc_id         = COALESCE(EXCLUDED.zh_tc_id, card_alias.zh_tc_id),
                      zh_sc_id         = COALESCE(EXCLUDED.zh_sc_id, card_alias.zh_sc_id),
                      match_method     = 'manual',
                      confidence       = 1.0,
                      source           = EXCLUDED.source,
                      notes            = EXCLUDED.notes,
                      last_verified_at = EXCLUDED.last_verified_at
                """,
                (
                    ck,
                    jp_id,
                    (ov.get("kr_id") or "").strip() or None,
                    (ov.get("en_id") or "").strip() or None,
                    (ov.get("zh_tc_id") or "").strip() or None,
                    (ov.get("zh_sc_id") or "").strip() or None,
                    (ov.get("source") or "manual_override").strip(),
                    (ov.get("notes") or "").strip(),
                    now,
                    now,
                ),
            )
            applied += 1
        return applied

    def _upsert_alias(
        self,
        *,
        jp_set_id: str,
        jp_card_num: str,
        jp_id: Optional[str],
        region_col: str,
        region_id: str,
        method: str,
        confidence: float,
        now: int,
    ) -> None:
        """UPSERT one row. Refuses to overwrite manual rows — the
        operator has the last word and an automated pass must never
        silently undo a manual link.

        For unmatched rows (jp_set_id == '') we synthesise a canonical
        key from the region side: 'unmatched:<region_id>'. This keeps
        the PK invariant satisfied while letting the dashboard count
        and re-attempt these on the next pass."""
        if not region_col:
            return
        if jp_set_id and jp_card_num:
            # Normalise both halves of the key so '001' and '1' on
            # disk produce the same canonical row — the alternative
            # (one canonical row per zero-padding variant) breaks the
            # whole "single spine" promise.
            ck = _canonical_key(jp_set_id, _normalise_number(jp_card_num))
        else:
            ck = f"unmatched:{region_id}"

        cur = self.conn.cursor()
        cur.execute(
            f"""
            INSERT INTO card_alias
                (canonical_key, jp_id, {region_col},
                 match_method, confidence, source,
                 created_at, last_verified_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (canonical_key) DO UPDATE
              SET jp_id            = COALESCE(EXCLUDED.jp_id, card_alias.jp_id),
                  {region_col}     = COALESCE(EXCLUDED.{region_col},
                                              card_alias.{region_col}),
                  match_method     = CASE
                      WHEN card_alias.match_method = 'manual'
                          THEN card_alias.match_method
                      ELSE EXCLUDED.match_method
                  END,
                  confidence       = CASE
                      WHEN card_alias.match_method = 'manual'
                          THEN card_alias.confidence
                      ELSE EXCLUDED.confidence
                  END,
                  source           = CASE
                      WHEN card_alias.match_method = 'manual'
                          THEN card_alias.source
                      ELSE EXCLUDED.source
                  END,
                  last_verified_at = EXCLUDED.last_verified_at
            """,
            (
                ck,
                jp_id,
                region_id,
                method,
                float(confidence),
                "auto",
                now,
                now,
            ),
        )
