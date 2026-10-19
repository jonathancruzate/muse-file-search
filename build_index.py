#!/usr/bin/env python3
"""build_index.py — build the cached JSONL indexes for the universal search.

Surfaces: local files, Google Drive (rclone), OneDrive (connector inventory),
Dropbox (connector), Google Keep (titles only), email headers (Gmail /
Outlook / agent mailbox). Read-only against every cloud surface.
Email is HEADERS/METADATA ONLY — no message bodies are ever indexed.
Zero-token: deterministic script, no model calls.
"""
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "hidden_files")
os.makedirs(OUT, exist_ok=True)

RCLONE = os.path.expanduser("~/.local/bin/rclone")
DROPBOX = "/opt/hatch/bin/dropbox"
GWS = "/opt/hatch/bin/hatch_gws_cli"
OUTLOOK = "/opt/hatch/bin/outlook-mail"
KEEP = os.path.expanduser("~/workspace/skills/google-keep/bin/keep")
ONEDRIVE_CLI = os.path.expanduser("~/workspace/skills/onedrive/bin/onedrive.py")
AGENT_MAIL = os.path.expanduser("~/workspace/custom-skills/local-agent-mail/bin/agent-mail")
OD_INV_DIR = os.path.expanduser("~/workspace/onedrive-cleanup/hidden_files")
LOCAL_ROOTS = [os.path.expanduser(p) for p in
               ("~/workspace", "~/memory", "~/dreams", "~/hooks")]
SKIP_DIRS = {".git", "node_modules", "__pycache__"}

