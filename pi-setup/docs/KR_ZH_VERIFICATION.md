# KR + ZH + EN Pipeline Verification Checklist

After a fresh Pi boot, after a `git pull`, after a UGreen-dock plug-in, or
any time the operator suspects the multilingual card pipelines are out
of sync with reality, run through this checklist **in order**. KR runs
first because it's the smallest pipeline and gives a fast smoke signal
that the core POS is up; ZH runs second because it depends on `bg_worker`
loops that take longer to settle; EN runs last because it shares the
TCGdex source with JP and is the lowest-priority pipeline at a Korean
trade-show booth.

All endpoints are unauthenticated read-only `GET`s on `localhost:8080`
(the booth Pi only) — they don't touch the admin portal so they're safe
to call from any operator terminal.

```bash
# Optional: point all the curls at the booth Pi over Tailscale
export PI=hanryxvault   # or 100.125.5.34
```

---

## Phase 0 — One-curl shortcut (`/admin/pipelines/status`)

Before walking the full checklist, hit the aggregated status endpoint —
it composes everything Phases 1-4 verify into a single JSON blob, so a
healthy response usually means you can skip straight to whatever scan or
sale you actually came here to do.

```bash
curl -s "http://${PI:-localhost:8080}/admin/pipelines/status" | jq .
```

**Expected shape** (truncated):

```json
{
  "languages": {
    "kr":  {"count": 12000, "gap": {"sets_audited": 180, "total_missing": 14}},
    "chs": {"count": 50000, "gap": {"sets_audited": 220, "total_missing": 87}},
    "tc":  {"count": 30000, "gap": {"sets_audited": 200, "total_missing": 42}},
    "jpn": {"count": 22000},
    "en":  {"count": 18000, "gap": {"sets_audited": 240, "total_missing": 35}}
  },
  "card_alias": {
    "by_match_method": {"set_abbrev": 9800, "clip": 420, "manual": 17}
  },
  "workers": {
    "kr_set_audit":         {"status": "OK",      "started_at": 1714492800, "items_ok": 180, "items_failed": 0},
    "zh_set_audit":         {"status": "OK",      "started_at": 1714492801, "items_ok": 220, "items_failed": 0},
    "en_set_audit":         {"status": "OK",      "started_at": 1714492802, "items_ok": 240, "items_failed": 0},
    "cross_region_alias":   {"status": "OK",      "started_at": 1714492803, "items_ok": 9800, "items_failed": 0}
  }
}
```

**Skip-the-checklist rule of thumb**: every `count` non-zero, every
worker `status` is `OK`, and `total_missing` is well under 10% of
`sets_audited × ~150 cards`. If anything is `FAILED`, `EMPTY_SOURCE`,
`PARTIAL`, or `RUNNING` longer than ~5 minutes, jump to the matching
Phase below. The endpoint is a single read-only transaction so it's
safe to alias as a per-second shell loop while debugging.

---

## Phase 1 — Korean (`cards_kr`) ✅

KR is an HTTP-only pipeline (no local mirror dependency); a green status
here means Postgres is reachable, the importer ran, and the dashboard's
admin status route is wired up.

### 1.1 Importer health

```bash
curl -s "http://${PI:-localhost:8080}/admin/kr-cards/status" | jq .
```

**Expected**:

```json
{
  "count": 12000,            // any number ≥ 10 000 — KR catalog is ~12k cards
  "sample": [
    {"name": "리자몽", "set": "SV1S", "number": "058", "rarity": "RR"},
    ...                      // five rows, most-recent imported_at first
  ]
}
```

**Red flags**:

| Symptom | Likely cause | Fix |
|---|---|---|
| `count: 0` | Importer never ran | Re-run `python3 pi-setup/import_kr_cards.py` |
| `count` < 10 000 | Importer crashed mid-run | Check `journalctl -u hanryx-pos` for the last `[import_kr_cards]` block |
| HTTP 500 | Postgres down | `docker compose ps db` — should be `healthy` |
| `sample` empty but `count` > 0 | `imported_at` was never set | Re-run the importer with `--force-reimport` |

### 1.2 Direct DB sanity (via the POS container)

```bash
docker compose exec pos psql -U vaultpos -d vaultpos -c \
  "SELECT prod_code, COUNT(*) FROM cards_kr GROUP BY prod_code ORDER BY 2 DESC LIMIT 10;"
```

