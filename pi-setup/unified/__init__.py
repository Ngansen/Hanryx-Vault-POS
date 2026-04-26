"""
unified/ — Unified multi-source card database package.

This package adds a new layer on top of the existing per-language card
tables (cards, cards_kr, cards_jpn, cards_jpn_pocket, cards_chs). Those
tables stay exactly as they are — the unified layer pulls from them PLUS
the new src_*_xlsx / src_tcgdex_multi / src_pocket_limitless / etc.
tables built from your Card-Database Excel files and the additional
forks under Ngansen/*.

Three layers:

    Layer 1  src_*       Raw per-source tables (one per importer).
                         Never mutated by the consolidator.

    Layer 2  ref_*       Cross-language mappings (sets, variants,
                         promos, species). Tiny but conceptually
                         critical — these are the Rosetta Stone that
                         lets the consolidator match a Korean card
                         to its English equivalent.

    Layer 3  cards_master  Single denormalised view, one row per
                           (set_id, card_number, variant_code), with
                           every-language name + serial codes joined
                           in. This is what the POS searches.

See `pi-setup/docs/UNIFIED_DB_PLAN.md` for the full design and
`pi-setup/unified/schema.py` for the canonical CREATE TABLE statements.
"""
from .schema import init_unified_schema  # re-exported for convenience

__all__ = ["init_unified_schema"]
