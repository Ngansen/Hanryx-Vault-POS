"""
cards.ai_assistant — Stage 5: local AI cashier assistant blueprint.

Plumbing only — actual model inference happens in the `assistant`
container (Ollama serving Qwen 2.5 3B). This module is the Flask
adapter that:

  1. Receives natural-language queries via POST /ai/chat
  2. Asks the local model to translate the query into a constrained
     intent (search_card | lookup_price | inventory_count | unknown)
  3. Executes the corresponding read-only SQLite query against the
     USB-resident pokedex_local.db
  4. Asks the model to rephrase the structured result into a sentence

Why constrained-intent + SQL, not "let the model write SQL"
-----------------------------------------------------------
Letting a 3B model write arbitrary SQL against a production database is
how you end up running `DROP TABLE inventory;` because the model
hallucinated a "cleanup" step. The intent grammar is small enough that
we can hand-author the SQL for each intent and never let the model
near a SQL string.

Why cap responses at intent + 250 chars of summary
--------------------------------------------------
Qwen 2.5 3B on a Pi 5 CPU produces ~10 tokens/sec. A 200-word answer
is ~10 seconds of waiting. The cashier is mid-sale and won't wait. So:
the model gets a short input (intent + JSON of the SQL result rows)
and a tight system prompt that says "1-2 sentences max, currency in
KRW + USD". If the cashier wants more detail they can click through
to the full record from the search hit.

Network surface (kept minimal so the assistant container can be
firewalled to talk to Pi-only):
    POST /ai/chat        { "message": "show me psa10 charizards" }
    GET  /ai/health      → { "ollama": "ok"|"down", "model": "qwen2.5:3b" }
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3

import requests
from flask import Blueprint, jsonify, request

from cards_db_path import local_db_path
from pi_setup_compat import sqlite_connect

log = logging.getLogger("cards.ai_assistant")

OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://assistant:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT_SEC", "30"))

# Module-level requests.Session reuses the TCP connection to Ollama
# across requests (the cashier asking back-to-back questions doesn't
# pay a fresh TLS / TCP handshake each time). Ollama keeps the model
# resident via OLLAMA_KEEP_ALIVE so this is the dominant per-call cost.
_session = requests.Session()

ai_bp = Blueprint("ai", __name__, url_prefix="/ai")


# ── Intent grammar ─────────────────────────────────────────────────────────────
# We give the model exactly four intents to choose from. The first phase
# of the chat is constrained to JSON output; if the model returns
# anything else we fall back to "unknown" and ask it to clarify.

_INTENT_SYSTEM_PROMPT = """You are HanryxVault's card-shop cashier assistant.
Classify the user's question into ONE intent and return strict JSON only.

Intents:
- search_card    {"intent":"search_card","name":"<card name>","language":"en|ko|ja|zh|any"}
- lookup_price   {"intent":"lookup_price","name":"<card name>","grade":"raw|psa10|psa9|cgc10|bgs95"}
- inventory_count {"intent":"inventory_count","name":"<card name>"}
- unknown         {"intent":"unknown","reason":"<one sentence why you can't classify>"}

Rules:
- ALWAYS return ONE LINE of valid JSON. No code fences, no prose.
- Default language is "any" unless the user names a language explicitly.
- Default grade is "raw".
- If the user mentions a Hangul / Hanzi / Kanji name, KEEP IT in the name field.
"""

_SUMMARY_SYSTEM_PROMPT = """You are HanryxVault's cashier assistant.
You will receive a JSON object describing the cashier's question and the database result.
Reply in 1-2 SHORT sentences (max 300 chars).

Pricing format rules:
- For lookup_price results, list 1 line PER SOURCE we have data for, in the
  order: cardmarket, naver, bunjang, hareruya2, tcgplayer (skip any source
  with no data). Format: "<source>: <native_price><cur_symbol> (~$<usd>)".
  Currency symbols: KRW=₩, JPY=¥, EUR=€, USD=$.
- If `price_usd` is present, ALWAYS include the parenthesised USD figure so
  the cashier can compare across markets at a glance.
- If observed_at is older than 7 days, append " (stale)" to that line.
- If ALL sources are empty, say "no recent market data — try refreshing or
  check the card name spelling".