**Expected**: top set is recent (e.g. SV9, SV10), each has 70-200 rows. No
single set should have `1` — that means the importer was killed during
that set.

### 1.3 OCR loop end-to-end

```bash
curl -sF "image=@samples/kr_charizard.jpg" \
  "http://${PI:-localhost:8080}/card/scan/ocr?lang=kor" | jq '.matches[0]'
```

**Expected**: a single match with `name_kr` populated and a `commodity` /
`prod_code` that matches the test image. If it returns `[]`, OCR is
producing text that doesn't intersect any `name_kr` in the table — usually
means tesseract's `kor` traineddata isn't installed in the POS image.

---

## Phase 2 — Chinese (`cards_chs` + `card_alias` + `zh_set_gap`) 🇨🇳

ZH has more moving parts: SC comes from a 5.6 GB local-mirror clone, TC
comes from a `ptcg.tw` scrape on cron, and both feed `card_alias` via the
nightly `cross_region_aliaser` worker. Verify in this order so you can
narrow which layer broke.

### 2.1 SC importer (`cards_chs`)

```bash
curl -s "http://${PI:-localhost:8080}/admin/chs-cards/status" | jq .
```

**Expected**:

```json
{
  "count": 50000,            // any number ≥ 40 000 — SC catalog is ~50k cards
  "sample": [
    {"name": "喷火龙", "set": "C-1.5A", "number": "048", "rarity": "RR"},
    ...
  ]
}
```

**Red flags**: same table as 1.1 — the importer pattern is identical, just
sourced from `Ngansen/PTCG-CHS-Datasets` instead of an HTTP API. If
`count` is 0, check that `/mnt/cards/PTCG-CHS-Datasets/` has been pulled
recently (`git -C /mnt/cards/PTCG-CHS-Datasets log -1`).

### 2.2 ZH set-gap audit (`zh_set_gap`)

This is the "have we seen every card in every set" check that ZH-3 / ZH-4
populate. A row here means the set was walked; missing rows mean the
walker hasn't reached that set yet.

```bash
docker compose exec pos psql -U vaultpos -d vaultpos -c "
SELECT set_id, lang_variant, expected_count, actual_count,
       jsonb_array_length(missing_numbers) AS missing
  FROM zh_set_gap
 ORDER BY audited_at DESC
 LIMIT 20;"
```

**Expected**: each row has `actual_count` close to `expected_count`. A few
missing numbers per set is normal (secret rares not yet scanned); >10%
missing is a real gap that needs the operator to either bump
`expected_card_count` in `canonical_sets/zh_*.json` or chase the missing
images.

### 2.3 Cross-region alias coverage (`card_alias`)

```bash
docker compose exec pos psql -U vaultpos -d vaultpos -c "
SELECT match_method, source, COUNT(*)
  FROM card_alias
 GROUP BY match_method, source
 ORDER BY 3 DESC;"
```

**Expected**:

| match_method | source | count |
|---|---|---|
| `set_abbrev` | `auto` | thousands (the bulk of routine matches) |
| `clip` | `auto` | hundreds (where set abbrev was ambiguous and CLIP picked a winner) |
| `manual` | `manual_overrides` | however many lines you put in `/mnt/cards/manual_aliases.json` |
| `unmatched` | `auto` | a small tail |

**Red flag**: `manual` count = 0 but you know `manual_aliases.json` has
rows in it → the worker rejected the file. Check 2.4.

### 2.4 cross_region_aliaser run history

```bash
docker compose exec pos psql -U vaultpos -d vaultpos -c "
SELECT run_id, started_at, ended_at, items_ok, items_failed, notes
  FROM bg_worker_run
 WHERE worker_type = 'cross_region_aliaser'
 ORDER BY run_id DESC
 LIMIT 5;"
```

**Expected**: most-recent run has `ended_at IS NOT NULL`, `items_failed = 0`,
and an empty `notes`. A populated `notes` means the run completed but
flagged something for the operator to look at.

If you see no rows at all, the worker has never run — check
`docker compose logs pos | grep cross_region_aliaser`.

### 2.5 Manual-override validation status

This is the FU-1 fail-closed contract — if the operator-curated
`/mnt/cards/manual_aliases.json` has a typo, the whole worker run aborts
permanently and the error lands in `bg_task_queue.last_error`.

