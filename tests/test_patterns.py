import pytest

from suite.patterns import PATTERNS, REDACT_NAMES, compile_rule, redact


def test_redacts_pii():
    s = "mail bob@example.com or call +44 20 7946 0958, card 4111 1111 1111 1111"
    assert redact(s) == (
        "mail [REDACTED:email_address] or call [REDACTED:phone_number], card [REDACTED:card_number]"
    )
    assert "[REDACTED:phone_number]" in redact("ring (555) 123-4567 now")


@pytest.mark.parametrize(
    "s",
    ["PI-001#2", "2026-10-23", "SYSTEM-CANARY-7Q4P", "version 1.1.0", "HTTP 503"],
)
def test_redact_leaves_ids_dates_and_canary(s):
    assert redact(s) == s


@pytest.mark.parametrize(
    "number",
    ["555-123-4567", "555.123.4567", "(555) 123-4567", "+1 555 123 4567", "020 7946 0958"],
)
def test_redacts_common_phone_formats(number):
    assert redact(f"call {number}.") == "call [REDACTED:phone_number]."


@pytest.mark.parametrize(
    "number",
    ["4111111111111111", "4111-1111-1111-1111", "3782 822463 10005", "4111 1111 1111 1111"],
)
def test_redacts_card_formats(number):
    assert redact(f"card {number} ok") == "card [REDACTED:card_number] ok"


def test_redact_leaves_ip_addresses_and_short_digit_runs_alone():
    s = "server 192.168.100.200 got 12 345 requests at 10:30 on build 2026-10-23"
    assert redact(s) == s


def test_redacts_an_email_whose_local_part_is_a_long_digit_run_as_one_email():
    assert redact("bob1234567890123@example.com") == "[REDACTED:email_address]"


def test_redact_replaces_every_match_and_keeps_surrounding_text():
    out = redact("a@b.io, c@d.org; e@f.net")
    assert out == "[REDACTED:email_address], [REDACTED:email_address]; [REDACTED:email_address]"


def test_redact_names_are_all_library_patterns():
    assert REDACT_NAMES == ("email_address", "phone_number", "card_number")
    assert set(REDACT_NAMES) <= set(PATTERNS)
    assert "ipv4_address" in PATTERNS


def test_ipv4_pattern_matches_addresses_only():
    ip = PATTERNS["ipv4_address"]
    assert ip.search("host 10.0.0.255.") and ip.search("1.2.3.4")
    assert not ip.search("999.1.1.1") and not ip.search("version 1.1.0")


def test_compile_rule_named_and_raw():
    assert compile_rule("email_address")[0] == "email_address"
    name, rx = compile_rule("re:ab+c")
    assert name == "re:ab+c" and rx.search("xabbcx")
    for bad in ("nope", "re:("):
        with pytest.raises(ValueError):
            compile_rule(bad)


def test_compile_rule_errors_say_what_is_wrong():
    with pytest.raises(ValueError, match="unknown pattern 'nope'.*email_address"):
        compile_rule("nope")
    with pytest.raises(ValueError, match=r"invalid regular expression in 're:\('"):
        compile_rule("re:(")
    with pytest.raises(ValueError, match="needs a regular expression"):
        compile_rule("re:")