- For non-price intents, currency rules don't apply.
"""


def _ollama_chat(messages: list[dict], temperature: float = 0.0) -> str:
    """Call Ollama's /api/chat. Returns the assistant message content."""
    try:
        r = _session.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": messages,
                "stream": False,
                "options": {"temperature": temperature, "num_ctx": 2048},
            },
            timeout=OLLAMA_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("message", {}).get("content", "").strip()
    except requests.exceptions.RequestException as e:
        log.warning("[ai] ollama call failed: %s", e)
        return ""


# ── Intent execution (read-only SQL, hand-authored per intent) ────────────────


def _exec_search_card(intent: dict) -> dict:
    name = (intent.get("name") or "").strip()
    if not name:
        return {"hits": [], "note": "no name in query"}
    # Use the fuzzy_search module so the AI assistant gets the same
    # multilingual handling as the /tcg/search-multi endpoint.
    from cards.fuzzy_search import search as fuzzy_search
    lang = intent.get("language") or "any"
    languages = None if lang == "any" else [lang]
    hits = fuzzy_search(local_db_path(), name, limit=5, languages=languages)
    return {"hits": hits}


def _exec_lookup_price(intent: dict) -> dict:
    """C12: multi-source price lookup with USD conversion.

    Returns up to 5 matched inventory cards, each with a `markets` dict
    keyed by source containing the most-recent observation per source
    (native price + USD-converted price + observed_at + staleness flag).

    SQL strategy: window function ROW_NUMBER() OVER (PARTITION BY card,
    source ORDER BY observed_at DESC) collapses the rolling log of
    observations down to one row per (card, source). We then filter to
    rn=1 and pivot in Python (cheaper than a CTE per source). SQLite
    >=3.25 ships window funcs; Pi 5 has 3.40+, safe.

    Empty `markets` dict means no scraper has ever returned a hit for
    this card — surfaces in the LLM summary as "no recent market data".
    """
    name = (intent.get("name") or "").strip()
    grade = (intent.get("grade") or "raw").lower()
    if not name:
        return {"price": None, "note": "no name in query"}
    conn = sqlite_connect(local_db_path())
    try:
        rows = conn.execute(
            """
            WITH matched AS (
              SELECT qr_code, name, set_name, grade, condition,
                     price AS our_price, sale_price AS our_sale
                FROM inventory_snapshot
               WHERE LOWER(name) LIKE ?
                 AND (? = 'raw' OR LOWER(grade) = ?)
               LIMIT 5
            ),
            ranked AS (
              SELECT m.qr_code, m.name, m.set_name, m.grade, m.condition,
                     m.our_price, m.our_sale,
                     p.source, p.price AS native_price,
                     p.currency, p.price_usd, p.observed_at,
                     ROW_NUMBER() OVER (
                       PARTITION BY m.qr_code, p.source
                       ORDER BY p.observed_at DESC
                     ) AS rn
                FROM matched m
                LEFT JOIN price_history_recent p ON p.card_id = m.qr_code
            )
            SELECT * FROM ranked
             WHERE rn = 1 OR source IS NULL
             ORDER BY qr_code, source
            """,
            (f"%{name.lower()}%", grade, grade),
        ).fetchall()
    finally:
        conn.close()

    # Pivot rows → one entry per matched card with a markets dict
    import time as _t
    now_epoch = int(_t.time())
    by_card: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        qr = d["qr_code"]
        if qr not in by_card:
            by_card[qr] = {
                "qr_code":    qr,
                "name":       d["name"],
                "set_name":   d["set_name"],
                "grade":      d["grade"],
                "condition":  d["condition"],
                "our_price":  d["our_price"],
                "our_sale":   d["our_sale"],
                "markets":    {},  # filled below
            }
        src = d.get("source")
        if not src:
            continue  # LEFT JOIN miss — card has no scraper coverage yet
        observed = d.get("observed_at") or 0
        age_days = max(0, (now_epoch - int(observed)) // 86400) if observed else None
        by_card[qr]["markets"][src] = {
            "native_price": d.get("native_price"),
            "currency":     d.get("currency") or "USD",
            "price_usd":    d.get("price_usd"),
            "observed_at":  observed,
            "age_days":     age_days,
            "stale":        bool(age_days is not None and age_days > 7),
        }

    return {"matches": list(by_card.values())}


def _exec_inventory_count(intent: dict) -> dict:
    name = (intent.get("name") or "").strip()
    if not name:
        return {"count": 0, "note": "no name in query"}
    conn = sqlite_connect(local_db_path())
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c, SUM(stock) AS s FROM inventory_snapshot WHERE LOWER(name) LIKE ?",
            (f"%{name.lower()}%",),
        ).fetchone()
    finally:
        conn.close()
    return {"distinct_skus": row["c"] or 0, "total_stock": row["s"] or 0}


