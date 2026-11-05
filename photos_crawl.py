#!/usr/bin/env python3
"""photos_crawl.py — incremental Google Photos timeline crawl for the
Muse File Search index.

Runs under the Google Photos connector's venv python (it needs the `mcp`
package) and holds ONE MCP session open for the whole crawl — the
connector's pagination cursors live only inside a single server process,
so page-by-page CLI calls cannot walk the timeline.

Strategy (mirrors the email incremental design in build_index.py):
  * Fresh phase: page the library newest-first, adding items whose IDs
    are not in the existing index, until a full page is already known,
    the fresh cap is hit, or the timeline ends.
  * Backfill phase: if the library is not fully covered yet, page the
    window ending at the oldest indexed taken date, adding older items
    until the backfill cap is hit or the timeline ends (which marks the
    crawl complete in the state file).
Only item metadata is indexed (filename, description, place, taken date,
type, the photos.google.com link) — never image data.

Writes the merged index atomically (tmp + replace) and prints one JSON
stats line to stdout. On any hard failure it exits non-zero and leaves
the previous index untouched.
"""
import argparse, asyncio, json, os, sys, time
from pathlib import Path

PAGE_SIZE = 50
MAX_PAGES_PER_PHASE = 80


def load_jsonl(path):
    out = []
    if path and os.path.exists(path):
        with open(path, errors="replace") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def to_record(item):
    name = item.get("filename") or item.get("description") or "(untitled photo)"
    path_bits = ["Google Photos"]
    if item.get("place"):
        path_bits.append(item["place"])
    if item.get("description"):
        path_bits.append(item["description"])
    return {
        "surface": "photos",
        "name": name,
        "path": " · ".join(path_bits),
        "size": None,
        "modified": item.get("taken_at"),
        "id": item.get("id"),
        "link": item.get("url"),
        "description": item.get("description"),
        "place": item.get("place"),
        "media_type": item.get("type"),
        "favourite": item.get("favourite"),
        "archived": item.get("archived"),
    }


class PhotosSession:
    def __init__(self, connector_dir):
        self.connector_dir = Path(connector_dir)
        self.server = self.connector_dir / "venv" / "bin" / "photos-mcp"
        self.session_dir = self.connector_dir / "session"
        self.state_dir = self.connector_dir / "state"
        account = None
        cfg = self.connector_dir / "config.json"
        if cfg.exists():
            try:
                account = json.loads(cfg.read_text())["account"]
            except Exception:
                account = None
        if not account:
            meta = json.loads((self.session_dir / "meta.json").read_text())
            account = meta["email"]
        self.account = account
        self._session = None

    async def __aenter__(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        env = dict(os.environ)
        env.update({
            "GOOGLE_PHOTOS_EXPECTED_EMAIL": self.account,
            "PHOTOS_SESSION_DIR": str(self.session_dir),
            "PHOTOS_STATE_DIR": str(self.state_dir),
            "PHOTOS_BUDGET_TZ": "America/New_York",
        })
        params = StdioServerParameters(command=str(self.server),
                                       args=["--transport", "stdio"], env=env)
        self._cm = stdio_client(params)
        read, write = await self._cm.__aenter__()
        self._session_cm = ClientSession(read, write)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc):
        await self._session_cm.__aexit__(*exc)
        await self._cm.__aexit__(*exc)

    async def list_page(self, cursor=None, to_date=None):
        args = {"source": "library", "limit": PAGE_SIZE, "media": "all"}
        if cursor:
            args["cursor"] = cursor
        if to_date:
            args["to_date"] = to_date
        last_err = None
        for attempt in range(3):
            res = await self._session.call_tool("photos_list", args)
            data = res.structured_content
            if data is None:
                txt = "".join(getattr(b, "text", "") for b in res.content or [])
                try:
                    data = json.loads(txt)
                except Exception:
                    data = {"error": {"code": "unparseable_result"}}
            if not res.is_error and "items" in data:
                return data
            code = ((data or {}).get("error") or {}).get("code", "tool_error")
            last_err = code
            if code in ("account_mismatch", "blocked"):
                raise RuntimeError(f"photos_list aborted: {code}")
            await asyncio.sleep(5 * (attempt + 1))
        raise RuntimeError(f"photos_list failed after retries: {last_err}")


async def crawl(connector_dir, index_path, state_path, fresh_cap, backfill_cap):
    old_records = load_jsonl(index_path)
    by_id = {r["id"]: r for r in old_records if r.get("id")}
    state = {}
    if os.path.exists(state_path):
        try:
            state = json.load(open(state_path))
        except Exception:
            state = {}
    complete = bool(state.get("complete"))
    stats = {"added_fresh": 0, "added_backfill": 0, "pages": 0}

    async with PhotosSession(connector_dir) as sess:
        # ---- fresh phase: newest-first until known territory ----
        cursor = None
        for _ in range(MAX_PAGES_PER_PHASE):
            if stats["added_fresh"] >= fresh_cap:
                break
            page = await sess.list_page(cursor=cursor)
            stats["pages"] += 1
            items = page.get("items", [])
            new_here = 0
            for item in items:
                rid = item.get("id")
                if rid and rid not in by_id:
                    by_id[rid] = to_record(item)
                    stats["added_fresh"] += 1
                    new_here += 1
            cursor = page.get("next_cursor")
            if not cursor:
                complete = True
                break
            if items and new_here == 0:
                break  # reached previously crawled territory

        # ---- backfill phase: extend coverage backwards ----
        if not complete and by_id and backfill_cap > 0:
            dates = sorted(r["modified"] for r in by_id.values() if r.get("modified"))
            if dates:
                to_date = dates[0][:10]
                cursor = None
                for _ in range(MAX_PAGES_PER_PHASE):
                    if stats["added_backfill"] >= backfill_cap:
                        break
                    page = await sess.list_page(cursor=cursor, to_date=to_date)
                    stats["pages"] += 1
                    items = page.get("items", [])
                    for item in items:
                        rid = item.get("id")
                        if rid and rid not in by_id:
                            by_id[rid] = to_record(item)
                            stats["added_backfill"] += 1
                    cursor = page.get("next_cursor")
                    if not cursor:
                        complete = True
                        break

    records = sorted(by_id.values(),
                     key=lambda r: r.get("modified") or "", reverse=True)
    tmp = index_path + ".tmp"
    with open(tmp, "w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, index_path)
    dates = [r["modified"] for r in records if r.get("modified")]
    new_state = {"complete": complete,
                 "count": len(records),
                 "earliest": min(dates) if dates else None,
                 "latest": max(dates) if dates else None,
                 "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with open(state_path, "w") as fh:
        json.dump(new_state, fh, indent=2)
    return {**new_state, **stats}


def main():
    ap = argparse.ArgumentParser(description="Incremental Google Photos crawl for Muse File Search.")
    ap.add_argument("--connector-dir", required=True)
    ap.add_argument("--index", required=True, help="photos.jsonl path (read + atomically rewritten)")
    ap.add_argument("--state", required=True, help="crawl state JSON path")
    ap.add_argument("--fresh-cap", type=int, default=1000)
    ap.add_argument("--backfill-cap", type=int, default=1000)
    a = ap.parse_args()
    stats = asyncio.run(crawl(a.connector_dir, a.index, a.state,
                              a.fresh_cap, a.backfill_cap))
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
