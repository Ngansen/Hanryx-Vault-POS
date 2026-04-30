# Chinese-Language Card Sources

This document is the source survey for the Traditional Chinese (繁體, TC) and
Simplified Chinese (简体, SC) Pokémon TCG pipelines. It feeds the
`zh_sources.py` registry (slice ZH-2) and the canonical set lists under
`pi-setup/scripts/canonical_sets/zh_{tc,sc}.json`.

**Architectural principle: local-mirror-first.** Where a Ngansen GitHub fork
already mirrors a region's data, Phase D walks the local clone (kept fresh
by Phase A `git pull`). Web scraping is the FALLBACK, not the primary path.
This keeps the "continuously build out + keep pace with current sets" goal
zero-network at the booth.

## Market context

| Aspect | Traditional Chinese (TC) | Simplified Chinese (SC) |
|---|---|---|
| Region | Taiwan, Hong Kong, Macau | Mainland China |
| Official launch | 2023 (Sword & Shield High Class onwards) | 2024 (Tencent partnership) |
| Sets in print (apx, 2026-04) | ~20–25 | ~15+ (192 entries in CHS metadata) |
| Local Ngansen fork | **NONE** (gap — needs web scrape) | **YES** — `Ngansen/PTCG-CHS-Datasets` (5.6 GB) |
| Update mechanism | ptcg.tw scrape on cron | `git pull` in Phase A |

## Source inventory

### SC-LOCAL: `Ngansen/PTCG-CHS-Datasets` — primary SC source

Already cloned by Phase A into `$MIRROR_ROOT/PTCG-CHS-Datasets/`.

- **Layout**: `img/<set_id>/<card_num>.png` (no zero padding on card_num)
- **Set count**: 192 collections (per `ptcg_chs_infos.json` `collections[]`)
- **Card count**: ~50,000 images, 5.6 GB on disk
- **Self-describing metadata**: `ptcg_chs_infos.json` (21 MB) contains:
  - `dict.{regulation_mark, pokemon_type, trainer_type, card_type,
     energy_type, special_card, ability_cost, evolve, regulation,
     resistance_type, series, attribute, weakness_type, rarity,
     special_trainer}` — code/value lookup tables
  - `collections[]` — every set with `{id, name, commodityCode, salesDate,
     cards: [...]}` — this IS the canonical SC set list, no hand-curation
     needed
- **Update**: weekly Phase A `git pull` picks up new sets the upstream
  curator publishes
- **License**: dataset README — verify before redistribution; offline kiosk
  use is fine

**Phase D action**: read `ptcg_chs_infos.json`, iterate `collections[]`,
hardlink (or copy across-FS) each `img/<id>/<num>.png` to
`/mnt/cards/zh/sc/PTCG-CHS-Datasets/<id>/<num>.png`. Hardlinks save ~5.6 GB
of duplication when source + destination are on the same filesystem.

### TC-REMOTE: `ptcg.tw` — primary TC source

No local Ngansen TC fork exists. We scrape the official Taiwan site.

- **Base URL**: `https://www.ptcg.tw/`
- **Set index**: `https://www.ptcg.tw/expansions`
- **Image pattern**: `https://www.ptcg.tw/static/cards/<set-slug>/<num>.jpg`
  (VERIFY current — site occasionally redesigns)
- **Robots.txt**: VERIFY each Phase D run; last check allowed `/static/`
- **Rate limit**: 1 req/sec (conservative; site is small)
- **Coverage**: 100% of post-2023 official TC sets
- **Pre-2023 gap**: TC fans imported HK/Macau sets in the late-2010s era;
  these are not on ptcg.tw. Audit (ZH-4) surfaces them; MyCardArt is the
  optional fallback (TC-FALLBACK below).

### TC-FALLBACK: MyCardArt — opt-in only

- **Base URL**: `https://mycardart.com/` (VERIFY current domain)
- **Why fallback only**: license unclear (community uploads, mixed
  copyrights). Not bulk-walked. Triggered ONLY when zh_set_audit (ZH-4)
  reports a missing card on a pre-2023 TC set.
- **Operator confirmed**: yes (per session notes — "yes all").

### Sources rejected from default Phase D

- **Tencent `ptcg.qq.com`**: REJECTED. The `Ngansen/PTCG-CHS-Datasets`
  fork is upstream of (or as good as) anything on Tencent for our use,
  with no anti-scrape risk and no ToS exposure. Listed here so future
  maintainers don't re-add it without reason.
- **`tcg.fans` / Pokellector TC subset**: low coverage (~20–60%), not
  worth maintaining a third TC source.
- **`Ngansen/Card-Database`**: XLSX spreadsheets (not structured); useful
  as a cross-reference for set names but not as an image source.

## Phase D walking strategy

```
python -m scripts.sync_card_mirror --phase D \
    [--include-zh-tc] [--include-zh-sc] [--zh-fallback-sources]
```

Default: TC + SC primary sources. Fallback sources (MyCardArt) opt-in.

For each enabled source:

1. **LocalMirrorSource** (SC):
   - Verify `$MIRROR_ROOT/<repo_dir>/.git` exists (Phase A ran)
   - Iterate sets per `canonical_sets/zh_sc.json` (auto-refreshed from
     `ptcg_chs_infos.json` as a derived artifact)
   - For each `(set_id, card_num)`: `os.link()` source→dest, falling back
     to copy across filesystems
2. **RemoteWebSource** (TC):
   - Iterate sets per `canonical_sets/zh_tc.json` (hand-curated)
   - Use `_download()` with per-source UA + rate-limit
   - Honor `If-Modified-Since` (free 304s on re-runs)
3. Record outcome via `mirror_failure_log.record_mirror_outcome()`.

Phase D is idempotent: re-running on a fully-mirrored tree does the minimum
network/FS work (304s for remote, link-already-exists for local).

## Cross-region linking (feeds slice ZH-3)

Canonical key = JP `(set_id, card_num)` — JP is the most complete catalog
and predates all other regions.

Match priority for each new ZH card:

1. **Set abbreviation match** (e.g., TC "SV1S" → JP "SV1S") + card number
2. **Card number** within set + visual CLIP similarity ≥ 0.92 vs JP image
3. **Manual override** at `/mnt/cards/manual_aliases.json` (always wins)

For SC the `commodityCode` field in `ptcg_chs_infos.json` (e.g.,
`"30th-P-01"`) is the abbreviation candidate — see ZH-3 for the mapping
heuristic.

## TL;DR

- **SC**: walk local `PTCG-CHS-Datasets` (already on Pi via Phase A).
  No web scraping. Self-describing via `ptcg_chs_infos.json`.
- **TC**: scrape `ptcg.tw` on the same weekly cron (no local fork exists).
  MyCardArt is opt-in fallback for pre-2023 sets.
- **Tencent**: NOT used. The local SC fork makes it unnecessary.
- Cross-region key = JP `(set_id, card_num)`; CLIP fingerprint ≥ 0.92 is
  the tiebreaker.
- All four operator confirmations from the previous draft answered "yes
  all" — no blockers remain.
