# Chinese-Language Card Sources

This document is the source survey for the Traditional Chinese (繁體, TC) and
Simplified Chinese (简体, SC) Pokémon TCG pipelines. It feeds the
`zh_sources.py` registry (built in slice ZH-2) and the canonical set lists
under `pi-setup/scripts/canonical_sets/zh_{tc,sc}.json`.

The TL;DR is at the bottom. The middle is a per-source dossier so future
maintainers can re-evaluate without redoing the research from scratch.

## Market context

The TC and SC markets have very different shapes and you should not treat
them symmetrically:

| Aspect | Traditional Chinese (TC) | Simplified Chinese (SC) |
|---|---|---|
| Region | Taiwan, Hong Kong, Macau | Mainland China |
| Official launch | 2023 (Sword & Shield High Class onwards) | 2024 (Tencent partnership) |
| Pre-official era | JP imports + community translations | None — SC is brand-new |
| Sets in print (apx, 2026-04) | ~20–25 | ~5–10 |
| Open data ecosystem | Mature (multiple community DBs) | Sparse (Tencent-only) |
| Anti-scraping posture | Generally permissive | Heavy (Tencent DRM + bot detection) |

Implication: TC has multiple cross-checkable sources; SC has effectively
one (Tencent) and we must scrape it politely.

## Source comparison matrix

| Source | Lang | Kind | License posture | Coverage | Image quality | Update freq | Default include? |
|---|---|---|---|---|---|---|---|
| `ptcg.tw` | TC | Official | Tolerated for personal use | 100% post-2023 sets, 0% pre-2023 | High (official scans) | Quarterly w/ release | **YES** |
| MyCardArt | TC | Community | Mixed; no commercial-use guarantee | ~70% incl. pre-2023 | Variable | Monthly | **YES** (gap-filler) |
| `tcg.fans` | TC | Community | CC-BY-SA stated | ~60% | Medium | Sporadic | NO (low coverage) |
| Pokellector TC subset | TC | Community | Permissive scrape | ~20% TC | Medium | Sporadic | NO (low coverage) |
| `ptcg.qq.com` (Tencent) | SC | Official | Anti-scrape; private content terms | 100% SC | High (official scans) | Per-release | **YES** (only viable source) |
| Community SC mirrors | SC | Community | Unverified | <5% | Variable | Sporadic | NO (insufficient) |

## Per-source dossier

### TC-1: `ptcg.tw` — Pokémon Trading Card Game Taiwan (official)

- **Base URL**: `https://www.ptcg.tw/`
- **Set index**: `https://www.ptcg.tw/expansions` (single page, server-rendered)
- **Card listing pattern**: `https://www.ptcg.tw/expansions/<set-slug>/cards`
- **Card image pattern**: `https://www.ptcg.tw/static/cards/<set-slug>/<num>.jpg`
- **Robots.txt**: VERIFY — last checked posture allows `/static/` and
  `/expansions/`; recheck before each Phase D run.
- **Recommended walk strategy**: scrape set index quarterly, walk new sets
  card-by-card with 1 req/sec rate limit. Honor `Last-Modified` headers.
- **Risks**: site redesign breaks scraper. Cache the parsed set index in
  `/mnt/cards/zh/tc/_set_index.json` so a single redesign doesn't lose
  history.

### TC-2: MyCardArt

- **Base URL**: `https://mycardart.com/` (VERIFY current domain)
- **Why**: only realistic source for TC sets that predate the 2023 official
  launch (community-uploaded scans of Hong Kong / Macau distribution from
  the late-2010s era).
- **Coverage**: ~70% across all TC eras, but quality varies wildly. Use
  ONLY as a fallback when `ptcg.tw` returns 404 for a card.
- **License**: unclear — mixed sources, some uploads explicitly copyrighted.
  We mirror only the IMAGE for offline lookup at the booth (not for
  commercial redistribution).
- **Recommended walk strategy**: walk on-demand triggered by ZH-4 audit
  (zh_set_audit reports a missing card → enqueue MyCardArt fetch as
  fallback). Do NOT bulk-walk every release.

### SC-1: `ptcg.qq.com` — Tencent Pokémon TCG (official, sole source)

