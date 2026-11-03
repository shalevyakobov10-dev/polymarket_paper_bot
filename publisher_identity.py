"""Normalize publisher identity across RSS channels and GDELT article domains.

Publisher identity is a source-diversity aid, not proof that reports are
independent or that an event has been verified.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_SOURCE_ALIASES = {
    "bbc news": "bbc",
    "the guardian": "guardian",
    "al jazeera english": "al-jazeera",
    "al jazeera": "al-jazeera",
    "npr": "npr",
    "deutsche welle": "dw",
    "sky news": "sky",
    "france 24": "france24",
}
_DOMAIN_ALIASES = {
    "bbc.com": "bbc",
    "bbc.co.uk": "bbc",
    "bbci.co.uk": "bbc",
    "theguardian.com": "guardian",
    "aljazeera.com": "al-jazeera",
    "npr.org": "npr",
    "dw.com": "dw",
    "dw.de": "dw",
    "sky.com": "sky",
    "france24.com": "france24",
}
_MULTI_LEVEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk",
    "com.au", "net.au", "org.au",
    "co.nz", "co.jp", "com.br", "com.mx",
    "co.in", "com.sg", "com.tr", "com.cn",
    "com.tw", "co.za",
}


def _hostname(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if any(character.isspace() for character in raw):
        return ""
    if "://" not in raw and "." not in raw.split("/", 1)[0]:
        return ""
    candidate = raw if "://" in raw else "//" + raw
    try:
        host = (urlsplit(candidate).hostname or "").lower().strip(".")
    except ValueError:
        return ""
    while host.startswith(("www.", "feeds.", "rss.", "m.", "mobile.")):
        host = host.split(".", 1)[1]
    return host


def _domain_identity(host: str) -> str:
    if not host:
        return ""
    for domain, publisher in _DOMAIN_ALIASES.items():
        if host == domain or host.endswith("." + domain):
            return publisher
    labels = [part for part in host.split(".") if part]
    if len(labels) <= 2:
        return host
    suffix = ".".join(labels[-2:])
    if suffix in _MULTI_LEVEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def publisher_identity(
    source: str = "",
    feed_url: str = "",
    article_url: str = "",
) -> str:
    """Return a stable publisher key, preferring known outlet aliases."""
    source_base = str(source or "").split(" - ", 1)[0].strip()
    label_key = re.sub(r"[^a-z0-9]+", " ", source_base.lower()).strip()
    if label_key in _SOURCE_ALIASES:
        return _SOURCE_ALIASES[label_key]

    # For indexed media, the publisher's article domain is stronger than the
    # aggregation service or the query lane that surfaced the result.
    for candidate in (article_url, feed_url, source):
        host = _hostname(candidate)
        identity = _domain_identity(host)
        if identity:
            return identity

    fallback = re.sub(r"[^a-z0-9]+", "-", label_key).strip("-")
    return fallback[:100]