GMAIL_BACKFILL_MAX = 1500
GMAIL_PAGE = 500  # +triage appears to cap a single call at ~500 messages
GMAIL_ENRICH_GETS = 60


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def run(cmd, timeout=600):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def parse_date(s):
    """Any date string -> ISO UTC string, or None."""
    if not s:
        return None
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s / 1000 if s > 1e12 else s,
                                      timezone.utc).isoformat()
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(str(s))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def write_jsonl(name, records):
    tmp = os.path.join(OUT, name + ".tmp")
    with open(tmp, "w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, os.path.join(OUT, name))


def read_jsonl(name):
    path = os.path.join(OUT, name)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def rec(surface, name, path, size=None, modified=None, rid=None, link=None, **kw):
    r = {"surface": surface, "name": name, "path": path, "size": size,
         "modified": modified, "id": rid, "link": link}
    r.update(kw)
    return r


# ---------------- local ----------------

def build_local():
    records = []
    for root in LOCAL_ROOTS:
        if not os.path.isdir(root):
            continue
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if d not in SKIP_DIRS]
            if os.path.abspath(dp).startswith(os.path.join(HERE, "")) or \
               os.path.abspath(dp) == HERE:
                dn[:] = []
                continue
            for f in fn:
                p = os.path.join(dp, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                records.append(rec(
                    "local", f, p, st.st_size,
                    datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()))
    return records, {}


# ---------------- drive ----------------

def build_drive():
    rc, out, err = run([RCLONE, "lsjson", "--recursive", "gdrive:"], timeout=1500)
    note = {}
    if rc == 0 and out.strip():
        entries = json.loads(out)
        records = []
        for e in entries:
            if e.get("IsDir"):
                continue
            fid = e.get("ID") or None
            size = e.get("Size")
            records.append(rec(
                "drive", e.get("Name") or os.path.basename(e.get("Path", "")),
                e.get("Path", ""), size if isinstance(size, int) and size >= 0 else None, parse_date(e.get("ModTime")),
                fid, f"https://drive.google.com/file/d/{fid}/view" if fid else None))
        return records, note
    # fallback: lsf with pipe separator
    note["fallback"] = "lsjson failed; used lsf"
    rc, out, err = run([RCLONE, "lsf", "--recursive", "--files-only",
                        "--format", "psti", "--separator", "|", "gdrive:"],
                       timeout=1500)
    if rc != 0:
        raise RuntimeError(f"rclone lsjson and lsf both failed: {err[:200]}")
    records = []
    for line in out.splitlines():
        parts = line.rsplit("|", 3)
        if len(parts) < 4:
            continue
        path, size, mod, fid = parts
        records.append(rec(
            "drive", os.path.basename(path), path,
            int(size) if size.lstrip("-").isdigit() else None,
            parse_date(mod), fid or None,
            f"https://drive.google.com/file/d/{fid}/view" if fid else None))
    return records, note


# ---------------- onedrive ----------------

def build_onedrive():
    cands = []
    if os.path.isdir(OD_INV_DIR):
        for f in os.listdir(OD_INV_DIR):
            if f.startswith("inventory") and f.endswith(".json"):
                cands.append(os.path.join(OD_INV_DIR, f))
    if not cands:
        raise RuntimeError("no OneDrive inventory JSON found")
    src = max(cands, key=os.path.getmtime)
    d = json.load(open(src))
    records = []
    for f in d.get("files", []):
        records.append(rec("onedrive", f.get("name") or os.path.basename(f.get("path", "")),
                           f.get("path", ""), f.get("size"),
                           parse_date(f.get("modified")), f.get("id")))
    return records, {"source": src,
                     "source_mtime": datetime.fromtimestamp(
                         os.path.getmtime(src), timezone.utc).isoformat()}


# ---------------- dropbox ----------------

def dropbox_parse(stdout):
    d = json.loads(stdout)
    res = d.get("result", d)
    sc = res.get("structuredContent")
    if sc:
        return sc
    txt = res["content"][0]["text"]
    return json.loads(txt)


def build_dropbox():
    records = []
    args = {"path": "", "recursive": True, "max_results": 600,
            "object_types": ["file"]}
    pages = 0
    while True:
        rc, out, err = run([DROPBOX, "call-tool", "--name", "list_folder",
                            "--arguments-json", json.dumps(args)], timeout=300)
        if rc != 0:
            raise RuntimeError(f"dropbox list_folder failed: {err[:200] or out[:200]}")
        page = dropbox_parse(out)
        for e in page.get("entries", []):
            if e.get("object_type") not in (None, "file"):
                continue
            path = re.sub(r"^ns:\d+//", "/", e.get("path", ""))
            records.append(rec("dropbox", e.get("name", ""), path,
                               (e.get("file") or {}).get("size"),
                               parse_date(e.get("modified_time")),
                               e.get("file_id")))
        pages += 1
        if not page.get("has_more") or pages >= 100:
            break
        args = {"cursor": page["cursor"]}
    return records, {"pages": pages}


# ---------------- keep ----------------

def build_keep():
    rc, out, err = run([KEEP, "list"], timeout=300)
    if rc != 0:
        raise RuntimeError(f"keep list failed: {err[:200]}")
    notes = json.loads(out)
    records = []
    for n in notes:
        if n.get("trashed"):
            continue
        title = (n.get("title") or "").strip()
        if not title:
            body = (n.get("text") or "").strip().splitlines()
            title = body[0].strip() if body else "(untitled note)"
        records.append(rec("keep", title, "Google Keep", None,
                           parse_date(n.get("updated")), n.get("id")))
    return records, {}


# ---------------- email ----------------

def fmt_addr(v):
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        ea = v.get("emailAddress") or v
        name, addr = ea.get("name"), ea.get("address") or ea.get("email")
        if name and addr:
            return f"{name} <{addr}>"
        return addr or name
    if isinstance(v, list):
        return ", ".join(filter(None, (fmt_addr(x) for x in v)))
    return str(v)


def gmail_triage(query, maxn, timeout):
    cmd = [GWS, "gmail", "+triage", "--max", str(maxn), "--format", "json"]
    if query:
        cmd += ["--query", query]
    rc, out, err = run(cmd, timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"gmail +triage failed: {err[:200]}")
    return json.loads(out).get("messages", [])


def gmail_records(msgs):
    out = []
    for m in msgs:
        sent = (m.get("message_sent_at") or {}).get("utc") or m.get("date")
        out.append(rec("email", m.get("subject") or "(no subject)",
                       "Gmail", None, parse_date(sent), m.get("id"),
                       f"https://mail.google.com/mail/u/0/#inbox/{m.get('id')}",
                       mailbox="gmail", **{"from": m.get("from")}, to=None,
                       snippet=None, labels=None, has_attachments=False,
                       attachments=[]))
    return out


def gmail_enrich(records):
    """Mark has_attachments from a has:attachment list; fetch attachment
    filenames (+to/snippet/labels) for the newest GMAIL_ENRICH_GETS."""
    info = {"enriched": 0}
    if not records:
        return info
    dates = sorted(r["modified"] for r in records if r.get("modified"))
    earliest = dates[0][:10].replace("-", "/") if dates else "2000/01/01"
    rc, out, err = run([GWS, "gmail", "users", "messages", "list", "--params",
                        json.dumps({"userId": "me",
                                    "q": f"has:attachment after:{earliest}",
                                    "maxResults": 500}),
                        "--page-all", "--page-limit", "4", "--format", "json"],
                       timeout=900)
    if rc != 0:
        info["enrich_error"] = err[:150]
        return info
    att_ids = {m["id"] for m in json.loads(out).get("messages", [])}
    by_id = {r["id"]: r for r in records}
    targets = [r for r in records if r["id"] in att_ids]
    for r in targets:
        r["has_attachments"] = True
    targets.sort(key=lambda r: r.get("modified") or "", reverse=True)
    # Skip records already enriched by an earlier run (keeps refreshes fast).
    todo = [r for r in targets if not r.get("attachments") and not r.get("snippet")]
    for r in todo[:GMAIL_ENRICH_GETS]:
        try:
            rc, out, err = run([GWS, "gmail", "users", "messages", "get",
                                "--params", json.dumps({"userId": "me",
                                                        "id": r["id"],
                                                        "format": "full"}),
                                "--format", "json"], timeout=120)
            if rc != 0:
                continue
            msg = json.loads(out)
            names = []

            def walk(part):
                if part.get("filename"):
                    names.append(part["filename"])
                for sub in part.get("parts") or []:
                    walk(sub)
            walk(msg.get("payload") or {})
            hdrs = {h["name"].lower(): h["value"] for h in
                    (msg.get("payload") or {}).get("headers", [])}
            r["attachments"] = names
            r["to"] = hdrs.get("to")
            r["snippet"] = msg.get("snippet")
            r["labels"] = msg.get("labelIds")
            info["enriched"] += 1
        except Exception:
            continue
    return info


def build_gmail(old_records, watermark):
    old = [r for r in old_records if r.get("mailbox") == "gmail"]
    if watermark:
        wd = (datetime.fromisoformat(watermark) - timedelta(days=1))
        q = f"after:{wd.strftime('%Y/%m/%d')}"
        msgs = gmail_triage(q, 1000, 1200)
        new = gmail_records(msgs)
        merged = {r["id"]: r for r in old}
        for r in new:
            merged.setdefault(r["id"], r)
        records = list(merged.values())
        mode = "incremental"
    else:
        # Backfill: page backwards with before: queries (+triage caps each
        # call at ~500 messages) until GMAIL_BACKFILL_MAX or a short page.
        records, seen, before = [], set(), None
        while len(records) < GMAIL_BACKFILL_MAX:
            q = "after:2000/01/01" + (f" before:{before}" if before else "")
            try:
                msgs = gmail_triage(q, GMAIL_PAGE, 1500)
            except Exception:
                if records:
                    break
                msgs = gmail_triage("after:2000/01/01", 300, 700)
            page = [r for r in gmail_records(msgs) if r["id"] not in seen]
            for r in page:
                seen.add(r["id"])
            records.extend(page)
            dates = sorted(r["modified"] for r in page if r.get("modified"))
            if len(msgs) < GMAIL_PAGE or not dates:
                break
            before = dates[0][:10].replace("-", "/")
        mode = "backfill"
    info = gmail_enrich(records)
    info["mode"] = mode
    return records, info


def build_outlook(old_records, watermark):
    old_ids = {r["id"] for r in old_records if r.get("mailbox") == "outlook"}
    records = [r for r in old_records if r.get("mailbox") == "outlook"]
    new_count = 0
    for folder in ("inbox", "sentitems"):
        token = None
        for _ in range(30):
            cmd = [OUTLOOK, "list", "--folder", folder, "--page-size", "100"]
            if token:
                cmd += ["--page-token", token]
            rc, out, err = run(cmd, timeout=300)
            if rc != 0:
                raise RuntimeError(f"outlook list {folder} failed: {err[:200]}")
            d = json.loads(out)
            stop = False
            for m in d.get("messages", []):
                if m.get("id") in old_ids:
                    stop = True
                    continue
                mod = parse_date((m.get("message_received_at") or {}).get("utc")
                                 or m.get("date"))
                records.append(rec(
                    "email", m.get("subject") or "(no subject)",
                    f"Outlook/{folder}", None, mod, m.get("id"), None,
                    mailbox="outlook", **{"from": fmt_addr(m.get("from"))},
                    to=fmt_addr(m.get("to")), snippet=m.get("preview"),
                    labels=[folder],
                    has_attachments=bool(m.get("has_attachments")),
                    attachments=[]))
                new_count += 1
            token = d.get("next_page_token")
            if stop or not token:
                break
    return records, {"new": new_count}


def build_agentmail(old_records):
    rc, out, err = run(["python3", AGENT_MAIL, "inbox", "--limit", "1000",
                        "--json"], timeout=300)
    if rc != 0:
        raise RuntimeError(f"agent-mail inbox failed: {err[:200]}")
    msgs = json.loads(out)
    records = []
    for m in msgs:
        records.append(rec("email", m.get("subject") or "(no subject)",
                           "Agent mailbox INBOX", None, parse_date(m.get("date")),
                           f"agentmail:{m.get('uid')}", None,
                           mailbox="agent", **{"from": m.get("from")}, to=None,
                           snippet=None, labels=["INBOX"],
                           has_attachments=False, attachments=[]))
    return records, {"note": "INBOX only (CLI surface)"}


def build_email(old_manifest):
    old_records = read_jsonl("email.jsonl")
    wm = ((old_manifest.get("surfaces", {}).get("email", {})
           .get("mailboxes", {})))
    all_records, mailboxes, unavailable = [], {}, []
    builders = (("gmail", lambda: build_gmail(
                     old_records, (wm.get("gmail") or {}).get("watermark"))),
                ("outlook", lambda: build_outlook(
                     old_records, (wm.get("outlook") or {}).get("watermark"))),
                ("agent", lambda: build_agentmail(old_records)))
    for name, fn in builders:
        try:
            recs_, info = fn()
            all_records.extend(recs_)
            dates = sorted(r["modified"] for r in recs_ if r.get("modified"))
            mailboxes[name] = {
                "count": len(recs_),
                "earliest": dates[0] if dates else None,
                "latest": dates[-1] if dates else None,
                "watermark": dates[-1][:10] if dates else None,
                **info}
        except Exception as e:
            unavailable.append({"surface": f"email/{name}", "reason": str(e)[:300]})
            kept = [r for r in old_records if r.get("mailbox") == name]
            all_records.extend(kept)
            if kept:
                mailboxes[name] = {"count": len(kept),
                                   "note": "kept previous cache; refresh failed"}
    all_records.sort(key=lambda r: r.get("modified") or "", reverse=True)
    return all_records, {"mailboxes": mailboxes}, unavailable


# ---------------- main ----------------

def main():
    old_manifest = {}
    mf = os.path.join(OUT, "manifest.json")
    if os.path.exists(mf):
        try:
            old_manifest = json.load(open(mf))
        except Exception:
            old_manifest = {}
    manifest = {"built_at": now_iso(), "surfaces": {}, "unavailable": []}
    builders = (("local", build_local), ("drive", build_drive),
                ("onedrive", build_onedrive), ("dropbox", build_dropbox),
                ("keep", build_keep))
    for name, fn in builders:
        t0 = time.time()
        try:
            records, info = fn()
            write_jsonl(f"{name}.jsonl", records)
            manifest["surfaces"][name] = {"count": len(records),
                                          "seconds": round(time.time() - t0, 1),
                                          "status": "ok", **info}
            print(f"{name}: {len(records)} records in {time.time()-t0:.1f}s",
                  flush=True)
        except Exception as e:
            manifest["surfaces"][name] = {"count": 0,
                                          "seconds": round(time.time() - t0, 1),
                                          "status": "unavailable"}
            manifest["unavailable"].append({"surface": name,
                                            "reason": str(e)[:300]})
            print(f"{name}: UNAVAILABLE — {e}", flush=True)
    t0 = time.time()
    try:
        records, info, unav = build_email(old_manifest)
        write_jsonl("email.jsonl", records)
        manifest["surfaces"]["email"] = {"count": len(records),
                                         "seconds": round(time.time() - t0, 1),
                                         "status": "ok", **info}
        manifest["unavailable"].extend(unav)
        print(f"email: {len(records)} records in {time.time()-t0:.1f}s",
              flush=True)
    except Exception as e:
        manifest["surfaces"]["email"] = {"count": 0,
                                         "seconds": round(time.time() - t0, 1),
                                         "status": "unavailable"}
        manifest["unavailable"].append({"surface": "email",
                                        "reason": str(e)[:300]})
        print(f"email: UNAVAILABLE — {e}", flush=True)
    with open(mf, "w") as fh:
        json.dump(manifest, fh, indent=2)
    print("manifest written:", json.dumps(manifest["surfaces"], indent=2)[:600])


if __name__ == "__main__":
    main()
