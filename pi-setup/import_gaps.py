"""
Self-healing gap detector for the multi-language card imports.

What it does
------------
After every import (or on operator demand from the admin UI), it walks
every alias cluster and asks two questions per language table:

  1. How many cards do we have stored under this set's known codes?
  2. How many should we have, according to upstream metadata?

The answer to (2) comes from the synced pokemontcg.io catalogue
(``$HV/data/set_aliases_synced.json`` carries each set's id and we
keep a rolling ``sets_meta`` row with ``total`` per set).  When (1) <
(2), we compute the *missing card numbers* and hand them back as
actionable gap-fill jobs that the importers can pull instead of
re-running a full refresh.

This closes the import loop: imports stop being silently incomplete.

Public API
----------
    refresh_set_totals(conn)            → refresh expected totals from sources
    cluster_gaps(conn, cluster_name)    → detailed gap report for one cluster
    all_gaps(conn, *, min_missing=1)    → list[dict] of every gap, sorted
    suggest_backfill_jobs(conn, ...)    → list[(source, set_code, [numbers])]
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Iterable

log = logging.getLogger("import_gaps")


_DDL = [
    """
    CREATE TABLE IF NOT EXISTS sets_meta (
        cluster        TEXT NOT NULL,
        set_code       TEXT NOT NULL,
        source         TEXT NOT NULL,
        expected_total INTEGER NOT NULL DEFAULT 0,
        release_date   TEXT NOT NULL DEFAULT '',
        updated_at     BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (cluster, set_code, source)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sets_meta_cluster ON sets_meta (cluster)",
]


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        for stmt in _DDL:
            cur.execute(stmt)
    conn.commit()


# ── totals refresh ──────────────────────────────────────────────────────────
def _data_dir() -> str:
    base = os.environ.get("HV") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "data")


def _synced_clusters() -> list[dict]:
    p = os.path.join(_data_dir(), "set_aliases_synced.json")
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            return list(json.load(f) or [])
    except Exception as exc:
        log.info("[import_gaps] could not read synced aliases: %s", exc)
        return []


