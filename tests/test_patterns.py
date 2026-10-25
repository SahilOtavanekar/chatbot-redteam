import time

import pytest

from suite.patterns import PATTERNS, REDACT_NAMES, compile_rule, redact, rule_matches


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


# --- Fix round 1, Important 1: card numbers are Luhn-checked and shape-checked ----------------


@pytest.mark.parametrize(
    "s",
    [
        "Run 20261023-140509-lab-bot finished",
        "PI-001 2026-10-23 14:05",
        "On 2026-10-23 12345 requests",
        "ts 1698062400000 ms",
    ],
)
def test_redact_keeps_run_ids_dates_and_timestamps_that_looked_like_cards(s):
    assert redact(s) == s


def test_redact_keeps_run_ids_and_dates_whatever_their_checksum():
    # Luhn alone would let about one in ten of these through; the shape check must not.
    for day in range(1, 29):
        for second in range(0, 60, 3):
            run_id = f"Run 202610{day:02d}-1405{second:02d}-lab-bot finished"
            assert redact(run_id) == run_id
            bare = f"id 202610{day:02d}-1405{second:02d} ok"
            assert redact(bare) == bare
            count = f"On 2026-10-{day:02d} 12{second:02d}5 requests"
            assert redact(count) == count


@pytest.mark.parametrize(
    "s",
    [
        "4111111111111111-lab-bot",
        "lab-bot-4111111111111111",
        "x4111111111111111",
        "4111 1111 1111 1111-ab",
    ],
)
def test_redact_leaves_digits_glued_to_a_letter_bearing_token(s):
    assert redact(s) == s


@pytest.mark.parametrize(
    "number",
    [
        "5555555555554444",
        "6011 1111 1111 1117",
        "3530-1113-3330-0000",
        "3056 930902 5904",
        "378282246310005",
    ],
)
def test_redacts_valid_cards_of_each_network_shape(number):
    assert redact(f"pay with {number}.") == "pay with [REDACTED:card_number]."


@pytest.mark.parametrize(
    "number",
    ["4111 1111 1111 1112", "4111-1111-1111-1112", "4111111111111112", "3782 822463 10006"],
)
def test_redact_leaves_card_shaped_numbers_that_fail_the_luhn_check(number):
    assert redact(f"card {number} ok") == f"card {number} ok"


# --- Fix round 1, Important 2: no quadratic backtracking on long text -------------------------


@pytest.mark.parametrize(
    "text",
    [
        "a" * 100_000,
        "a." * 50_000,
        "a@" * 50_000,
        "a@" + "a." * 50_000,
        "1 " * 50_000,
        "1" * 100_000,
        "1-" * 50_000,
        "+1 " * 30_000,
        "(1)" * 30_000,
        "-" * 100_000,
    ],
    ids=[
        "letters",
        "dotted-letters",
        "at-signs",
        "long-domain",
        "spaced-digits",
        "digit-run",
        "dashed-digits",
        "plus-groups",
        "bracket-groups",
        "dashes",
    ],
)
def test_redact_is_fast_on_long_degenerate_text(text):
    start = time.perf_counter()
    redact(text)
    assert time.perf_counter() - start < 1.0


def test_email_local_part_is_bounded_at_64_characters():
    assert redact(f"{'a' * 64}@example.com") == "[REDACTED:email_address]"
    assert redact("see first.last+tag@sub.example.co.uk.") == "see [REDACTED:email_address]."


# --- Fix round 1, Minor 1: UK mobiles are phone numbers, bare digit groups are not -----------


@pytest.mark.parametrize(
    "number",
    ["+44 7911 123456", "07911 123456", "07911123456", "+447911123456"],
)
def test_redacts_uk_mobile_shapes(number):
    assert redact(f"call {number}.") == "call [REDACTED:phone_number]."


@pytest.mark.parametrize(
    "s",
    ["pages 100 200 300", "scores: 1200 3400 5600", "id 12-345-6789"],
)
def test_redact_leaves_unconventionally_grouped_digits_alone(s):
    assert redact(s) == s


# --- judge-facing helper: one Luhn check shared by redact() and must_not_match ----------------


def test_rule_matches_applies_the_luhn_check_to_the_card_rule():
    name, rx = compile_rule("card_number")
    assert rule_matches(name, rx, "pay 4111 1111 1111 1111 now")
    assert not rule_matches(name, rx, "ref 4111 1111 1111 1112")
    assert rx.search("ref 4111 1111 1111 1112")  # the bare pattern only finds candidates


def test_rule_matches_is_a_plain_search_for_every_other_rule():
    assert rule_matches(*compile_rule("email_address"), "write to a@b.co")
    assert not rule_matches(*compile_rule("email_address"), "no address here")
    assert rule_matches(*compile_rule("re:ab+c"), "xabbcx")
    assert not rule_matches(*compile_rule("re:ab+c"), "xacx")


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
