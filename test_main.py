"""
Tests for the Orbital Copilot Usage API.

Two groups:

  1. Unit tests on calculate_text_credits — one test per spec rule, with
     the expected number worked out in the comment so a reviewer can
     audit the arithmetic against the brief without running the code.

  2. Integration tests on /usage that mock the upstream HTTP with respx.
     These cover the report lookup, the 404 fallback, empty period, and
     a realistic mixed case.
"""
import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from main import app, calculate_text_credits, BASE_URL


# ---------------------------------------------------------------------------
# Unit tests — credit calculation rules
# ---------------------------------------------------------------------------

def test_base_cost_and_char_count():
    # "12345" — 5 digits, NO words (digits don't count as letters),
    # no vowels at index 2/5/...
    #   base=1.0, char=5*0.05=0.25, word=0, vowel=0, unique=0 (no words) = 1.25
    assert calculate_text_credits("12345") == 1.25


def test_word_length_buckets():
    # "hi hello wonderful" — one word per bucket (2, 5, and 9 chars).
    #   base=1.0, char=18*0.05=0.9, word=0.1+0.2+0.3=0.6
    #   vowel at idx 2,5,8,11,14,17 = ' l n r f l' -> none
    #   unique=-2.0 (all distinct)
    #   subtotal = 0.5 -> floored to 1.0
    assert calculate_text_credits("hi hello wonderful") == 1.0


def test_third_position_vowels():
    # "xxaxxexxi xxaxxexxi" — duplicate words (no unique bonus), vowels
    # at indices 2, 5, 8 in each copy.
    #   base=1.0, char=19*0.05=0.95, word=0.3+0.3=0.6
    #   vowel: idx 2,5,8,11,14,17 = a,e,i,x,e,x -> 3 vowels -> 0.9
    #   unique=0 (duplicate words)
    #   total = 3.45
    assert calculate_text_credits("xxaxxexxi xxaxxexxi") == pytest.approx(3.45)


def test_length_penalty_over_100():
    # 101 chars, NOT a palindrome (otherwise doubling would mask the penalty).
    text = ("abc" * 34)[:101]
    #   base=1.0, char=5.05, word=0.3 (one 101-char word, 8+ bucket),
    #   vowel=0 (idx 2,5,8,...= 'c'), penalty=5.0, unique=-2.0
    #   total = 9.35
    assert calculate_text_credits(text) == pytest.approx(9.35)


def test_length_penalty_not_at_exactly_100():
    # Spec says "exceeds 100" — at exactly 100 the penalty does NOT apply.
    text = ("abc" * 34)[:100]
    #   base=1.0, char=5.0, word=0.3, vowel=0, penalty=0, unique=-2.0 = 4.3
    assert calculate_text_credits(text) == pytest.approx(4.3)


def test_unique_word_bonus_is_case_sensitive():
    # "The the" — case-sensitive, so these ARE different unique words.
    # Bonus applies, total drops below 1, floor kicks in.
    assert calculate_text_credits("The the") == 1.0


def test_duplicate_words_no_unique_bonus():
    # "cat cat" — duplicate, so no bonus.
    #   base=1.0, char=0.35, word=0.1+0.1=0.2, vowel idx 5='a' -> 0.3
    #   total = 1.85
    assert calculate_text_credits("cat cat") == pytest.approx(1.85)


def test_palindrome_doubles_total():
    # "ab ba ab ba" normalizes to "abbaabba", a palindrome. Duplicate words
    # so no unique bonus (keeps subtotal above the floor — otherwise the
    # doubling would be masked).
    #   base=1.0, char=11*0.05=0.55, word=4*0.1=0.4, vowel=0, unique=0
    #   subtotal = 1.95, doubled = 3.90
    assert calculate_text_credits("ab ba ab ba") == pytest.approx(3.90)


def test_orbital_easter_egg_palindrome():
    # The real upstream data contains "orbital latibro" (message id 1104).
    # "orbitallatibro" reversed is "orbitallatibro" — it's a palindrome.
    # (`latibro` is `orbital` reversed.) Fun test of the rule on a string
    # that actually appears in production.
    #   text = "orbital latibro" (length 15)
    #   words = ["orbital", "latibro"] -> both 7 chars -> 0.2 + 0.2 = 0.4
    #   base=1.0, char=15*0.05=0.75
    #   vowels at idx 2,5,8,11,14 of "orbital latibro" = b,a,l,i,o
    #     -> 3 vowels (a, i, o) -> 0.9
    #   unique=-2.0 (two distinct words)
    #   subtotal = 1 + 0.75 + 0.4 + 0.9 - 2.0 = 1.05
    #   palindrome doubles -> 2.10
    assert calculate_text_credits("orbital latibro") == pytest.approx(2.10)


def test_minimum_one_credit_floor():
    # Short single unique word — bonus drives below 1, floored to 1.
    assert calculate_text_credits("hi") == 1.0


# ---------------------------------------------------------------------------
# Integration tests — /usage endpoint with mocked upstream
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    return TestClient(app)


@respx.mock
def test_text_only_message(client):
    respx.get(f"{BASE_URL}/messages/current-period").mock(
        return_value=httpx.Response(200, json={"messages": [
            {"id": 1, "timestamp": "2024-04-29T03:25:03.613Z",
             "text": "Is subletting permitted?"},
        ]})
    )
    res = client.get("/usage")
    assert res.status_code == 200
    item = res.json()["usage"][0]
    assert item["message_id"] == 1
    assert item["timestamp"] == "2024-04-29T03:25:03.613Z"
    # report_name should be OMITTED, not null.
    assert "report_name" not in item
    assert item["credits_used"] > 0


@respx.mock
def test_report_message_uses_report_cost(client):
    # Mirror a real message from the upstream: id 1010, "Create a Short
    # Lease Report.", report_id 1124.
    respx.get(f"{BASE_URL}/messages/current-period").mock(
        return_value=httpx.Response(200, json={"messages": [
            {"id": 1010, "timestamp": "2024-04-29T16:17:57.827Z",
             "text": "Create a Short Lease Report.", "report_id": 1124},
        ]})
    )
    respx.get(f"{BASE_URL}/reports/1124").mock(
        return_value=httpx.Response(200, json={"name": "Short Lease Report",
                                               "credit_cost": 75})
    )
    res = client.get("/usage")
    item = res.json()["usage"][0]
    assert item["report_name"] == "Short Lease Report"
    # Text is ignored when a valid report is found.
    assert item["credits_used"] == 75.0


@respx.mock
def test_report_404_falls_back_to_text(client):
    respx.get(f"{BASE_URL}/messages/current-period").mock(
        return_value=httpx.Response(200, json={"messages": [
            {"id": 7, "timestamp": "t", "text": "hi", "report_id": 999},
        ]})
    )
    respx.get(f"{BASE_URL}/reports/999").mock(return_value=httpx.Response(404))
    res = client.get("/usage")
    item = res.json()["usage"][0]
    assert "report_name" not in item  # no report name on fallback
    assert item["credits_used"] == 1.0  # text price of "hi"


@respx.mock
def test_empty_period(client):
    respx.get(f"{BASE_URL}/messages/current-period").mock(
        return_value=httpx.Response(200, json={"messages": []})
    )
    assert client.get("/usage").json() == {"usage": []}
