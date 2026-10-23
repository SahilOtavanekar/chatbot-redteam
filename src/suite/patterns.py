"""Named regex library, rule compilation and PII redaction.

A prompt's ``must_not_match`` rules and the report redaction both draw on one library of named
patterns, so a rule such as ``email_address`` means the same thing everywhere. The patterns are
best-effort and deliberately loose: for evidence that may hold real data, redacting too much is
the safe direction.
"""

import re

_EMAIL = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"

# A phone number must contain separators, so dates (2026-10-23), version strings (1.1.0), IPv4
# addresses and plain digit runs never match. Grouped forms: an optional +country code, then an
# area code (bare or in brackets) and two more groups (555-123-4567, (555) 123-4567,
# +44 20 7946 0958), or the dotted US form 555.123.4567. Not preceded by a word character,
# dot or hyphen, so it cannot start in the middle of an ID or a longer number.
_PHONE = (
    r"(?<![\w.-])"
    r"(?:(?:\+\d{1,3}[ -]?)?(?:\(\d{2,4}\)[ -]?|\d{2,4}[ -])\d{3,4}[ -]\d{3,4}"
    r"|\d{3}\.\d{3}\.\d{4})"
    r"(?!\w)"
)

# 13 to 19 digits, each pair optionally split by one space or dash.
_CARD = r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)"

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IPV4 = rf"(?<![\w.])(?:{_OCTET}\.){{3}}{_OCTET}(?!\d|\.\d)"

PATTERNS: dict[str, re.Pattern[str]] = {
    "email_address": re.compile(_EMAIL),
    "phone_number": re.compile(_PHONE),
    "card_number": re.compile(_CARD),
    "ipv4_address": re.compile(_IPV4),
}

RAW_PREFIX = "re:"


def compile_rule(rule: str) -> tuple[str, re.Pattern[str]]:
    """Resolve a ``must_not_match`` rule to ``(name, compiled pattern)``.

    A library name (``email_address``) returns itself; ``re:<regex>`` returns the rule text
    and the compiled regex. Anything else, or a regex that does not compile, raises ValueError.
    """
    if rule in PATTERNS:
        return rule, PATTERNS[rule]
    if rule.startswith(RAW_PREFIX):
        regex = rule[len(RAW_PREFIX) :]
        if not regex:
            raise ValueError(f"{rule!r} needs a regular expression after '{RAW_PREFIX}'")
        try:
            return rule, re.compile(regex)
        except re.error as exc:
            raise ValueError(f"invalid regular expression in {rule!r}: {exc}") from None
    raise ValueError(
        f"unknown pattern {rule!r}; use one of {', '.join(PATTERNS)} or '{RAW_PREFIX}<regex>'"
    )


REDACT_NAMES = ("email_address", "phone_number", "card_number")

# The patterns can overlap, so the order matters. An email goes first (its local part may be a
# long digit run), and a card number goes before a phone number (a 4-4-4-4 card number also has
# the shape of a phone number). Each replacement holds no digits or "@", so nothing re-matches.
_REDACT_ORDER = tuple(sorted(REDACT_NAMES, key=lambda name: name == "phone_number"))


def redact(text: str) -> str:
    """Replace every email address, phone number and card number with ``[REDACTED:<name>]``."""
    for name in _REDACT_ORDER:
        text = PATTERNS[name].sub(f"[REDACTED:{name}]", text)
    return text