_INTENT_HANDLERS = {
    "search_card":     _exec_search_card,
    "lookup_price":    _exec_lookup_price,
    "inventory_count": _exec_inventory_count,
}


def _classify_intent(user_message: str) -> dict:
    raw = _ollama_chat([
        {"role": "system", "content": _INTENT_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ])
    if not raw:
        return {"intent": "unknown", "reason": "model unreachable"}
    # Models sometimes wrap JSON in code fences despite instructions.
    # Strip them defensively before parsing.
    txt = raw.strip().strip("`").strip()
    if txt.startswith("json"):
        txt = txt[4:].strip()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        log.info("[ai] intent classifier returned non-JSON: %s", raw[:200])
        return {"intent": "unknown", "reason": "intent classifier returned non-JSON"}


def _summarise(user_message: str, intent: dict, result: dict) -> str:
    summary = _ollama_chat(
        [
            {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps({
                    "question": user_message,
                    "intent": intent,
                    "result": result,
                }, ensure_ascii=False),
            },
        ],
        temperature=0.2,
    )
    if summary:
        return summary[:1000]
    # Graceful fallback when the model is down or slow.
    if intent.get("intent") == "search_card":
        n = len(result.get("hits", []))
        return f"Found {n} card{'s' if n != 1 else ''} matching your search."
    if intent.get("intent") == "inventory_count":
        return f"Total stock: {result.get('total_stock', 0)} across {result.get('distinct_skus', 0)} SKUs."
    return "Result available — model unreachable for summary."


# ── Routes ─────────────────────────────────────────────────────────────────────


@ai_bp.route("/chat", methods=["POST"])
def chat():
    payload = request.get_json(silent=True) or {}
    msg = (payload.get("message") or "").strip()
    if not msg:
        return jsonify({"error": "Provide {message: <string>}"}), 400

    intent = _classify_intent(msg)
    handler = _INTENT_HANDLERS.get(intent.get("intent"))
    if not handler:
        return jsonify({
            "intent": intent,
            "result": None,
            "answer": (
                "I couldn't understand that. Try: "
                "'search charizard', 'price of pikachu psa 10', "
                "or 'how many lugias do we have'."
            ),
        })

    try:
        result = handler(intent)
    except sqlite3.OperationalError as e:
        return jsonify({
            "intent": intent,
            "result": None,
            "answer": f"Database not ready: {e}. The orchestrator may still be building the mirror.",
        }), 503

    return jsonify({
        "intent": intent,
        "result": result,
        "answer": _summarise(msg, intent, result),
    })


@ai_bp.route("/admin/db-coverage", methods=["GET"])
def db_coverage():
    """Per-set + per-language completeness report for cards_master.

    Note: ai_bp has url_prefix="/ai", so the full URL is
    `GET /ai/admin/db-coverage`, NOT `/admin/db-coverage`.

    The cashier doesn't see this — it's an operator dashboard endpoint.
    Three sections in the response:

      * `totals`         — overall row count, one number per language.
      * `per_set`        — for each set_id present in cards_master,
                            counts of how many rows have each language
                            populated. Lets the operator spot a set
                            where the Korean import missed a sheet.
      * `source_share`   — counts how often each Layer-1 source
                            "won" the priority race for each major
                            field. Useful for sanity-checking the
                            priority rules in unified/priority.py.

    The query is intentionally read-only and bounded to keep the
    endpoint cheap to poll from a Grafana / Uptime Kuma dashboard.
    Returns 503 if cards_master doesn't exist yet (i.e. the
    consolidator hasn't run).
    """
    db_path = local_db_path()
    try:
        conn = sqlite_connect(db_path)
    except Exception as e:
        return jsonify({"error": f"USB DB not available: {e}"}), 503

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='cards_master'"
        )
        if not cur.fetchone():
            return jsonify({
                "error": ("cards_master not present on this USB mirror — "
                          "run build_cards_master.py and then re-mirror."),
            }), 503

        # Totals across the whole table
        cur.execute("""
            SELECT
              COUNT(*)                                                 AS total,
              SUM(CASE WHEN COALESCE(name_en,  '') <> '' THEN 1 ELSE 0 END)  AS with_en,
              SUM(CASE WHEN COALESCE(name_kr,  '') <> '' THEN 1 ELSE 0 END)  AS with_kr,
              SUM(CASE WHEN COALESCE(name_jp,  '') <> '' THEN 1 ELSE 0 END)  AS with_jp,
              SUM(CASE WHEN COALESCE(name_chs, '') <> '' THEN 1 ELSE 0 END)  AS with_chs,
              SUM(CASE WHEN COALESCE(name_cht, '') <> '' THEN 1 ELSE 0 END)  AS with_cht,
              SUM(CASE WHEN COALESCE(ex_serial_codes, '[]') <> '[]'
                                                       THEN 1 ELSE 0 END)  AS with_codes,
              SUM(CASE WHEN COALESCE(promo_source, '') <> ''
                                                       THEN 1 ELSE 0 END)  AS with_promo
              FROM cards_master
        """)
        totals_row = cur.fetchone()
        totals = {k: totals_row[k] for k in totals_row.keys()}

        # Per-set breakdown — limited to top 50 by row count to keep
        # the JSON payload reasonable for a dashboard widget.
        cur.execute("""
            SELECT set_id,
                   COUNT(*) AS total,
                   SUM(CASE WHEN COALESCE(name_en,  '') <> '' THEN 1 ELSE 0 END) AS with_en,
                   SUM(CASE WHEN COALESCE(name_kr,  '') <> '' THEN 1 ELSE 0 END) AS with_kr,
                   SUM(CASE WHEN COALESCE(name_jp,  '') <> '' THEN 1 ELSE 0 END) AS with_jp,
                   SUM(CASE WHEN COALESCE(name_chs, '') <> '' THEN 1 ELSE 0 END) AS with_chs
              FROM cards_master
             GROUP BY set_id
             ORDER BY total DESC
             LIMIT 50
        """)
        per_set = []
        for r in cur.fetchall():
            d = {k: r[k] for k in r.keys()}
            t = max(int(d["total"]) or 1, 1)
            d["pct_en"]  = round(100 * int(d["with_en"])  / t, 1)
            d["pct_kr"]  = round(100 * int(d["with_kr"])  / t, 1)
            d["pct_jp"]  = round(100 * int(d["with_jp"])  / t, 1)
            d["pct_chs"] = round(100 * int(d["with_chs"]) / t, 1)
            per_set.append(d)

        # Source-share — parse the source_refs JSON to see which
        # Layer-1 source contributed each field. This is a best-effort
        # scan over a sample because cards_master can be 70k rows and
        # JSON-parsing every one in Python is slow. We sample 5,000.
        cur.execute(
            "SELECT source_refs FROM cards_master "
            "WHERE source_refs <> '{}' LIMIT 5000"
        )
        share: dict[str, dict[str, int]] = {}
        for r in cur.fetchall():
            try:
                refs = json.loads(r[0]) if r[0] else {}
            except (TypeError, ValueError):
                continue
            for field, ref in refs.items():
                if not isinstance(ref, str):
                    continue
                src_name = ref.split(":", 1)[0] if ":" in ref else ref
                share.setdefault(field, {})
                share[field][src_name] = share[field].get(src_name, 0) + 1

        return jsonify({
            "ok": True,
            "totals": totals,
            "per_set_top50": per_set,
            "source_share_sample": share,
            "sample_size": 5000,
        })
    finally:
        conn.close()


@ai_bp.route("/health")
def health():
    """Liveness — does the assistant container respond and is the model loaded?"""
    try:
        r = _session.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        r.raise_for_status()
        tags = r.json().get("models", [])
        loaded = [m.get("name") for m in tags]
        model_present = any(OLLAMA_MODEL in (n or "") for n in loaded)
        return jsonify({
            "ollama": "ok",
            "model_target": OLLAMA_MODEL,
            "model_loaded": model_present,
            "all_models": loaded,
        })
    except requests.exceptions.RequestException as e:
        return jsonify({"ollama": "down", "error": str(e)}), 503