```bash
docker compose exec pos psql -U vaultpos -d vaultpos -c "
SELECT task_id, task_key, status, attempts, last_error
  FROM bg_task_queue
 WHERE task_type = 'cross_region_aliaser'
   AND status   = 'FAILED'
 ORDER BY task_id DESC
 LIMIT 3;"
```

**Expected — happy path**: zero rows. The worker hasn't permanent-failed.

**Expected — unhappy path with a malformed `manual_aliases.json`**:

```text
manual_aliases.json validation failed (3 errors):
  - override #2: zh_tc_id 'zh-tc:ptcg.tw:NOPE:001' references unknown set 'NOPE'
  - override #5: unknown key 'zh_tcc_id' (typo of 'zh_tc_id'?)
  - override #7: duplicate canonical_key 'jp:SV1S:001' (also at #3)
```

Fix the file, then re-queue with:

```bash
docker compose exec pos psql -U vaultpos -d vaultpos -c "
UPDATE bg_task_queue
   SET status = 'PENDING', attempts = 0, last_error = ''
 WHERE task_type = 'cross_region_aliaser' AND status = 'FAILED';"
```

The next worker tick (≤30s) picks it up. Re-run 2.4 to confirm `items_ok`
went up and `items_failed` stayed at 0.

---

## Phase 3 — Cross-pipeline sanity (`cards_master`)

`cards_master` is the deduplicated multi-language lookup table that the
POS UI queries when a tablet asks "what is this card?". Any `card_alias`
row should be reflected here within one nightly merge cycle.

```bash
docker compose exec pos psql -U vaultpos -d vaultpos -c "
SELECT
  COUNT(*)                                                           AS total,
  COUNT(*) FILTER (WHERE name_kr  != '')                             AS with_kr,
  COUNT(*) FILTER (WHERE name_chs != '' OR name_cht != '')           AS with_zh,
  COUNT(*) FILTER (WHERE name_kr  != '' AND (name_chs != '' OR name_cht != '')) AS with_kr_and_zh
  FROM cards_master;"
```

**Expected**: `with_kr_and_zh` should be in the thousands once both
pipelines are live. If `with_kr` is healthy but `with_kr_and_zh` is 0,
the `card_alias` join into `cards_master` hasn't happened — that's the
unified-DB merge worker, not the per-language importers.

---

## Phase 4 — English (`cards_master.name_en` + `en_set_gap`) 🇬🇧

EN is the simplest pipeline at the booth — almost no Korean shoppers ask
for English cards by name, but the EN spine is what `card_alias` reasons
against when matching unknown CHS/TC scans, so a broken EN pipeline shows
up as a *cross-language* alias miss, not a missing English card. Verify
in this order so you can tell "no EN data" apart from "EN data, but the
audit hasn't run".

### 4.1 Importer health

EN doesn't have its own importer — `cards_master.name_en` is populated as
a side effect of the TCGdex multi-language source landing in
`src_tcgdex_multi`. The status endpoint reports both numbers so you can
tell which side is short.

```bash
curl -s "http://${PI:-localhost:8080}/admin/en-cards/status" | jq .
```

**Expected**:

```json
{
  "count": 18000,            // cards_master rows with name_en non-empty
  "tcgdex_en_count": 18250,  // src_tcgdex_multi rows that ship an `en` name
  "gap": {
    "expected_total": 18250,
    "actual_total":   18000,
    "missing_total":  250,
    "extra_total":    0,
    "sets_audited":   240,
    "last_audited_at": 1714492802
  },
  "sample": [
    {"name": "Charizard ex", "set": "sv3pt5", "number": "199", "rarity": "Special Illustration Rare"},
    ...                      // five rows, most-recently-built first
  ]
}
```

**Red flags**:

| Symptom | Likely cause | Fix |
|---|---|---|
| `count: 0` and `tcgdex_en_count: 0` | TCGdex source never landed | Re-run the TCGdex pull, then `python3 pi-setup/build_cards_master.py` |
| `count: 0` but `tcgdex_en_count > 0` | `cards_master` build hasn't picked up the EN names | Re-run `python3 pi-setup/build_cards_master.py` |
| `gap.last_audited_at: 0` | Audit worker has never run | See 4.2 |
| `gap.missing_total > 10%` of `expected_total` | Real coverage gap | Inspect the gap rows in 4.3 |
| `gap.extra_total > 0` | `cards_master` has rows TCGdex doesn't list — usually a stale alias | Check `card_alias` for that set_id; an extra is a strong signal that an old override is pointing at a deleted upstream row |

