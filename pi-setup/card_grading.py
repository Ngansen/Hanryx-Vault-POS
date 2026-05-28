"""
card_grading.py — Visual card condition grading via GPT-4o Vision.

Grades a raw (unslabbed) Pokémon TCG card from a photograph.
When a local NM reference image is available (from /mnt/cards/HanryxVault)
it is sent alongside the trade-in photo so GPT can compare directly,
which measurably improves grade accuracy.

Grade scale and default offer rates (% of market price):
  NM  Near Mint          — 70 %
  LP  Lightly Played     — 52 %
  MP  Moderately Played  — 35 %
  HP  Heavily Played     — 20 %
  DMG Damaged            —  8 %
"""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Optional

import requests as _req

log = logging.getLogger(__name__)

_OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")

GRADES: list[str] = ["NM", "LP", "MP", "HP", "DMG"]

# What the shop OFFERS as a fraction of market price, per raw-card condition.
# These intentionally undercut typical resale margins so the shop stays
# profitable after reconditioning / handling costs.
CONDITION_OFFER_RATE: dict[str, float] = {
    "NM":  0.70,
    "LP":  0.52,
    "MP":  0.35,
    "HP":  0.20,
    "DMG": 0.08,
}

_SYSTEM = (
    "You are a professional Pokémon TCG card grader with 10+ years of experience. "
    "You use the industry-standard raw-card scale:\n"
    "  NM  (Near Mint)         — perfect or near-perfect; corners sharp; surface pristine.\n"
    "  LP  (Lightly Played)    — minor whitening on ≤2 corners; very slight surface scuffs; no creases.\n"
    "  MP  (Moderately Played) — noticeable corner whitening; visible scuffs; no major creases.\n"
    "  HP  (Heavily Played)    — heavy corner wear, multiple creases, significant surface damage.\n"
    "  DMG (Damaged)           — tears, holes, extreme creases, water damage, or writing on card.\n"
    "Examine both front and back when visible. Be conservative — when in doubt grade lower."
)

_PROMPT_NO_REF = (
    "Grade the condition of the Pokémon card in this photo. "
    "Inspect corners, surface, edges, and centering.\n"
    "Reply with ONLY a JSON object — no prose, no markdown fences:\n"
    '{"grade":"NM","confidence":0.85,"notes":"<one sentence>","defects":["list","of","defects"]}\n'
    "grade must be one of: NM, LP, MP, HP, DMG."
)

_PROMPT_WITH_REF = (
    "The FIRST image is a reference scan of this card in NM (Near Mint) condition.\n"
    "The SECOND image is the customer's copy being traded in.\n"
    "Compare them carefully and grade the customer's copy.\n"
    "Reply with ONLY a JSON object — no prose, no markdown fences:\n"
    '{"grade":"NM","confidence":0.85,"notes":"<one sentence>","defects":["list","of","defects"]}\n'
    "grade must be one of: NM, LP, MP, HP, DMG."
)


def _mime(path: str) -> str:
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp"}.get(
        Path(path).suffix.lower(), "image/jpeg"
    )


def _b64_file(path: str) -> str:
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()


def grade_card(
    img_b64: str,
    reference_path: Optional[str] = None,
    openai_key: Optional[str] = None,
) -> dict:
    """
    Grade a card from a base64 image.

    Parameters
    ----------
    img_b64 : str
        Base64-encoded JPEG/PNG of the card to grade.
        A ``data:image/...;base64,`` URI prefix is stripped automatically.
    reference_path : str, optional
        Filesystem path to a local NM reference image for comparison.
        When supplied the reference is sent as a second image so GPT can
        compare side-by-side, improving grade accuracy.
    openai_key : str, optional
        Override for ``OPENAI_API_KEY`` env var.

    Returns
    -------
    dict
        ``grade``, ``confidence``, ``notes``, ``defects``, ``offer_rate``
        — or ``{"error": "..."}`` on failure.
    """
    key = openai_key or _OPENAI_API_KEY
    if not key:
        return {"error": "OPENAI_API_KEY not configured"}

    if "," in img_b64:
        img_b64 = img_b64.split(",", 1)[1]

    use_ref = bool(reference_path and os.path.isfile(reference_path))

    if use_ref:
        ref_b64  = _b64_file(reference_path)  # type: ignore[arg-type]
        ref_mime = _mime(reference_path)       # type: ignore[arg-type]
        content: list[dict] = [
            {"type": "text", "text": _PROMPT_WITH_REF},
            {"type": "image_url", "image_url": {
                "url": f"data:{ref_mime};base64,{ref_b64}", "detail": "low",
            }},
            {"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,{img_b64}", "detail": "high",
            }},
        ]
    else:
        content = [
            {"type": "text", "text": _PROMPT_NO_REF},
            {"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,{img_b64}", "detail": "high",
            }},
        ]

    try:
        resp = _req.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o",
                "max_tokens": 300,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user",   "content": content},
                ],
            },
            timeout=35,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        log.exception("[card_grading] GPT-4o Vision call failed")
        return {"error": str(exc)}

    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("[card_grading] non-JSON response: %s", raw[:300])
        return {"error": "GPT response not valid JSON", "raw": raw}

    grade = str(result.get("grade", "NM")).upper().strip()
    if grade not in GRADES:
        grade = "NM"

    return {
        "grade":      grade,
        "confidence": float(result.get("confidence", 0.7)),
        "notes":      str(result.get("notes", "")),
        "defects":    result.get("defects") or [],
        "offer_rate": CONDITION_OFFER_RATE[grade],
    }
