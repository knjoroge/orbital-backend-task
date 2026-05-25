"""
Orbital Copilot — Usage API

Orbital Copilot is an AI assistant for real estate lawyers. Users ask it
questions about legal documents (leases, titles) and can also request
generated reports (e.g. "Short Lease Report", "Tenant Obligations Report").
Copilot bills on consumption — each interaction costs a number of credits.

This service exposes ONE endpoint, GET /usage, which returns the credit
cost per message for the current billing period. For each message we
either look up a fixed report cost upstream, or calculate a cost from
the message text using the pricing rules in the brief.

Everything lives in this one file because the task is small enough that
splitting it across modules would obscure rather than clarify. The
sections below are: pricing rules, then the route.
"""
import re
import httpx
from fastapi import FastAPI, HTTPException

BASE_URL = "https://owpublic.blob.core.windows.net/tech-task"

# A "word" per the spec: a continuous run of letters, apostrophes, and
# hyphens. Compiled once at import time. Real Copilot questions like
# "tenant's obligations" or "state-of-the-art" need apostrophes/hyphens
# to count as part of one word.
WORD_RE = re.compile(r"[A-Za-z'\-]+")
VOWELS = set("aeiouAEIOU")


# ---------------------------------------------------------------------------
# Pricing rules — pure function, no I/O. Easy to test, easy to audit
# against the spec line-by-line.
# ---------------------------------------------------------------------------

def calculate_text_credits(text: str) -> float:
    """Apply the text-based pricing rules from the brief, in order."""

    # 1. Base cost — every message starts at 1 credit.
    total = 1.0

    # 2. Character count — 0.05 per character (includes spaces/punctuation).
    total += 0.05 * len(text)

    # 3. Word-length multipliers (1-3 chars / 4-7 / 8+).
    words = WORD_RE.findall(text)
    for w in words:
        if len(w) <= 3:
            total += 0.1
        elif len(w) <= 7:
            total += 0.2
        else:
            total += 0.3

    # 4. Every 3rd character (index 2, 5, 8...) that's a vowel adds 0.3.
    #    text[2::3] is a slice that grabs exactly those positions.
    total += sum(0.3 for ch in text[2::3] if ch in VOWELS)

    # 5. Length penalty for messages over 100 characters.
    if len(text) > 100:
        total += 5.0

    # 6. Unique-word bonus — case-sensitive, so "The" and "the" count as
    #    different words per the spec's explicit note.
    if words and len(set(words)) == len(words):
        total -= 2.0

    # 7. Palindrome check — lowercase, strip non-alphanumerics, compare to
    #    reverse. The brief includes a fun example in the data: message
    #    1104 "orbital latibro" normalizes to "orbitallatibro" which IS
    #    a palindrome (latibro = orbital reversed). Doubles the total.
    cleaned = "".join(ch for ch in text.lower() if ch.isalnum())
    if cleaned and cleaned == cleaned[::-1]:
        total *= 2

    # 8. Minimum 1 credit. Applied LAST so it's a global guarantee — the
    #    unique-word bonus can drive the subtotal below 1 even after
    #    palindrome doubling.
    return max(round(total, 2), 1.0)


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------

app = FastAPI(title="Orbital Copilot — Usage API")


@app.get("/usage")
async def get_usage():
    """Return credit usage for every message in the current billing period."""

    # One AsyncClient per request is fine at this scale. If we needed
    # higher throughput we'd promote it to app.state via a lifespan handler
    # to keep keep-alive connections warm.
    async with httpx.AsyncClient(timeout=10.0) as http:
        # 1. Fetch all messages for the current period.
        try:
            res = await http.get(f"{BASE_URL}/messages/current-period")
            res.raise_for_status()
            messages = res.json().get("messages", [])
        except httpx.HTTPError as e:
            # Billing accuracy matters more than partial data — fail loud.
            raise HTTPException(502, f"Failed to fetch messages: {e}")

        # 2. Build one usage entry per message.
        usage = []
        for msg in messages:
            entry = {
                "message_id": msg["id"],
                "timestamp": msg["timestamp"],
            }

            report_id = msg.get("report_id")
            report = None

            # If the message triggered a report (e.g. "Produce a Short Lease
            # Report"), look up its fixed credit cost. A 404 means the
            # report ID is unknown — per the spec, fall back to text pricing.
            # Any other status is unexpected and surfaces as an error.
            if report_id is not None:
                try:
                    r = await http.get(f"{BASE_URL}/reports/{report_id}")
                except httpx.HTTPError as e:
                    raise HTTPException(502, f"Failed to fetch report {report_id}: {e}")
                if r.status_code == 200:
                    report = r.json()
                elif r.status_code != 404:
                    raise HTTPException(
                        502,
                        f"Unexpected status {r.status_code} for report {report_id}",
                    )

            if report is not None:
                # Report found — use its name and cost; ignore the message text.
                entry["report_name"] = report["name"]
                entry["credits_used"] = float(report["credit_cost"])
            else:
                # No report (or 404) — price from the message text.
                # `report_name` is OMITTED from the dict (not set to null),
                # per the literal reading of the spec.
                entry["credits_used"] = calculate_text_credits(msg.get("text", ""))

            usage.append(entry)

    return {"usage": usage}
