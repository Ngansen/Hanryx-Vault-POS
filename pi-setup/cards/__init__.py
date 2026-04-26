"""
pi-setup.cards — offline card database extension package.

New modules added in the USB-offline-DB rollout (Stages 1-5) live here to
keep them out of the 26k-line server.py monolith. Existing card-related
modules (tcg_lookup, import_*_cards, price_*) stay at the top of pi-setup/
to avoid a sweeping rename in this change set.

Modules:
    fuzzy_search   — multilingual rapidfuzz search across cards_kr / cards_jpn /
                     cards_chs / cards_jpn_pocket / inventory.
    ai_assistant   — Flask blueprint for the local Ollama + Qwen 2.5 cashier
                     assistant. Tools-via-prompt; queries SQLite read-only.
"""
