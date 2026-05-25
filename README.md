# Orbital Copilot — Usage API

A small Python API that reports credit consumption for the current
billing period in Orbital Copilot.

## Context

[Orbital](https://www.orbital.tech/) builds property due-diligence and
risk-management technology for real estate lawyers and conveyancers.
**Orbital Copilot** is their AI assistant — users ask it questions about
legal documents (leases, titles), and can also request generated reports
like a *Short Lease Report* or *Tenant Obligations Report*.

Copilot is billed on a credits-consumed basis. This service combines
the raw message data and report-pricing data from two upstream APIs and
exposes a single endpoint that consuming teams can use for billing.

## Run

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Then:

```bash
curl http://localhost:8000/usage | python -m json.tool
```

Interactive OpenAPI docs at `http://localhost:8000/docs`.

## Test

```bash
pytest -v
```

The suite runs in well under a second — no real network calls, upstream
HTTP is mocked with `respx`.

## Files

```
main.py          # FastAPI app: pricing rules + /usage route
test_main.py     # Unit tests on pricing + integration tests on /usage
requirements.txt
README.md
```

## Decisions worth highlighting

**Single file.** The whole API is about 80 lines of code. Splitting it
across modules (separate schemas, upstream client, service layer) would
add imports and indirection without making it clearer. If a second
endpoint or a second pricing scheme appeared, I'd refactor — but
premature structure trades readability now for flexibility I may never
need.

**Pure function for the credit math.** `calculate_text_credits` has no
I/O — every spec rule is a numbered step, in the order the spec lists
them. This matters: the palindrome rule doubles the running total, and
the minimum-1 floor is applied last. Pure means it's trivially unit-
testable. Each test has the arithmetic in a comment so a reviewer can
verify expected values against the brief without running the code.

**Minimum-1 floor applied LAST, after palindrome doubling.** The brief is
slightly ambiguous here — the floor is mentioned inside the unique-word
bullet, but I read "the minimum cost should still be 1 credit" as a
global guarantee applied after all other rules including doubling. This
is the one judgement call worth flagging in review.

**`report_name` is omitted, not null.** The brief says "this field should
be omitted" — building the response as a plain dict and conditionally
adding the key handles that cleanly in one line. (No Pydantic model
needed for a one-endpoint API.)

**Rounding to 2 decimal places.** Credits are money-like; floats like
`9.350000000000001` shouldn't reach consumers. I round once, at the end,
after the palindrome doubling. Rounding earlier would compound.

**404 handling on report lookups.** Per the brief, a 404 means "report
not found, fall back to text pricing". Any other upstream failure (5xx,
timeout, network error) surfaces as a 502 to the caller. The brief
explicitly stresses that billing accuracy matters, so I'd rather fail
loud than silently serve partial data.

**Async client.** `httpx.AsyncClient` matches FastAPI's async runtime and
makes it a one-line change to parallelize report lookups with
`asyncio.gather` if that becomes a hotspot.

## Concessions / things I'd add with more time

These would be follow-up work — left out to keep the solution honest to
the 2–3 hour budget:

- **Parallel report lookups via `asyncio.gather`.** Currently the route
  fetches reports one at a time inside the loop. With ~100 messages in
  the real billing period and ~30 of those being report requests, this
  is the most obvious performance win.
- **Per-request memoization of report lookups.** The real upstream data
  shows the same `report_id` repeating across many messages (e.g.
  `1124` for "Short Lease Report" appears 5+ times). A small `dict`
  cache keyed by `report_id` would cut upstream calls significantly.
- **Retries with backoff** on transient upstream failures (via `tenacity`
  or `httpx-retries`).
- **Structured logging + request IDs** for production diagnosability.
- **Property-based tests** with Hypothesis on the credit calculator to
  catch edge cases (Unicode letters, very long inputs, etc.) that
  example-based tests can miss.
- **A `Dockerfile`** for one-command running.

## Things worth knowing when reviewing

- The unit tests are the most important part of the suite — each one
  isolates a single pricing rule, with the expected number worked out
  step-by-step in the comment.
- I deliberately included a test for `"orbital latibro"`, which appears
  in the real upstream data (message id 1104). `"orbitallatibro"` is a
  palindrome — `latibro` is `orbital` reversed. Nice sanity check that
  the palindrome rule fires on real data, not just contrived examples.
- The `test_length_penalty_over_100` test uses a deliberately non-
  palindromic 101-char string. My first attempt used `'z' * 101`,
  which IS a palindrome — the length-penalty rule fired but the result
  got silently doubled. Good reminder of why isolating one rule per
  test matters.
