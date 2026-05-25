"""Orbital Copilot Usage API — GET /usage returns per-message credit cost
for the current billing period. Pricing rules are in calculate_text_credits."""
import asyncio
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


# Pricing rules — pure function, no I/O, so it audits cleanly against the spec.

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


# Route

async def _fetch_report(http: httpx.AsyncClient, report_id: int) -> dict | None:
    """Fetch one report. Returns None on 404 (spec: fall back to text pricing)."""
    try:
        r = await http.get(f"{BASE_URL}/reports/{report_id}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Failed to fetch report {report_id}: {e}")
    if r.status_code == 200:
        return r.json()
    if r.status_code == 404:
        return None
    # Anything other than 200/404 is unexpected — fail loud, billing accuracy matters.
    raise HTTPException(502, f"Unexpected status {r.status_code} for report {report_id}")


app = FastAPI(title="Orbital Copilot — Usage API")


@app.get("/usage")
async def get_usage():
    """Return credit usage for every message in the current billing period."""

    async with httpx.AsyncClient(timeout=10.0) as http:
        try:
            res = await http.get(f"{BASE_URL}/messages/current-period")
            res.raise_for_status()
            messages = res.json().get("messages", [])
        except httpx.HTTPError as e:
            # Billing accuracy matters more than partial data — fail loud.
            raise HTTPException(502, f"Failed to fetch messages: {e}")

        # Fetch each unique report once, in parallel. The real period
        # repeats report_ids — id 1124 "Short Lease Report" appears 5+
        # times — so deduping keeps the upstream call count down. 404s
        # stay in the dict as None so fallback paths don't refetch.
        unique_ids = list({m["report_id"] for m in messages if m.get("report_id") is not None})
        fetched = await asyncio.gather(*(_fetch_report(http, rid) for rid in unique_ids))
        reports: dict[int, dict | None] = dict(zip(unique_ids, fetched))

        usage = []
        for msg in messages:
            entry = {"message_id": msg["id"], "timestamp": msg["timestamp"]}
            report = reports.get(msg.get("report_id"))

            if report is not None:
                entry["report_name"] = report["name"]
                entry["credits_used"] = float(report["credit_cost"])
            else:
                # No report (or 404) — price from the message text.
                # `report_name` is OMITTED from the dict (not set to null).
                entry["credits_used"] = calculate_text_credits(msg.get("text", ""))

            usage.append(entry)

    return {"usage": usage}