def refresh_set_totals(conn) -> dict:
    """
    Refresh the ``sets_meta.expected_total`` table from pokemontcg.io.

    Uses the synced catalogue's per-set ``total`` field (each cluster's
    ``_meta`` block carries id, ptcgoCode, releaseDate; we fetch the
    actual card count using the same /v2/cards endpoint with a
    pageSize=1 trick to read totalCount cheaply).
    """
    try:
        import http_client
    except Exception:
        http_client = None

    clusters = _synced_clusters()
    if not clusters:
        return {"ok": False, "reason": "no synced catalogue yet"}

    api_key = os.environ.get("POKEMONTCG_API_KEY", "").strip()
    headers = {}
    if api_key:
        headers["X-Api-Key"] = api_key

    inserted = 0
    for cl in clusters:
        meta = cl.get("_meta") or {}
        sid = (meta.get("id") or "").strip()
        if not sid:
            continue
        url = f"https://api.pokemontcg.io/v2/cards?q=set.id:{sid}&pageSize=1&page=1"
        total = 0
        if http_client is not None:
            res = http_client.request(url, headers=headers, priority="background")
            if res is None:
                continue
            status, body, _ = res
            if status != 200:
                continue
            try:
                data = json.loads(body.decode("utf-8"))
                total = int(data.get("totalCount") or 0)
            except Exception:
                continue
        else:
            continue

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sets_meta
                    (cluster, set_code, source, expected_total,
                     release_date, updated_at)
                VALUES (%s, %s, 'pokemontcg.io', %s, %s, %s)
                ON CONFLICT (cluster, set_code, source) DO UPDATE
                  SET expected_total = EXCLUDED.expected_total,
                      release_date   = EXCLUDED.release_date,
                      updated_at     = EXCLUDED.updated_at
                """,
                (cl.get("name") or sid, sid, total,
                 meta.get("releaseDate") or "", int(time.time())),
            )
        inserted += 1
    conn.commit()
    return {"ok": True, "clusters_refreshed": inserted}


# ── gap math ────────────────────────────────────────────────────────────────
# For every cluster, we count rows in each language table whose set-id
# column matches any of the cluster's tokens.  If the count is below
# ``expected_total``, we build the set of integer card numbers we have
# and subtract from {1..expected_total} to get the missing numbers.

_LANG_COUNT_QUERIES = {
    "kr": ("cards_kr", """
        SELECT card_number FROM cards_kr
         WHERE LOWER(prod_code) = ANY(%s::text[])
            OR LOWER(set_name)  = ANY(%s::text[])
    """),
    "chs": ("cards_chs", """
        SELECT collection_number FROM cards_chs
         WHERE LOWER(commodity_code) = ANY(%s::text[])
            OR LOWER(commodity_name) = ANY(%s::text[])
    """),
    "jpn": ("cards_jpn", """
        SELECT card_number FROM cards_jpn
         WHERE LOWER(set_code) = ANY(%s::text[])
            OR LOWER(set_name) = ANY(%s::text[])
    """),
    "jpn_pocket": ("cards_jpn_pocket", """
        SELECT card_number::text FROM cards_jpn_pocket
         WHERE LOWER(set_code) = ANY(%s::text[])
    """),
    "multi": ("cards_multi", """
        SELECT card_number FROM cards_multi
         WHERE LOWER(set_code) = ANY(%s::text[])
            OR LOWER(set_name) = ANY(%s::text[])
    """),
}


def _intish(s: str) -> int | None:
    try:
        return int(str(s).split("/")[0].lstrip("0") or "0")
    except (TypeError, ValueError):
        return None


def _gap_for_cluster(conn, cluster: dict, expected: int) -> dict:
    tokens = sorted({t.strip().lower() for t in cluster.get("tokens", []) if t})
    out = {"cluster": cluster.get("name") or "?", "expected": expected,
           "by_lang": {}}
    for lang, (_, sql) in _LANG_COUNT_QUERIES.items():
        params = (tokens,) if sql.count("%s") == 1 else (tokens, tokens)
        nums: set[int] = set()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                for (raw,) in cur.fetchall():
                    n = _intish(raw)
                    if n is not None:
                        nums.add(n)
        except Exception as exc:
            log.info("[import_gaps] %s skipped for %s: %s",
                     lang, out["cluster"], exc)
        if expected > 0:
            missing = sorted(set(range(1, expected + 1)) - nums)
        else:
            missing = []
        out["by_lang"][lang] = {
            "have":    len(nums),
            "missing": missing[:200],   # cap for the response
            "missing_count": len(missing),
        }
    return out


def cluster_gaps(conn, cluster_name: str) -> dict | None:
    """Detailed gap report for one cluster by name (case-insensitive)."""
    try:
        import set_aliases as _sa
        clusters = _sa.all_clusters()
    except Exception as exc:
        log.info("[import_gaps] alias module unavailable: %s", exc)
        return None
    cluster = next((c for c in clusters
                    if (c.get("name") or "").lower() == cluster_name.lower()),
                   None)
    if not cluster:
        return None
    expected = _expected_for_cluster(conn, cluster.get("name") or "")
    return _gap_for_cluster(conn, cluster, expected)


def _expected_for_cluster(conn, cluster_name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(expected_total),0) FROM sets_meta WHERE cluster=%s",
            (cluster_name,),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def all_gaps(conn, *, min_missing: int = 1) -> list[dict]:
    """Every cluster with at least one missing card, sorted by total missing."""
    try:
        import set_aliases as _sa
        clusters = _sa.all_clusters()
    except Exception as exc:
        log.info("[import_gaps] alias module unavailable: %s", exc)
        return []
    out: list[dict] = []
    for cl in clusters:
        expected = _expected_for_cluster(conn, cl.get("name") or "")
        if expected <= 0:
            continue
        rep = _gap_for_cluster(conn, cl, expected)
        total_missing = sum(v["missing_count"] for v in rep["by_lang"].values())
        if total_missing >= min_missing:
            rep["total_missing"] = total_missing
            out.append(rep)
    out.sort(key=lambda r: -r["total_missing"])
    return out


def suggest_backfill_jobs(conn, *, max_jobs: int = 30) -> list[dict]:
    """
    Produce concrete jobs for the importers:
      [{"language": "kr", "cluster": "...", "set_codes": [...],
        "missing_numbers": [1, 5, 12, ...], "priority": "high"}]
    Higher priority = fewer missing numbers (cheap to fill) + a known
    high-volume cluster.
    """
    jobs: list[dict] = []
    for rep in all_gaps(conn):
        for lang, info in rep["by_lang"].items():
            if not info["missing_count"]:
                continue
            jobs.append({
                "language":        lang,
                "cluster":         rep["cluster"],
                "expected":        rep["expected"],
                "have":            info["have"],
                "missing_numbers": info["missing"],
                "missing_count":   info["missing_count"],
                # "priority" is used by cluster_backfill's queue.
                "priority": "high" if info["missing_count"] <= 25 else "background",
            })
    jobs.sort(key=lambda j: j["missing_count"])
    return jobs[:max_jobs]
