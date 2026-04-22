"""
cgc_cert.py — CGC Trading Cards cert lookup.

CGC has no public REST API for cards. Their cert verification page at
https://www.cgccards.com/certlookup/{cert}/ returns HTML with the cert
record embedded. We fetch + parse it and return the same shape as
psa_cert.lookup_cert() so the caller can treat both grading services
identically.

This is a best-effort scrape. If CGC restructures their HTML the parser
returns {"error": "parse"} and the caller falls back to PSA-only.

Cert records are immutable once assigned, so we cache positives 30 days
and not-founds 1 hour, matching psa_cert.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("hanryx.cgc_cert")

_CGC_URL = "https://www.cgccards.com/certlookup/{cert}/"
_UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) HanryxVault/1.0 Safari/537.36")

_POS_TTL_S = 30 * 86_400
_NEG_TTL_S = 3_600

_lru: dict[str, tuple[float, object]] = {}
_LRU_MAX = 4096


def _cache_get(redis_client, key: str):
    if redis_client is not None:
        try:
            raw = redis_client.get(key)
            if raw:
                return json.loads(raw)
        except Exception as e:
            log.debug("[cgc] redis get failed: %s", e)
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
            log.debug("[cgc] redis set failed: %s", e)
    if len(_lru) >= _LRU_MAX:
        for k in list(_lru.keys())[: _LRU_MAX // 10]:
            _lru.pop(k, None)
    _lru[key] = (time.time() + ttl, value)


def _txt(node) -> str:
    return (node.get_text(" ", strip=True) if node else "").strip()


def _find_field(soup: BeautifulSoup, label: str) -> str:
    """
    CGC's cert page uses a definition-list-ish layout where each fact is
    a label/value pair. The exact tag varies (dt/dd, th/td, span/span)
    so we look for any element whose text matches the label and grab
    the next sibling's text.
    """
    pat = re.compile(rf"^\s*{re.escape(label)}\s*:?\s*$", re.IGNORECASE)
    for tag in soup.find_all(["dt", "th", "span", "div", "label", "strong"]):
        if pat.match(_txt(tag)):
            sib = tag.find_next_sibling()
            if sib:
                v = _txt(sib)
                if v and not pat.match(v):
                    return v
            parent = tag.parent
            if parent:
                full = _txt(parent)
                stripped = re.sub(rf"^\s*{re.escape(label)}\s*:?\s*",
                                  "", full, flags=re.IGNORECASE).strip()
                if stripped:
                    return stripped
    return ""


def _parse_cgc_html(html: str, cert: str) -> Optional[dict]:
    soup = BeautifulSoup(html, "lxml")

    # CGC returns 200 with a "no cert found" message rather than 404.
    body_text = soup.get_text(" ", strip=True).lower()
    if ("no record" in body_text or "could not be found" in body_text
            or "no results" in body_text):
        return None

    grade        = _find_field(soup, "Grade")
    grade_label  = _find_field(soup, "Grade Description") or grade
    subject      = (_find_field(soup, "Card")
                    or _find_field(soup, "Title")
                    or _find_field(soup, "Subject"))
    set_name     = _find_field(soup, "Set") or _find_field(soup, "Series")
    year         = _find_field(soup, "Year")
    card_number  = _find_field(soup, "Card Number") or _find_field(soup, "#")
    variety      = (_find_field(soup, "Variant")
                    or _find_field(soup, "Variety")
                    or _find_field(soup, "Pedigree"))
    publisher    = _find_field(soup, "Publisher") or _find_field(soup, "Game")
    cert_no      = _find_field(soup, "Certification") or cert

    # Sub-grades (Pristine 10 / Perfect 10 break-out)
    subgrades = {}
    for label_key, out_key in [
        ("Centering", "centering"),
        ("Corners",   "corners"),
        ("Edges",     "edges"),
        ("Surface",   "surface"),
    ]:
        v = _find_field(soup, label_key)
        if v and re.match(r"^\d+(\.\d+)?$", v):
            try:
                subgrades[out_key] = float(v)
            except Exception:
                pass

    if not (grade or subject):
        return {"error": "parse"}

    return {
        "cert_number":  str(cert_no or cert).strip(),
        "grade":        str(grade or "").strip() or None,
        "grade_label":  str(grade_label or "").strip() or None,
        "year":         str(year or "").strip() or None,
        "brand":        str(publisher or "").strip() or None,
        "set_name":     str(set_name or "").strip() or None,
        "subject":      str(subject or "").strip() or None,
        "card_number":  str(card_number or "").strip() or None,
        "variety":      str(variety or "").strip() or None,
        "category":     "TCG Cards",
        "subgrades":    subgrades or None,
        "population":   None,   # CGC doesn't expose pop on the cert page
        "pop_higher":   None,
        "source":       "cgc",
    }


def lookup_cert(cert_number: str, redis_client=None) -> Optional[dict]:
    """
    Look up a CGC cert by number. Same return shape as psa_cert.lookup_cert.

    Possible error codes:
      • "bad_cert_format" — input not all-digits
      • "upstream"        — network / 5xx / scrape failure
      • "parse"           — CGC HTML changed shape unexpectedly
    """
    cert = (cert_number or "").strip()
    if not cert.isdigit():
        return {"error": "bad_cert_format"}

    cache_key = f"hv:cgc:cert:{cert}"
    cached = _cache_get(redis_client, cache_key)
    if cached is not None:
        return cached or None

    try:
        r = requests.get(
            _CGC_URL.format(cert=cert),
            headers={"User-Agent": _UA, "Accept": "text/html"},
            timeout=10,
        )
        if r.status_code == 404:
            _cache_set(redis_client, cache_key, {}, _NEG_TTL_S)
            return None
        if r.status_code == 429:
            return {"error": "rate_limited"}
        r.raise_for_status()
    except Exception as e:
        log.warning("[cgc] cert %s lookup failed: %s", cert, e)
        return {"error": "upstream"}

    result = _parse_cgc_html(r.text, cert)
    if result is None:
        _cache_set(redis_client, cache_key, {}, _NEG_TTL_S)
        return None
    if isinstance(result, dict) and result.get("error"):
        return result

    _cache_set(redis_client, cache_key, result, _POS_TTL_S)
    return result
