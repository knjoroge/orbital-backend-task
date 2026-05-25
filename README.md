# Orbital Copilot — Usage API

A small Python API that reports credit consumption for the current
billing period in Orbital Copilot.

## Context

[Orbital](https://www.orbital.tech/) builds property due-diligence and
risk-management technology for real estate lawyers and conveyancers.
**Orbital Copilot** is their AI assistant — users ask it questions about
legal documents (leases, titles), and can also request generated reports
like a *Short Lease Report* or *Tenant Obligations Report*.

Copilot is billed on credits consumed. This service stitches together
the raw message data and report-pricing data from two upstream APIs,
and exposes a single endpoint that consuming teams can use for billing.

## Running it

Requires Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Or with [`uv`](https://docs.astral.sh/uv/):

```bash
uv venv
uv pip install -r requirements.txt
uv run uvicorn main:app --reload
```

Then:

```bash
curl http://localhost:8000/usage | python3 -m json.tool
```

Interactive OpenAPI docs are at `http://localhost:8000/docs`.

If you'd rather use Docker:

```bash
docker build -t orbital-usage .
docker run --rm -p 8000:8000 orbital-usage
```

## Testing

```bash
pytest -v
```

The suite runs in well under a second — no real network calls, upstream
HTTP is mocked with `respx`. The unit tests on `calculate_text_credits`
each isolate one pricing rule and work the expected number out step-by-
step in a comment, so you can audit them against the brief without
running the code.

## How it's put together

The whole service lives in `main.py` — about 90 lines. The pricing
rules are a pure function (`calculate_text_credits`) with no I/O; the
route (`/usage`) handles the upstream calls and assembles the response.
I considered splitting them across modules, but at this size the extra
indirection felt like it'd cost more than it added. If a second
endpoint or a second pricing scheme appeared I'd refactor — I just
didn't want to design for that up front.

The pricing function applies the rules in the order the brief lists
them. The palindrome rule doubles the running total, and the minimum-1
floor is applied last, after the doubling. That last call is the one
piece of the brief I'd flag for review: "the minimum cost should still
be 1 credit" lives inside the unique-word-bonus bullet, but I read it
as a global guarantee rather than a rule scoped to that one bonus.
Either reading is defensible — I went with the more conservative one.

The route fetches messages first, then issues all the report lookups in
parallel via `asyncio.gather`. The real upstream period repeats the
same `report_id` across many messages — id 1124, "Short Lease Report",
appears 5+ times in one period — so the report IDs are deduped before
the parallel fetch. 404s are kept in the dict as `None`, which means
messages whose report lookup falls back to text pricing still avoid
refetching. Any other upstream failure — 5xx, timeout, network error —
surfaces as a 502 to the caller. The brief stresses that billing
accuracy matters, so I chose to fail loud rather than silently serve
partial data.

A few smaller decisions worth flagging:

- `report_name` is **omitted** when there's no report, not set to
  `null` — the brief specifies "this field should be omitted".
  Building the response as a plain dict and conditionally adding the
  key handles that in one line.
- Credits are rounded to 2 decimal places at the end, after the
  palindrome doubling, so floats like `9.350000000000001` don't reach
  consumers. Rounding per-rule instead would let the errors compound.
- One of the tests asserts on `"orbital latibro"` — a string that
  actually appears in the real upstream data (message id 1104).
  `latibro` is `orbital` reversed, so `"orbitallatibro"` is a
  palindrome. Felt worth including alongside the synthetic palindrome
  tests as a check against real data.

## What I'd add next

The first thing I'd add is **retries with backoff** on transient
upstream failures (via `tenacity` or `httpx-retries`). The current
behavior is to surface any non-404 upstream failure as a 502, which
fits a billing service — but a brief network blip shouldn't have to
fail a whole period's worth of usage. The other thing I'd add before
running this in production is **structured logging with request IDs**,
to make it easier to diagnose those upstream issues when they happen.
