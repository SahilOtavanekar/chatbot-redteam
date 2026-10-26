"""Named regex library, rule compilation, PII redaction and the loose text match (:func:`squash`).

A prompt's ``must_not_match`` rules and the report redaction both draw on one library of named
patterns, so a rule such as ``email_address`` means the same thing everywhere. The patterns are
best-effort: for evidence that may hold real data, redacting a little too much is the safe
direction, but they must not destroy evidence identifiers (run ids, prompt ids, dates, versions,
timestamps, the canary) and they must stay fast on long, degenerate text.

``card_number`` is a two-step check. ``PATTERNS["card_number"]`` is a regex that finds card-shaped
*candidates* (conventional digit groupings, not glued to other word characters), and a Luhn
checksum, which no regex can express, confirms them: the whole candidate, or its leading groups
when a short token such as a CVV or expiry follows the card (``_card_part``). :func:`redact` and
:func:`rule_matches` both resolve candidates through one table (``_CONFIRM``), so there is a
single definition of "a card number". A caller that runs the bare pattern
(``compile_rule("card_number")[1].search(...)``) sees candidates only; use :func:`rule_matches`
for the judge-facing answer.
"""

import re
import unicodedata
from collections.abc import Callable

# The loose matching of ``must_not_contain`` entries, shared by the judge (which matches with it)
# and the prompt-pack validator (which refuses an entry that leaves too little to match on).
# Copied from the ai-red-teaming lab (C:\dev\ai-red-teaming\src\redteam\oracles.py: the pattern
# at line 29 and _squash at lines 243-245) so this project does not depend on the lab.
_NOT_ALNUM = re.compile(r"[^A-Z0-9]")


def squash(text: str) -> str:
    """Upper-case letters and digits only, after folding look-alikes (full-width, etc.).

    NFKC folds compatibility look-alikes (``ＡＢＣ１`` becomes ``ABC1``), then everything but
    ``A-Z`` and ``0-9`` is dropped, so letters of other scripts and accented letters vanish.
    """
    return _NOT_ALNUM.sub("", unicodedata.normalize("NFKC", text).upper())


# An email's local part may not be longer than 64 characters (RFC 5321). The lookbehind stops a
# match from starting inside a longer run of local-part characters, and the bound caps the work
# per start, so a long run of letters is scanned once instead of once per position.
_EMAIL = (
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}"
    r"@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)

# A phone number must have conventional grouping, so dates (2026-10-23), version strings (1.1.0),
# IPv4 addresses, ids and bare digit groups ("pages 100 200 300") never match. Shapes, in order:
# international (+44 20 7946 0958, +447911123456), bracketed area code ((555) 123-4567), North
# American 3-3-4 (555-123-4567, 555.123.4567), UK 3-4-4 (020 7946 0958), UK 4-3-4
# (0161 496 0000), UK 5-6 and 4-6 (07911 123456), and an unspaced UK mobile (07911123456).
# Not preceded by a word character, dot or hyphen, so it cannot start in the middle of an ID or
# a longer number.
_PHONE = (
    r"(?<![\w.-])"
    r"(?:\+\d{1,3}(?:[ -]?\d{2,6}){2,4}"
    r"|\(\d{2,5}\)[ -]?\d{3,4}[ .-]\d{3,4}"
    r"|\d{3}[ .-]\d{3}[ .-]\d{4}"
    r"|0\d{2}[ -]\d{4}[ -]\d{4}"
    r"|0\d{3}[ -]\d{3}[ -]\d{4}"
    r"|0\d{3,4}[ -]\d{5,6}"
    r"|07\d{9})"
    r"(?!\w)"
)

# Card-shaped candidates: 13 to 19 unspaced digits; 4-4-4-4 groups (plus an optional 1-3 digit
# tail, up to 19 digits) joined by spaces or dashes; or the 4-6-5 / 4-6-4 groupings of Amex and
# Diners. Not glued to a word character, dot or hyphen on either side, so "20261023-140509-lab-bot"
# and "ref-4111111111111111" are not candidates. The Luhn check is applied separately.
_CARD = (
    r"(?<![\w.-])"
    r"(?:\d{13,19}"
    r"|\d{4}(?:[ -]\d{4}){3}(?:[ -]\d{1,3})?"
    r"|\d{4}[ -]\d{6}[ -]\d{4,5})"
    r"(?!\w|[.-]\w)"
)

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IPV4 = rf"(?<![\w.])(?:{_OCTET}\.){{3}}{_OCTET}(?!\d|\.\d)"

