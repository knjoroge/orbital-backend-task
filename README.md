# Orbital Copilot — Usage API

[![CI](https://github.com/knjoroge/orbital-backend-task/actions/workflows/ci.yml/badge.svg)](https://github.com/knjoroge/orbital-backend-task/actions/workflows/ci.yml)

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

With your venv activated:

```bash
pytest -v
```

Or with `uv` (no activation needed):

```bash
uv run pytest -v
```

You should see 18 tests pass in well under a second — no real network
calls, upstream HTTP is mocked with `respx`. The unit tests on
`calculate_text_credits` each isolate one pricing rule and work the
expected number out step-by-step in a comment, so you can audit them
against the brief without running the code.

## What happens when you hit `/usage`

1. **Fetch the period's messages** from the upstream
   `/messages/current-period` endpoint. If that fails, return a 502
   immediately — there's no useful response to build.

2. **Dedupe the report IDs** across the messages, and **fetch each
   unique report in parallel** via `asyncio.gather`. 404s come back as
   `None` (meaning "report not found, fall back to text pricing"); any
   other non-200 fails the whole request.

3. **Price each message:**
   - If the message has a `report_id` and the lookup returned a report
     → use `report.credit_cost` and attach `report_name` to the entry.
   - Otherwise (no `report_id`, or the lookup returned 404) → price
     from the message text via `calculate_text_credits`, and omit
     `report_name`.

4. **Return** `{"usage": [...]}` — one entry per message with
   `message_id`, `timestamp`, `credits_used`, and optionally
   `report_name`.

A typical response looks like:

```json
{
  "usage": [
    {
      "message_id": 1000,
      "timestamp": "2024-04-29T02:08:29.375Z",
      "report_name": "Tenant Obligations Report",
      "credits_used": 79.0
    },
    {
      "message_id": 1001,
      "timestamp": "2024-04-29T03:25:03.613Z",
      "credits_used": 5.2
    }
  ]
}
```

On the real upstream — ~110 messages, ~25 of them with reports — the
whole request takes around 300ms end-to-end, most of which is the
upstream round-trips.

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

Two choices in the route are worth noting. **Deduping `report_id`s
before the parallel fetch** isn't a micro-optimization — the real
period repeats them heavily (id 1124 "Short Lease Report" appears 5+
times in a single period). And **failing loud on any non-404 upstream
error** comes from the brief's emphasis on billing accuracy: a 500
from the reports endpoint doesn't tell us what a credit cost should
have been, and serving partial usage data with some messages silently
omitted seemed worse than surfacing a 502 to the caller.

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
