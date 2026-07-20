# Fold scraper

Reads accounting practice listings from broker sites and writes them into Supabase.
Runs on Railway on a schedule. Nothing here depends on a chat session or a laptop.

## Sources

| key | broker | notes |
|---|---|---|
| aba | ABA Advisors | detail page per listing, reads Sale Pending |
| naab | Naab Consulting | prints Status directly |
| aps | Accounting Practice Sales | biggest source, publishes asking price, stops at the sold line |
| poe | Poe Group Advisors | publishes asking price and annual gross |
| ppt | Private Practice Transitions | publishes revenue, SDE, EBITDA, asking price |
| atb | Accounting and Tax Brokerage | California |
| businessesforsale | BusinessesForSale | marketplace, always loses a dedupe tie |

## Two speeds

    python main.py light    hourly, index pages only, finds new and vanished
    python main.py deep     twice daily, re reads detail pages for status changes

## Setup on Railway

1. Push this folder to a GitHub repo
2. Railway, New Project, Deploy from GitHub repo
3. Variables tab, add:
   - SUPABASE_URL
   - SUPABASE_SERVICE_KEY  (Supabase, Settings, API, service_role key. Never put this in the website.)
4. Settings, Cron Schedule. Add two cron jobs:
   - `0 * * * *`      runs light
   - `0 11,23 * * *`  runs deep  (6am and 6pm Central)

## Rules the engine enforces

- **first_seen never moves.** The age clock depends on it.
- **Everything on the very first run is tagged Legacy.** True age is unknown, so no fake day counts.
- **The clock freezes** when a listing goes pending or sold, storing days_on_market.
- **A vanished listing freezes at last_seen, not today**, and is labelled `no_longer_listed`, because we do not know whether it sold or was withdrawn.
- **Two consecutive misses** before a listing is retired, so a timeout cannot retire it.
- **Source collapse guard.** If a source returns less than half its recent listings, the whole source is discarded for that run and logged loudly. A broken parser looks exactly like everything sold.
- **Dedupe prefers the origin broker.** Marketplaces only surface a practice nobody else carries. The loser is recorded in `also_listed_at`.
