"""
psa_cert.py — PSA cert authentication via the PSA Public API.

Endpoint:  GET https://api.psacard.com/publicapi/cert/GetByCertNumber/{cert}
Auth:      Bearer token in `PSA_API_TOKEN` env var (free, request from
           https://www.psacard.com/publicapi).

Cert records are effectively immutable once a card is graded, so we cache
aggressively (30 days). Negative lookups (cert not found) are cached for
1 hour so a typo doesn't keep hammering the API.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import requests

log = logging.getLogger("hanryx.psa_cert")

_PSA_URL = "https://api.psacard.com/publicapi/cert/GetByCertNumber/{cert}"

_POS_TTL_S = 30 * 86_400   # 30 days for confirmed certs
_NEG_TTL_S = 3_600         # 1 hour for not-found

_lru: dict[str, tuple[float, object]] = {}
_LRU_MAX = 4096


def _cache_get(redis_client, key: str):
    if redis_client is not None:
        try:
            raw = redis_client.get(key)
            if raw:
                return json.loads(raw)
        except Exception as e:
            log.debug("[psa] redis get failed: %s", e)
    item = _lru.get(key)
    if not item:
        return None
    expires, value = item
    if time.time() > expires:
        _lru.pop(key, None)
        return None
    return value


def _cache_set(redis_client, key: str, value, ttl: int):
    if redis_client is not None:
        try:
            redis_client.setex(key, ttl, json.dumps(value))
            return
        except Exception as e:
            log.debug("[psa] redis set failed: %s", e)
    if len(_lru) >= _LRU_MAX:
        for k in list(_lru.keys())[: _LRU_MAX // 10]:
            _lru.pop(k, None)
    _lru[key] = (time.time() + ttl, value)


def lookup_cert(cert_number: str, redis_client=None) -> Optional[dict]:
    """
    Look up a PSA cert by number.

    Returns a normalised dict:
        {
          "cert_number":  "12345678",
          "grade":        "10",
          "grade_label":  "GEM MT 10",
          "year":         "2024",
          "brand":        "POKEMON SV151",
          "subject":      "Charizard ex",
          "card_number":  "199",
          "variety":      "FULL ART",
          "category":     "TCG Cards",
          "population":   1234,
          "pop_higher":   0,
          "is_dual_cert": False,
          "source":       "psa",
        }
    or None when the cert isn't found.

    Returns dict with key 'error' when:
      • PSA_API_TOKEN not configured            → {"error": "no_token"}
      • PSA API call fails (network/5xx)        → {"error": "upstream"}
      • Cert exists but response shape unknown  → {"error": "parse"}
    """
    cert = (cert_number or "").strip()
    if not cert.isdigit():
        return {"error": "bad_cert_format"}

    token = os.environ.get("PSA_API_TOKEN", "").strip()
    if not token:
        return {"error": "no_token"}

    cache_key = f"hv:psa:cert:{cert}"
    cached = _cache_get(redis_client, cache_key)
    if cached is not None:
        return cached or None  # negative cache stored as {}

    try:
        r = requests.get(
            _PSA_URL.format(cert=cert),
            headers={
                "Authorization": f"bearer {token}",
                "Accept":        "application/json",
            },
            timeout=10,
        )
        if r.status_code == 404:
            _cache_set(redis_client, cache_key, {}, _NEG_TTL_S)
            return None
        if r.status_code == 401:
            return {"error": "bad_token"}
        if r.status_code == 429:
            return {"error": "rate_limited"}
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        log.warning("[psa] cert %s lookup failed: %s", cert, e)
        return {"error": "upstream"}

    raw = body.get("PSACert") or body.get("psaCert") or body
    if not isinstance(raw, dict) or not raw.get("CertNumber"):
        log.warning("[psa] unexpected response shape for cert %s: %r", cert, body)
        return {"error": "parse"}

    result = {
        "cert_number":  str(raw.get("CertNumber") or cert),
        "grade":        str(raw.get("CardGrade")        or "").strip() or None,
        "grade_label":  str(raw.get("GradeDescription") or "").strip() or None,
        "year":         str(raw.get("Year")             or "").strip() or None,
        "brand":        str(raw.get("Brand")            or "").strip() or None,
        "subject":      str(raw.get("Subject")          or "").strip() or None,
        "card_number":  str(raw.get("CardNumber")       or "").strip() or None,
        "variety":      str(raw.get("Variety")          or "").strip() or None,
        "category":     str(raw.get("Category")         or "").strip() or None,
        "population":   raw.get("TotalPopulation"),
        "pop_higher":   raw.get("PopulationHigher"),
        "is_dual_cert": bool(raw.get("IsDualCert")),
        "is_psa_dna":   bool(raw.get("IsPSADNA")),
        "source":       "psa",
    }
    _cache_set(redis_client, cache_key, result, _POS_TTL_S)
    return result
