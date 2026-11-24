#!/usr/bin/env python3
"""One-shot manual refresh on VPS; no scheduler. Supports resuming same manual run."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from threading import Lock

sys.path.insert(0,str(Path(__file__).resolve().parent))
import manual_refresh as m

ROOT = Path(__file__).resolve().parents[1]
LAW_DIR = ROOT / "data" / "laws"
REPORT = ROOT / "data" / "manual-refresh-report.json"
INDEX = ROOT / "data" / "index.json"
progress_lock = Lock()

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def download(item: dict, index_number: int, resume: bool, delay: float) -> dict:
    url = item.get("url","")
    if not url:
        return {"status":"skip_pdf", "index":index_number}
    try:
        safe = m.approved_url(url)
        filename = m.server.slugify(safe)+".md"
        if not filename or "/" in filename or ".." in filename:
            raise ValueError("Unsafe law file name")
        file = LAW_DIR / filename
        if resume and file.is_file():
            body = file.read_text(encoding="utf-8")
            if len(body) > 120 and f"> منبع: {safe}" in body[:3000] and sum("\u0600" <= char <= "\u06ff" for char in body) >= 65:
                return {"status":"resumed","index":index_number}
        error=None
        for retry in range(3):
            try:
                body=m.fetch_markdown(item).rstrip("\n")
                m.atomic_write(file,body)
                return {"status":"fetched","index":index_number}
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                if retry < 2:
                    time.sleep(2*(retry+1))
        raise RuntimeError(error or "Unable to fetch text")
    except Exception as exc:
        return {"status":"error","index":index_number,"title":item.get("title","")[:160],"error":str(exc)[:300]}
    finally:
        time.sleep(delay)

def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--resume",action="store_true",help="Use texts already fetched in this same manual batch")
    parser.add_argument("--workers",type=int,default=3)
    parser.add_argument("--delay",type=float,default=0.8)
    opts=parser.parse_args()
    if not (1 <= opts.workers <= 3 and 0.8 <= opts.delay <= 10):
        parser.error("workers: 1..3, delay: 0.8..10 seconds")
    started=now()
    if not INDEX.is_file():
        raise RuntimeError("Initial index missing; refusing blind refresh")
    prior=json.loads(INDEX.read_text(encoding="utf-8"))
    m.check_robots()
    items=m.server.scrape_index()
    m.verify_index(items,prior)
    targets=[row for row in items if row.get("url")]
    filenames=[m.server.slugify(m.approved_url(row["url"]))+".md" for row in targets]
    if len(set(filenames)) != len(filenames):
        raise RuntimeError("Law filename collision; stop to protect documents")
    print(f"START index={len(items)} law_pages={len(targets)} workers={opts.workers} resume={opts.resume}",flush=True)
    LAW_DIR.mkdir(parents=True,exist_ok=True)
    counters={"fetched":0,"resumed":0,"skip_pdf":0,"error":0}
    failures=[]
    with ThreadPoolExecutor(max_workers=opts.workers) as pool:
        futures=[pool.submit(download,row,i,opts.resume,opts.delay) for i,row in enumerate(items,1)]
        for count,future in enumerate(as_completed(futures),1):
            result=future.result()
            counters[result["status"]]+=1
            if result["status"]=="error":
                failures.append(result)
                print(f"ERROR {result['index']}: {result['title']} :: {result['error']}",flush=True)
            if count%25==0:
                print(f"Progress {count}/{len(items)} fetched={counters['fetched']} resumed={counters['resumed']} errors={counters['error']}",flush=True)
    invalid=[]
    for row in targets:
        filename=m.server.slugify(row["url"])+".md"
        file=LAW_DIR/filename
        try:
            body=file.read_text(encoding="utf-8")
            if len(body) < 120 or f"> منبع: {m.approved_url(row['url'])}" not in body[:3000]:
                invalid.append(filename[:120])
        except (OSError,UnicodeError):
            invalid.append(filename[:120])
    count_invalid=len(invalid)
    allgood=not failures and count_invalid==0
    oldurl={x.get("url"):x for x in prior if x.get("url")}
    newurl={x.get("url"):x for x in items if x.get("url")}
    index_text=json.dumps(items,ensure_ascii=False,indent=1)
    index_changed=m.atomic_write(INDEX,index_text)
    completed=now()
    report={
      "status":"success" if allgood else "partial",
      "manual_trigger":True,
      "mode":"full",
      "attempted_at_utc":started,
      "completed_at_utc":completed,
      "last_successful_full_refresh_utc":completed if allgood else None,
      "source":m.SOURCE,
      "index_count":len(items),
      "new_urls":len(set(newurl)-set(oldurl)),
      "removed_urls":len(set(oldurl)-set(newurl)),
      "changed_metadata":sum(1 for u,r in newurl.items() if u in oldurl and r!=oldurl[u]),
      "index_changed":index_changed,
      "texts_checked":len(targets)-count_invalid,
      "texts_staged_or_updated":counters["fetched"],
      "texts_resumed":counters["resumed"],
      "pdf_only_skipped":counters["skip_pdf"],
      "failures_count":len(failures)+count_invalid,
      "failures":failures[:100],
      "invalid_files":invalid[:100],
      "disclaimer":"Archived Ekhtebar pages, not proof of legal validity or current amendments."
    }
    m.atomic_write(REPORT,json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print("FINAL_REPORT "+json.dumps({k:v for k,v in report.items() if k not in ("failures","invalid_files")},ensure_ascii=False),flush=True)
    return 0 if allgood else 1

if __name__=="__main__":
    sys.exit(main())
