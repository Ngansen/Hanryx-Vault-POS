"""
workers/zh_set_audit.py — TC + SC set completeness audit, plus
auto-refresh of canonical_sets/zh_sc.json from upstream PTCG-CHS-Datasets.

Two responsibilities, one worker, one nightly run:

  1. AUDIT — walk /mnt/cards/zh/<lang>/<source>/<set>/ and compare
     the on-disk card numbers against canonical_sets/zh_{tc,sc}.json's
     `expected_card_count` field. Per-set diff lands in zh_set_gap so
     the operator can see at a glance "set 460 (TC) is missing 7
     numbers" without diffing two file lists by hand.

  2. AUTO-REFRESH (SC only) — read PTCG-CHS-Datasets/ptcg_chs_infos.json
     and reconcile canonical_sets/zh_sc.json against it:
        * for EVERY existing entry, refresh the upstream-derivable
          fields (abbreviation, name_zh_sc, release_date,
          expected_card_count) — these are upstream-canonical and
          must not be frozen at a stale snapshot just because the
          operator confirmed jp_equivalent_id last quarter
        * preserve jp_equivalent_id and en_equivalent_id verbatim
          on every existing entry — those are the ONLY operator
          decisions in this file (which JP/EN release does this SC
          set physically correspond to?), and the worker must
          never silently revert that human judgement
        * append new collections from upstream that aren't in zh_sc
          yet, filling jp/en equivalents with the VERIFY sentinel
          so the operator's audit dashboard surfaces them
     Only writes the file if at least one field actually changed —
     don't bump mtime on a no-op pass.

  TC has no Ngansen fork and no upstream JSON to mirror, so this
  worker only AUDITS for TC; refresh is a no-op.

Idempotent. Safe to interrupt mid-run; partial UPSERT progress is
just continued on the next pass.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .base import Worker, WorkerError

log = logging.getLogger("workers.zh_set_audit")

DEFAULT_USB_ROOT = Path(os.environ.get("USB_CARDS_ROOT", "/mnt/cards"))
DEFAULT_ZH_ROOT = DEFAULT_USB_ROOT / "zh"

# Upstream PTCG-CHS-Datasets is cloned by Phase A under MIRROR_ROOT/<repo>.
# The infos file lives at the repo root.
DEFAULT_SC_INFOS_PATH = (
    DEFAULT_USB_ROOT / "PTCG-CHS-Datasets" / "ptcg_chs_infos.json"
)

VERIFY_SENTINEL = "VERIFY"

# Per (lang_dir on disk, lang_variant in zh_set_gap, canonical filename).
LANG_VARIANTS: tuple[tuple[str, str, str], ...] = (
    ("zh-tc", "TC", "zh_tc.json"),
    ("zh-sc", "SC", "zh_sc.json"),
)


# ─── Helpers ─────────────────────────────────────────────────────────────

def _normalise_number(raw: str) -> str:
    """Same convention every ZH helper uses: strip leading zeros so
    '001' and '1' compare equal; empty becomes '0'."""
    s = (raw or "").strip().lstrip("0")
    return s or "0"


def _num_sort_key(s: str) -> tuple[int, str]:
    """Sort '1' before '10' before '2'. Tuple form: (length, raw)
    falls back lexicographically for non-numeric numbers like 'TG01'."""
    return (len(s), s)


def _walk_sets(zh_root: Path, lang_dir: str) -> dict[str, set[str]]:
    """Returns {set_id: {normalised_card_number, ...}} for one lang.
    Walks ALL sources under that lang and merges — a card present in
    the primary source but missing from a fallback still counts as
    'on disk' since we can serve it."""
    out: dict[str, set[str]] = {}
    base = zh_root / lang_dir
    if not base.is_dir():
        return out
    for source_dir in base.iterdir():
        if not source_dir.is_dir() or source_dir.name.startswith("."):
            continue
        for set_dir in source_dir.iterdir():
            if not set_dir.is_dir() or set_dir.name.startswith("."):
                continue
            for f in set_dir.iterdir():
                if not f.is_file() or f.name.startswith("."):
                    continue
                if not any(c.isdigit() for c in f.stem):
                    continue
                out.setdefault(set_dir.name, set()).add(_normalise_number(f.stem))
    return out


def _load_canonical(canonical_dir: Path, fname: str) -> dict[str, dict[str, Any]]:
    """Returns {set_id: entry}. Empty + warning on missing/malformed."""
    p = canonical_dir / fname
    if not p.is_file():
        log.warning("[zh-audit] canonical sets file missing: %s", p)
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("[zh-audit] could not load %s: %s", p, exc)
        return {}
    sets = raw.get("sets") if isinstance(raw, dict) else None
    if not isinstance(sets, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for s in sets:
        if isinstance(s, dict):
            sid = (s.get("set_id") or "").strip()
            if sid:
                out[sid] = s
    return out


def _read_canonical_doc(canonical_dir: Path, fname: str) -> dict[str, Any]:
    """Reads the FULL canonical JSON (schema + sets) for the refresh
    pass. Returns a skeleton if the file is missing so refresh can
    bootstrap from scratch."""
    p = canonical_dir / fname
    if not p.is_file():
        return {"_schema": {"version": 1}, "sets": []}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"_schema": {"version": 1}, "sets": []}
    if not isinstance(raw, dict):
        return {"_schema": {"version": 1}, "sets": []}
    raw.setdefault("_schema", {"version": 1})
    raw.setdefault("sets", [])
    return raw


def _read_sc_infos(path: Path) -> list[dict[str, Any]]:
    """Returns ptcg_chs_infos.json's `collections[]` (the canonical
    upstream SC set list). Empty + warning on missing/malformed —
    audit/refresh just becomes a no-op for SC, which beats crashing."""
    if not path.is_file():
        log.warning("[zh-audit] SC infos file missing: %s — refresh skipped",
                    path)
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("[zh-audit] SC infos malformed (%s) — refresh skipped",
                    exc)
        return []
    cols = raw.get("collections") if isinstance(raw, dict) else None
    if not isinstance(cols, list):
        return []
    return [c for c in cols if isinstance(c, dict)]


def _refresh_sc_canonical(
    *,
    canonical_dir: Path,
    fname: str,
    upstream: list[dict[str, Any]],
) -> tuple[bool, int, int, int]:
    """Apply the auto-refresh rules to canonical_sets/zh_sc.json.

    Rules:
      * For every existing entry, refresh the upstream-derivable
        fields (abbreviation, name_zh_sc, release_date,
        expected_card_count) from upstream. jp_equivalent_id and
        en_equivalent_id are NEVER touched here regardless of value
        — they are the operator's confirmed mapping (or 'VERIFY'
        if not yet decided) and only the operator should change
        them. preserved_count counts entries where the upstream
        values matched and nothing was rewritten; refreshed_count
        counts entries where at least one derivable field moved.
      * New collection from upstream not in canonical → append with
        jp_equivalent_id='VERIFY' and en_equivalent_id='VERIFY'.

    Writes the file only if anything changed. Returns
    (wrote_file, preserved_count, refreshed_count, appended_count)."""
    doc = _read_canonical_doc(canonical_dir, fname)
    by_id: dict[str, dict[str, Any]] = {}
    for s in doc["sets"]:
        if isinstance(s, dict):
            sid = (s.get("set_id") or "").strip()
            if sid:
                by_id[sid] = s

    preserved = 0
    refreshed = 0
    appended = 0
    changed = False

    for col in upstream:
        sid = str(col.get("id", "")).strip()
        if not sid:
            continue
        derived_abbrev = (col.get("commodityCode") or "").strip() or VERIFY_SENTINEL
        derived_name = (col.get("name") or "").strip()
        derived_date = (col.get("salesDate") or "").strip() or VERIFY_SENTINEL
        cards = col.get("cards") if isinstance(col.get("cards"), list) else []
        derived_count = len(cards) if cards else "VERIFY"

        existing = by_id.get(sid)
        if existing is None:
            new_entry = {
                "set_id": sid,
                "abbreviation": derived_abbrev,
                "jp_equivalent_id": VERIFY_SENTINEL,
                "en_equivalent_id": VERIFY_SENTINEL,
                "expected_card_count": derived_count,
                "release_date": derived_date,
                "name_zh_sc": derived_name or f"VERIFY (collection {sid})",
            }
            doc["sets"].append(new_entry)
            by_id[sid] = new_entry
            appended += 1
            changed = True
            continue

        # Refresh upstream-derivable fields on EVERY existing entry,
        # whether or not the operator has confirmed jp_equivalent_id.
        # The protected surface is intentionally narrow: ONLY
        # jp_equivalent_id and en_equivalent_id are operator
        # decisions (they encode "is SC set 47 the same physical
        # release as JP set sv5K?" which only a human can answer).
        # Everything else (abbreviation, name, release date, card
        # count) is upstream-canonical and SHOULD reflect the latest
        # PTCG-CHS-Datasets state on every nightly pass — otherwise
        # an operator who confirmed jp_equivalent_id three months
        # ago is permanently frozen at three-month-old card counts.
        # Compare-then-set so we only mark `changed` when something
        # actually moves and we don't bump the file mtime on no-ops.
        local_changed = False
        for key, val in (
            ("abbreviation", derived_abbrev),
            ("release_date", derived_date),
            ("expected_card_count", derived_count),
        ):
            if existing.get(key) != val:
                existing[key] = val
                local_changed = True
        if derived_name and existing.get("name_zh_sc") != derived_name:
            existing["name_zh_sc"] = derived_name
            local_changed = True
        if local_changed:
            refreshed += 1
            changed = True
        else:
            preserved += 1

    if changed:
        # Stable-sort by set_id (numeric where possible) so the diff
        # of repeated runs is minimal. SC ids are mostly numeric
        # strings; non-numeric fall to the end alphabetically.
        def _k(e: dict[str, Any]) -> tuple[int, int, str]:
            sid = str(e.get("set_id", ""))
            try:
                return (0, int(sid), sid)
            except ValueError:
                return (1, 0, sid)
        doc["sets"].sort(key=_k)
        # Atomic write: tmp + replace, never partial JSON on disk.
        out_path = canonical_dir / fname
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, out_path)
    return changed, preserved, refreshed, appended


# ─── Worker ──────────────────────────────────────────────────────────────


class ZhSetAuditWorker(Worker):
    TASK_TYPE = "zh_set_audit"
    BATCH_SIZE = 1
    IDLE_SLEEP_S = 300.0
    CLAIM_TIMEOUT_S = 900       # 15 min ceiling — disk walk + UPSERT

    def __init__(
        self,
        conn,
        *,
        zh_root: Optional[Path] = None,
        canonical_dir: Optional[Path] = None,
        sc_infos_path: Optional[Path] = None,
        skip_sc_refresh: bool = False,
        now_fn=None,
        **kw,
    ):
        super().__init__(conn, **kw)
        self._zh_root = Path(zh_root) if zh_root is not None else DEFAULT_ZH_ROOT
        if canonical_dir is not None:
            self._canonical_dir = Path(canonical_dir)
        else:
            self._canonical_dir = (
                Path(__file__).resolve().parent.parent
                / "scripts" / "canonical_sets"
            )
        self._sc_infos_path = (
            Path(sc_infos_path) if sc_infos_path is not None
            else DEFAULT_SC_INFOS_PATH
        )
        self._skip_sc_refresh = bool(skip_sc_refresh)
        self._now_fn = now_fn or (lambda: int(time.time()))

    # ── Seeding ──────────────────────────────────────────────────

    def seed(self) -> int:
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
        now = self._now_fn()

        # 1. Auto-refresh SC canonical from upstream FIRST so the
        #    audit pass below uses the freshest expected_card_count
        #    values. Never auto-refresh TC (no upstream JSON exists).
        refresh_summary = {
            "wrote": False, "preserved": 0, "refreshed": 0, "appended": 0,
        }
        if not self._skip_sc_refresh:
            upstream = _read_sc_infos(self._sc_infos_path)
            wrote, preserved, refreshed, appended = _refresh_sc_canonical(
                canonical_dir=self._canonical_dir,
                fname="zh_sc.json",
                upstream=upstream,
            )
            refresh_summary = {
                "wrote": wrote, "preserved": preserved,
                "refreshed": refreshed, "appended": appended,
            }

        # 2. Audit each lang variant.
        audited = 0
        for lang_dir, lang_variant, fname in LANG_VARIANTS:
            canonical = _load_canonical(self._canonical_dir, fname)
            actual = _walk_sets(self._zh_root, lang_dir)

            # Union of keys — any set on either side gets a row.
            all_set_ids = set(canonical) | set(actual)
            for sid in all_set_ids:
                entry = canonical.get(sid, {})
                expected_field = entry.get("expected_card_count", 0)
                expected = (int(expected_field)
                            if isinstance(expected_field, int)
                            or (isinstance(expected_field, str)
                                and expected_field.isdigit())
                            else 0)
                actual_nums = actual.get(sid, set())

                # Build the canonical "expected numbers" list. When
                # we don't know per-card numbers, fall back to
                # 1..expected_count as the implied expectation.
                if expected > 0:
                    expected_nums = {str(i) for i in range(1, expected + 1)}
                else:
                    expected_nums = set()

                missing = sorted(expected_nums - actual_nums, key=_num_sort_key)
                extras = sorted(actual_nums - expected_nums,
                                key=_num_sort_key) if expected_nums else []

                self._upsert_gap(
                    set_id=sid,
                    lang_variant=lang_variant,
                    expected_count=expected,
                    actual_count=len(actual_nums),
                    missing=missing,
                    extras=extras,
                    now=now,
                )
                audited += 1

        self.conn.commit()
        return {
            "sets_audited": audited,
            "sc_refresh": refresh_summary,
        }

    # ── DB write ─────────────────────────────────────────────────

    def _upsert_gap(
        self,
        *,
        set_id: str,
        lang_variant: str,
        expected_count: int,
        actual_count: int,
        missing: list[str],
        extras: list[str],
        now: int,
    ) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO zh_set_gap
                (set_id, lang_variant, expected_count, actual_count,
                 missing_numbers, extra_numbers, audited_at)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
            ON CONFLICT (set_id, lang_variant) DO UPDATE
              SET expected_count  = EXCLUDED.expected_count,
                  actual_count    = EXCLUDED.actual_count,
                  missing_numbers = EXCLUDED.missing_numbers,
                  extra_numbers   = EXCLUDED.extra_numbers,
                  audited_at      = EXCLUDED.audited_at
            """,
            (
                set_id,
                lang_variant,
                int(expected_count),
                int(actual_count),
                json.dumps(missing),
                json.dumps(extras),
                now,
            ),
        )
