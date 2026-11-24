#!/usr/bin/env python3
"""Manually refresh an archived Iranian laws collection from public Ekhtebar pages.

ONLY invoked on user-requested GitHub workflow_dispatch. This does not claim that
these texts are official, complete, amended, or currently in force.
No challenge bypass, proxies, schedules or external OCR services.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # imports the fork's existing site scrapers, not copied code

DATA = ROOT / "data"
INDEX = DATA / "index.json"
LAW_DIR = DATA / "laws"
REPORT = DATA / "manual-refresh-report.json"
SOURCE = "https://www.ekhtebar.ir/قوانین/"
ROBOTS = "https://www.ekhtebar.ir/robots.txt"
UA = "Iran-Laws-Manual-Refresh/1.0"
ALLOWED_HOSTS = {"www.ekhtebar.ir", "ekhtebar.ir"}


def atomic_write(path: Path, content: str) -> bool:
    """Keep existing file unchanged unless its content differs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    tmp = path.with_name(path.name + ".manual-refresh-temp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return True


def approved_url(url: str) -> str:
    p = urlsplit(url)
    if p.scheme != "https" or p.hostname not in ALLOWED_HOSTS or p.username or p.password:
        raise ValueError("Source URL must be public HTTPS ekhtebar.ir")
    return url.split("#", 1)[0]


def check_robots() -> None:
    rp = RobotFileParser()
    rp.set_url(ROBOTS)
    rp.read()
    if not rp.can_fetch(UA, SOURCE):
        raise RuntimeError("Site robots rules do not allow reading the laws index")


def verify_index(items: list, prior: list) -> None:
    if not isinstance(items, list) or len(items) < max(250, int(len(prior) * 0.7)):
        raise RuntimeError("Suspiciously small or invalid laws index")
    urls = set()
    for item in items:
        if not isinstance(item, dict) or not item.get("title", "").strip():
            raise RuntimeError("Index row has no law title")
        if item.get("url"):
            url = approved_url(item["url"])
            if url in urls:
                raise RuntimeError("Duplicate URL in refreshed index")
            urls.add(url)
    if len(urls) < 200:
        raise RuntimeError("Index lacks enough actual law URLs")


def fetch_markdown(item: dict) -> str:
    url = approved_url(item["url"])
    _, text = server.fetch_law_markdown(url)
    text = text.strip()
    low = text.casefold()
    if (len(text) < 120
        or "__arcsjs" in low
        or "checking your browser" in low
        or "transferring to the website" in low
        or "در حال انتقال به سایت" in low
        or sum("\u0600" <= ch <= "\u06ff" for ch in text) < 65):
        raise RuntimeError("Page is empty, challenge text, or not a law")
    return f"# {item['title']}\n\n> منبع: {url}\n\n{text}\n"


def run(mode: str, delay: float) -> int:
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    previous = json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else []
    old_urls = {x["url"]: x for x in previous if x.get("url")}
    check_robots()
    items = server.scrape_index()
    verify_index(items, previous)
    urls = {x["url"]: x for x in items if x.get("url")}
    added = set(urls) - set(old_urls)
    removed = set(old_urls) - set(urls)

    if mode == "index":
        candidates = []
    elif mode == "missing":
        candidates = [
            row for row in items if row.get("url") and (
                row["url"] in added or
                not (LAW_DIR / (server.slugify(row["url"]) + ".md")).exists()
            )
        ]
    else:
        candidates = items

    checked = changed = skipped = 0
    failures = []
    for position, item in enumerate(candidates, 1):
        url = item.get("url", "")
        if not url:
            skipped += 1
            continue  # PDF-only entry: no unrelated OCR APIs.
        try:
            filename = server.slugify(url) + ".md"
            target = LAW_DIR / filename
            if target.parent != LAW_DIR or len(filename) > 180:
                raise ValueError("Unsafe output name")
            markdown = fetch_markdown(item)
            checked += 1
            if atomic_write(target, markdown):
                changed += 1
        except Exception as exc:
            failures.append({"title": item.get("title", "")[:120], "error": str(exc)[:260]})
        if position % 25 == 0:
            print(f"Progress {position}/{len(candidates)}: changed={changed}, errors={len(failures)}", flush=True)
        if position < len(candidates):
            time.sleep(delay)

    index_text = json.dumps(items, ensure_ascii=False, indent=1) + "\n"
    index_changed = atomic_write(INDEX, index_text)
    status = "partial" if failures else ("index_only" if mode == "index" else "success")
    earlier = json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else {}
    completed = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = {
        "status": status,
        "manual_trigger": True,
        "mode": mode,
        "attempted_at_utc": started,
        "completed_at_utc": completed,
        "last_successful_full_refresh_utc": (
            completed if status == "success" and mode == "full"
            else earlier.get("last_successful_full_refresh_utc")
        ),
        "source": SOURCE,
        "index_count": len(items),
        "new_urls": len(added),
        "removed_urls": len(removed),
        "changed_metadata": sum(1 for url, row in urls.items() if url in old_urls and old_urls[url] != row),
        "index_changed": index_changed,
        "index_sha256": hashlib.sha256(index_text.encode("utf-8")).hexdigest(),
        "texts_checked": checked,
        "texts_changed": changed,
        "pdf_only_skipped": skipped,
        "failures_count": len(failures),
        "failures": failures[:100],
        "disclaimer": "Archive freshness does not certify legal validity, amendment or repeal status."
    }
    atomic_write(REPORT, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k:v for k,v in report.items() if k != "failures"}, ensure_ascii=False, indent=2), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["full", "missing", "index"], default="full")
    parser.add_argument("--delay", type=float, default=1.5)
    args = parser.parse_args()
    if not 1 <= args.delay <= 15:
        parser.error("delay must be between 1 and 15 seconds")
    try:
        raise SystemExit(run(args.mode, args.delay))
    except Exception as exc:
        # Fail closed: never claim that an index challenged by a security
        # intermediary is a successful laws update.
        previous = {}
        if REPORT.exists():
            try:
                previous = json.loads(REPORT.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                previous = {}
        try:
            count = len(json.loads(INDEX.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            count = None
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        summary = {
            "status": "failed",
            "manual_trigger": True,
            "mode": args.mode,
            "attempted_at_utc": now,
            "completed_at_utc": now,
            "source": SOURCE,
            "index_count_before_attempt": count,
            "index_changed": False,
            "texts_changed": 0,
            "last_successful_full_refresh_utc": previous.get("last_successful_full_refresh_utc"),
            "error": str(exc)[:400],
            "note": "No existing archive data was overwritten. Source may be returning a security challenge.",
            "disclaimer": "No legal freshness or current validity confirmed."
        }
        atomic_write(REPORT, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        print("MANUAL_REFRESH_FAILED", summary["error"], file=sys.stderr, flush=True)
        raise