- **Base URL**: `https://ptcg.qq.com/` (VERIFY current — Tencent uses
  multiple subdomain partitions)
- **Why**: Tencent is the exclusive Pokémon Co partner for the SC market
  and the ONLY source that publishes complete, accurate SC set data.
- **Anti-scrape posture**: the site uses request signing,
  Cloudflare-style bot detection, and may serve different HTML to
  scrapers vs browsers. Plan for:
  - User-Agent rotation (browser-style, not python-urllib)
  - 1 req per 2 seconds maximum
  - Cookie jar (some endpoints require a session cookie from the landing
    page)
  - Backoff on 429/403 with at least 5-minute cooldown
- **Card image pattern**: VERIFY — Tencent obfuscates image URLs with a
  signed token query parameter that expires. We need to FETCH the image
  IMMEDIATELY when discovered (don't store the URL for later).
- **Legal**: check Tencent's ToS carefully before deployment. Personal /
  research use of card images is generally tolerated; bulk redistribution
  is not. Our use case (offline kiosk lookup) is closer to personal.
  **Operator confirmation required before this source ships.**

## Phase D walking strategy

Phase D will be invoked via:

```
python -m scripts.sync_card_mirror --phase D \
    [--include-zh-tc] [--include-zh-sc]
```

Default: include both. Operator can opt out per-language for testing or to
limit Tencent rate-limit exposure.

For each enabled source:

1. Fetch the set index. Cache locally at
   `/mnt/cards/zh/<lang>/_set_index.json` with mtime.
2. Diff against canonical_sets JSON. New sets logged for operator review
   (do NOT auto-add — canonical lists are hand-curated).
3. For each set in canonical_sets, walk the card listing.
4. For each card, download image atomically (tmp + fsync + rename) to
   `/mnt/cards/zh/<lang>/<source>/<set_id>/<card_num>.jpg`.
5. Record outcome via `mirror_failure_log.record_mirror_outcome()` so
   failures persist for triage.

## Cross-region linking (feeds slice ZH-3)

The canonical-key spine uses JP `(set_id, card_num)` as the primary key
because:

- JP is the most complete catalog (every release ships in JP first)
- JP set codes are stable and mostly mirrored by TC (e.g., TC "SV1S" ≡ JP "SV1S")
- KR, EN, and SC all derive from JP releases on a delay

Match priority for each new ZH card:

1. **Set abbreviation match** (e.g., TC "SV1S" → JP "SV1S") + card number
2. **Card number** within set + visual CLIP similarity ≥ 0.92 vs JP image
3. **Manual override** at `/mnt/cards/manual_aliases.json` (always wins)

Unmatched cards get logged to `bg_worker_run.notes` with enough context
for the operator to manually link them via override file.

## Open questions for operator confirmation

Before any of TC-2 / SC-1 ship, please confirm:

1. **MyCardArt commercial-use**: are we comfortable mirroring TC-2 images
   for offline kiosk use, given the unclear licensing? Recommend YES
   (kiosk lookup is fair-use-adjacent), but this is a business call.

2. **Tencent ToS posture**: explicit confirmation that polite scraping of
   `ptcg.qq.com` for offline kiosk use is acceptable to you. If NO, SC
   pipeline becomes manual import only.

3. **Pre-2023 TC priority**: is mirroring pre-official TC sets worth the
   ~30% expected effort overhead? If NO, we skip TC-2 entirely and rely
   on `ptcg.tw` + manual gap-filling for any pre-2023 cards a customer
   brings to the booth.

4. **Update cadence**: I propose `zh_full_sync.sh` runs on the same
   weekly cron as KR refresh. Confirm or specify alternative.

## TL;DR

- Default Phase D walks `ptcg.tw` (TC) + `ptcg.qq.com` (SC).
- MyCardArt is a fallback-only gap-filler triggered by zh_set_audit.
- Tencent needs polite scraping (1 req / 2 sec, UA rotation, session cookies).
- Cross-region key = JP `(set_id, card_num)`; CLIP fingerprint ≥ 0.92 is
  the tiebreaker when set codes don't match.
- Three operator confirmations required before TC-2 + SC-1 ship.
