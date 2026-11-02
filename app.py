"""Instagram public-profile reel tracker (Apify backend).

Run:  set APIFY_TOKEN, then  python app.py  and open http://localhost:5000
"""
import csv
import hmac
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

BASE = Path(__file__).parent
HISTORY = BASE / "history.csv"
API = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
REEL_ACTOR = "apify~instagram-reel-scraper"
PROFILE_ACTOR = "apify~instagram-profile-scraper"

app = Flask(__name__, static_folder="static")


class ApifyError(Exception):
    pass


def run_actor(actor, payload):
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise ApifyError("APIFY_TOKEN is not set. Set it in PowerShell and restart the app.")
    r = requests.post(API.format(actor=actor), params={"token": token, "timeout": 240},
                      json=payload, timeout=270)
    if r.status_code == 401:
        raise ApifyError("Apify rejected the token (401). Check APIFY_TOKEN.")
    if r.status_code == 402:
        raise ApifyError("Apify credit is used up for this month (402).")
    if not r.ok:
        raise ApifyError(f"Apify error {r.status_code}: {r.text[:300]}")
    return r.json()


def parse_username(value: str) -> str:
    value = value.strip()
    m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", value)
    name = m.group(1) if m else value.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", name):
        raise ValueError("Could not read an Instagram username from that link.")
    return name


def pick(d, *keys, default=None):
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return default


def engagement(likes, comments, followers):
    if followers and likes is not None:
        return round((likes + (comments or 0)) / followers * 100, 2)
    return None


APP_PASSWORD = os.environ.get("APP_PASSWORD")  # optional: protects a public deployment
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "3600"))
_cache = {}


def authorized():
    if not APP_PASSWORD:
        return True
    supplied = request.headers.get("X-Access-Key") or request.args.get("key", "")
    return hmac.compare_digest(supplied, APP_PASSWORD)


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def config():
    return jsonify(protected=bool(APP_PASSWORD))


@app.post("/api/scrape")
def scrape():
    if not authorized():
        return jsonify(error="Wrong or missing access password."), 401
    body = request.get_json(force=True, silent=True) or {}
    try:
        username = parse_username(body.get("url", ""))
        days = max(1, min(int(body.get("days", 7)), 90))
    except (ValueError, TypeError) as e:
        return jsonify(error=str(e)), 400

    key = (username.lower(), days)
    hit = _cache.get(key)
    if hit and not body.get("fresh") and time.time() - hit[0] < CACHE_SECONDS:
        return jsonify({**hit[1], "cached": True})

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            reel_f = pool.submit(run_actor, REEL_ACTOR, {
                "username": [username],
                "resultsLimit": 100,
                "onlyPostsNewerThan": f"{days} days",
            })
            prof_f = pool.submit(run_actor, PROFILE_ACTOR, {"usernames": [username]})
            items = reel_f.result()
            try:
                prof = (prof_f.result() or [{}])[0]
            except ApifyError:
                prof = {}
    except ApifyError as e:
        return jsonify(error=str(e)), 502
    except requests.RequestException as e:
        return jsonify(error=f"Could not reach Apify: {e}"), 502

    if prof.get("private"):
        return jsonify(error=f"@{username} is private - only public profiles can be read."), 403

    followers = pick(prof, "followersCount")
    reels = []
    for it in items:
        if it.get("error") or not (it.get("shortCode") or it.get("url")):
            continue
        likes = pick(it, "likesCount")
        likes = None if likes is None or likes < 0 else likes
        comments = pick(it, "commentsCount", default=0)
        views = pick(it, "videoPlayCount", "videoViewCount")
        code = it.get("shortCode") or ""
        reels.append({
            "shortcode": code,
            "url": it.get("url") or f"https://www.instagram.com/reel/{code}/",
            "posted": str(pick(it, "timestamp", default="")),
            "caption": (it.get("caption") or "")[:200],
            "duration": round(float(pick(it, "videoDuration", default=0) or 0), 1),
            "views": views,
            "likes": likes,
            "comments": comments,
            "engagement_rate": engagement(likes, comments, followers),
            "view_rate": round(views / followers * 100, 1) if views and followers else None,
        })

    collected = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _append_history(username, followers, reels, collected)
    reels.sort(key=lambda r: r["posted"], reverse=True)

    first = items[0] if items else {}
    result = dict(
        profile={
            "username": username,
            "full_name": pick(prof, "fullName") or first.get("ownerFullName") or "",
            "followers": followers,
            "following": pick(prof, "followsCount"),
            "posts": pick(prof, "postsCount"),
            "bio": pick(prof, "biography", default=""),
            "avatar": pick(prof, "profilePicUrlHD", "profilePicUrl", default=""),
            "verified": bool(prof.get("verified")),
        },
        days=days,
        collected_on=collected,
        reels=reels,
    )
    _cache[key] = (time.time(), result)
    return jsonify(result)


def _append_history(username, followers, reels, collected):
    new = not HISTORY.exists()
    with HISTORY.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["collected_on", "username", "followers", "shortcode", "posted",
                        "duration_s", "views", "likes", "comments", "engagement_rate_pct"])
        for r in reels:
            w.writerow([collected, username, followers, r["shortcode"], r["posted"],
                        r["duration"], r["views"], r["likes"], r["comments"], r["engagement_rate"]])


@app.get("/api/history.csv")
def history():
    if not authorized():
        return Response("Unauthorized", status=401)
    if not HISTORY.exists():
        return Response("collected_on,username\n", mimetype="text/csv")
    return Response(HISTORY.read_text(encoding="utf-8"), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=history.csv"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)