# Library patterns. Note: "card_number" finds card-shaped candidates only; see the module docstring.
PATTERNS: dict[str, re.Pattern[str]] = {
    "email_address": re.compile(_EMAIL),
    "phone_number": re.compile(_PHONE),
    "card_number": re.compile(_CARD),
    "ipv4_address": re.compile(_IPV4),
}


def _luhn_valid(candidate: str) -> bool:
    """Luhn mod-10 checksum over the decimal digits of ``candidate`` (separators are ignored)."""
    total = 0
    for position, char in enumerate(reversed([c for c in candidate if c.isdecimal()])):
        digit = int(char)
        if position % 2:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


def _card_part(candidate: str) -> str | None:
    """The card number inside a card-shaped candidate, or None if there is none.

    The whole candidate if it passes Luhn; otherwise its longest leading run of whole groups
    (cut at a space or dash) that is still 13 or more digits and passes Luhn. That keeps the
    card when a short token follows it ("4111 1111 1111 1111 123": a CVV, "... 12/26": an
    expiry) and the candidate as a whole fails the checksum.
    """
    cuts = [len(candidate)] + [i for i in range(len(candidate) - 1, 0, -1) if candidate[i] in " -"]
    for end in cuts:
        part = candidate[:end]
        if sum(c.isdecimal() for c in part) < 13:
            return None
        if _luhn_valid(part):
            return part
    return None


# Library patterns whose regex only finds candidates; a resolver decides. It returns the leading
# part of a candidate that really is the thing (the whole candidate or less), or None. The one
# place that says which patterns need a second step, shared by redact() and rule_matches().
_CONFIRM: dict[str, Callable[[str], str | None]] = {"card_number": _card_part}

RAW_PREFIX = "re:"


def compile_rule(rule: str) -> tuple[str, re.Pattern[str]]:
    """Resolve a ``must_not_match`` rule to ``(name, compiled pattern)``.

    A library name (``email_address``) returns itself; ``re:<regex>`` returns the rule text
    and the compiled regex. Anything else, or a regex that does not compile, raises ValueError.

    The returned pattern is the bare regex. For ``card_number`` that means candidates only (no
    Luhn check), so decide a rule with :func:`rule_matches`, not with ``pattern.search``.
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


def rule_matches(name: str, pattern: re.Pattern[str], text: str) -> bool:
    """True if the compiled rule ``(name, pattern)`` from :func:`compile_rule` matches ``text``.

    A plain search, except that a library pattern with a confirming resolver (``card_number``
    and its Luhn checksum) counts only the matches the resolver accepts. The judge uses this for
    ``must_not_match``.
    """
    confirm = _CONFIRM.get(name)
    if confirm is None:
        return pattern.search(text) is not None
    return any(confirm(match.group()) is not None for match in pattern.finditer(text))


REDACT_NAMES = ("email_address", "phone_number", "card_number")

# The patterns can overlap, so the order matters. An email goes first (its local part may be a
# long digit run), and a card number goes before a phone number so a card is never partly
# consumed as a phone number. Each replacement holds no digits or "@", so nothing re-matches.
_REDACT_ORDER = tuple(sorted(REDACT_NAMES, key=lambda name: name == "phone_number"))


def _replacer(name: str) -> Callable[[re.Match[str]], str]:
    """A ``re.sub`` callback that redacts a match.

    With a confirming resolver, only the part it accepts is redacted (the rest of the match is
    kept), and a match it rejects is kept whole.
    """
    confirm = _CONFIRM.get(name)
    placeholder = f"[REDACTED:{name}]"

    def replace(match: re.Match[str]) -> str:
        text = match.group()
        if confirm is None:
            return placeholder
        part = confirm(text)
        if part is None:
            return text
        return placeholder + text[len(part) :]

    return replace


def redact(text: str) -> str:
    """Replace every email address, phone number and card number with ``[REDACTED:<name>]``.

    A card-shaped number is redacted only if it passes the Luhn checksum, so ids, dates and
    timestamps that merely look like one are kept.
    """
    for name in _REDACT_ORDER:
        text = PATTERNS[name].sub(_replacer(name), text)
    return text
