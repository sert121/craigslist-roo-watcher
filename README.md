# craigslist-roo-watcher

Scrapes SF Craigslist rooms/shared, filters to a whitelist of neighborhoods,
emits matches as JSON. A scheduled remote Claude agent runs this hourly and
creates a Gmail draft per new listing (deduped via Gmail draft search on
the listing's post_id).

## Agent runbook (every hour)

```bash
pip install -q requests beautifulsoup4
python scrape.py --all
```

`scrape.py --all` prints a JSON array of candidate listings. For each
candidate, the agent:

1. Searches Gmail drafts for the `post_id`. If a draft already exists, skip.
2. Otherwise calls `create_draft` with:
   - `to`: `["placeholder@example.com"]` (filled in manually after fetching
     the reply email from the listing — Craigslist gates that behind hCaptcha)
   - `subject`: `Interested in your room — <neighborhood>`
   - `body`: contents of `EMAIL_TEMPLATE.txt` with `{url}`, `{price}`,
     `{location}` substituted at the bottom.

## Email template

See `EMAIL_TEMPLATE.txt`.