### 4.2 en_set_audit run history

```bash
docker compose exec pos psql -U vaultpos -d vaultpos -c "
SELECT run_id, started_at, ended_at, items_ok, items_failed, notes
  FROM bg_worker_run
 WHERE worker_type = 'en_set_audit'
 ORDER BY run_id DESC
 LIMIT 5;"
```

**Expected**: most-recent run has `ended_at IS NOT NULL`, `items_failed = 0`,
and a non-zero `items_ok`. An `EMPTY_SOURCE` run (zero EN rows in
`src_tcgdex_multi`) lands here as `items_ok = 0`, `items_failed = 0` —
it's a legitimate sentinel meaning "the importer hasn't filled
`src_tcgdex_multi` yet", not a worker failure. Confirm with
`/admin/en-cards/status` → `tcgdex_en_count: 0`.

If the latest row is older than 24h, the daily seeder hasn't enqueued a
fresh task; the worker's CLI seed is `python3 -m workers.run en_set_audit
--seed`.

### 4.3 EN set-gap inspection

```bash
docker compose exec pos psql -U vaultpos -d vaultpos -c "
SELECT set_id, expected_count, actual_count,
       jsonb_array_length(missing_numbers) AS missing,
       jsonb_array_length(extra_numbers)   AS extra
  FROM en_set_gap
 ORDER BY (expected_count - actual_count) DESC, set_id
 LIMIT 20;"
```

**Expected**: most rows have `actual_count = expected_count`. The few
worst offenders at the top tell you which sets the EN side is short on —
usually a recent set release where TCGdex has shipped the metadata but
upstream image scrapers haven't caught up.

**Red flag**: an `extra > 0` for any set means there are EN-named rows in
`cards_master` that the EN spine doesn't acknowledge — almost always a
stale `card_alias` pointing at a deleted upstream card. Don't truncate;
re-run the aliaser (it's idempotent) and check 2.3 again.

### 4.4 Quick cross-check: EN/JP overlap

EN and JP both come from `src_tcgdex_multi`, so a healthy pipeline has
substantial overlap once both names land in `cards_master`.

```bash
docker compose exec pos psql -U vaultpos -d vaultpos -c "
SELECT
  COUNT(*) FILTER (WHERE name_en != '')                      AS with_en,
  COUNT(*) FILTER (WHERE name_jp != '')                      AS with_jp,
  COUNT(*) FILTER (WHERE name_en != '' AND name_jp != '')    AS with_en_and_jp
  FROM cards_master;"
```

**Expected**: `with_en_and_jp` should be ≥ 80% of `min(with_en, with_jp)`.
A much lower overlap usually means the TCGdex pull only landed one of
the two name columns — re-run the pull with both languages enabled.

---

## When something doesn't look right

1. **Don't truncate any of the underlying tables.** The `cross_region_aliaser`
   worker is idempotent; re-running it is always cheaper than rebuilding
   from scratch.
2. **Look at `bg_worker_run.notes` first.** That's where the workers leave
   "non-fatal but you should know" messages.
3. **`bg_task_queue.last_error` is for fatal aborts.** That's where FU-1
   manual-override validation errors land — see 2.5 for the recovery dance.
4. **The admin portal stays off-limits.** All recovery in this checklist
   is via `psql` and the read-only `/admin/{kr,chs,en}-cards/status` +
   `/admin/pipelines/status` endpoints; no admin-UI mutation needed.

---

## Related docs

- `pi-setup/docs/ZH_SOURCES.md` — where the ZH catalogs come from
- `pi-setup/docs/UNIFIED_DB_PLAN.md` — how the language-specific tables
  funnel into `cards_master`
- `pi-setup/scripts/canonical_sets/zh_{tc,sc}.json` — the canonical
  set lists used by `_validate_manual_overrides()`
- `pi-setup/workers/cross_region_aliaser.py` — the worker driving 2.3-2.5
- `pi-setup/workers/en_set_audit.py` — the worker driving 4.2-4.3
- `pi-setup/server.py` — `/admin/pipelines/status` aggregator behind Phase 0
