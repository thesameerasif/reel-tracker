# Reel Tracker

Pulls the latest reels from a public Instagram profile (via Apify) and shows views, engagement and length.

Run locally:

    pip install -r requirements.txt
    set APIFY_TOKEN, then: python app.py

Environment variables (set in the host's settings, never in the code):
- `APIFY_TOKEN`: your Apify API token
- `APP_PASSWORD`: optional access password for the site
- `CACHE_SECONDS`: optional, default 3600
