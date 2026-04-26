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
Reply in 1-2 SHORT sentences (max 250 chars). Format any prices with both KRW and USD.
If the result list is empty, say so plainly and suggest one refinement.
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
    name = (intent.get("name") or "").strip()
    grade = (intent.get("grade") or "raw").lower()
    if not name:
        return {"price": None, "note": "no name in query"}
    conn = sqlite_connect(local_db_path())
    try:
        # Cross-reference inventory_snapshot to find the cards we actually
        # stock, then look up most-recent price from price_history_recent.
        rows = conn.execute(
            """
            SELECT i.qr_code, i.name, i.set_name, i.grade, i.condition,
                   i.price, i.sale_price,
                   p.price AS observed_price, p.source AS observed_source,
                   p.observed_at
              FROM inventory_snapshot i
              LEFT JOIN price_history_recent p ON p.card_id = i.qr_code
             WHERE LOWER(i.name) LIKE ?
               AND (? = 'raw' OR LOWER(i.grade) = ?)
             ORDER BY p.observed_at DESC NULLS LAST
             LIMIT 5
            """,
            (f"%{name.lower()}%", grade, grade),
        ).fetchall()
    finally:
        conn.close()
    return {"matches": [dict(r) for r in rows]}


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
