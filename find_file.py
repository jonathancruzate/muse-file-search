#!/usr/bin/env python3
"""find_file.py — zero-token universal cached search over your file surfaces.

Searches prebuilt JSONL indexes in hidden_files/ (local, Google Drive,
OneDrive, Dropbox, Google Keep, email headers). No model calls, no network
in warm mode. --live adds CLI/connector fallbacks on a miss.
"""
import argparse, json, os, re, subprocess, sys, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
IDX = os.path.join(HERE, "hidden_files")
SURFACE_ORDER = ["local", "drive", "onedrive", "dropbox", "keep", "email"]
SURFACE_TITLES = {
    "local": "LOCAL", "drive": "GOOGLE DRIVE", "onedrive": "ONEDRIVE",
    "dropbox": "DROPBOX", "keep": "GOOGLE KEEP", "email": "EMAIL",
}
LOCAL_ROOTS = [os.path.expanduser(p) for p in
               ("~/workspace", "~/memory", "~/dreams", "~/hooks")]
REGEX_HINT = set("[]()*+?^$|{}")  # '.' alone stays a literal substring


def human_size(n):
    if n is None:
        return "-"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    if n < 0:  # e.g. Google Docs-native files report size -1 via rclone
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def parse_ts(s):
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def load_candidates(query, use_regex):
    """Return parsed JSON records whose raw JSONL line matches the query.
    Prefilters with ripgrep over the index files (fast); pure-python fallback."""
    files = [os.path.join(IDX, f"{s}.jsonl") for s in SURFACE_ORDER]
    files = [f for f in files if os.path.exists(f)]
    if not files:
        return []
    lines = None
    rg = subprocess.run(["which", "rg"], capture_output=True, text=True).stdout.strip()
    if rg:
        args = [rg, "--no-heading", "--no-filename", "-i"]
        args += (["-e", query] if use_regex else ["-F", "-e", query])
        args += files
        p = subprocess.run(args, capture_output=True, text=True)
        if p.returncode in (0, 1):
            lines = p.stdout.splitlines()
    if lines is None:  # fallback: stream in python
        pat = re.compile(query, re.I) if use_regex else None
        ql = query.lower()
        lines = []
        for f in files:
            with open(f, errors="replace") as fh:
                for line in fh:
                    if (pat.search(line) if pat else ql in line.lower()):
                        lines.append(line)
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def haystack(rec):
    parts = [rec.get("name") or "", rec.get("path") or ""]
    if rec.get("surface") == "email":
        parts += [rec.get("from") or "", rec.get("to") or "",
                  rec.get("snippet") or "", " ".join(rec.get("attachments") or [])]
    return " ".join(parts).lower()


def rank_key(rec, query, use_regex):
    name = (rec.get("name") or "").lower()
    ql = query.lower()
    if use_regex:
        tier = 1 if re.search(query, name, re.I) else 2
    elif name == ql:
        tier = 0
    elif ql in name:
        tier = 1
    else:
        tier = 2
    return (tier, -parse_ts(rec.get("modified")))


def fmt_file(rec):
    bits = [rec.get("name") or "(unnamed)"]
    meta = []
    if rec.get("size") is not None:
        meta.append(human_size(rec["size"]))
    if rec.get("modified"):
        meta.append(str(rec["modified"])[:10])
    if meta:
        bits.append("(" + " · ".join(meta) + ")")
    line = "  " + " ".join(bits)
    line += f"\n    {rec.get('path') or ''}"
    extra = rec.get("link") or (f"id: {rec['id']}" if rec.get("id") else None)
    if extra:
        line += f"\n    {extra}"
    return line


def fmt_email(rec):
    date = str(rec.get("modified") or "")[:10]
    frm = rec.get("from") or "?"
    subj = rec.get("name") or "(no subject)"
    mb = rec.get("mailbox") or ""
    line = f"  [{date}] {frm} — {subj}"
    if mb:
        line += f"  <{mb}>"
    if rec.get("has_attachments"):
        att = ", ".join(rec.get("attachments") or []) or "attachment"
        line += f"\n    📎 {att}"
    if rec.get("snippet"):
        line += f"\n    {rec['snippet'][:110]}"
    if rec.get("link"):
        line += f"\n    {rec['link']}"
    return line


def search_indexes(query, use_regex, limit):
    recs = [r for r in load_candidates(query, use_regex)
            if use_regex or query.lower() in haystack(r)]
    if use_regex:  # rg matched the raw line; confirm against haystack too
        pat = re.compile(query, re.I)
        recs = [r for r in recs if pat.search(haystack(r)) or
                pat.search(r.get("name") or "") or pat.search(r.get("path") or "")]
    grouped = {}
    for r in recs:
        grouped.setdefault(r.get("surface", "?"), []).append(r)
    for s in grouped:
        grouped[s].sort(key=lambda r: rank_key(r, query, use_regex))
    return grouped


def live_local(query):
    out = []
    rg = subprocess.run(["which", "rg"], capture_output=True, text=True).stdout.strip()
    if rg:
        p = subprocess.run([rg, "--files"] + LOCAL_ROOTS,
                           capture_output=True, text=True)
        paths = p.stdout.splitlines()
    else:
        paths = []
        for root in LOCAL_ROOTS:
            for dp, dn, fn in os.walk(root):
                dn[:] = [d for d in dn if d not in (".git", "node_modules")]
                paths += [os.path.join(dp, f) for f in fn]
    ql = query.lower()
    for p in paths:
        if ql in p.lower():
            try:
                st = os.stat(p)
                mod = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
                size = st.st_size
            except OSError:
                mod, size = None, None
            out.append({"surface": "local", "name": os.path.basename(p),
                        "path": p, "size": size, "modified": mod, "live": True})
    return out


def live_drive(query):
    """Live Drive name search via the Drive API (gws CLI) — seconds.
    (rclone lsf --include over the full tree takes >10 min at this Drive's
    size, so it is only a last-resort fallback.)"""
    gws = "/opt/hatch/bin/hatch_gws_cli"
    out = []
    q = query.replace("'", "\\'")
    try:
        p = subprocess.run([gws, "drive", "files", "list", "--params",
                            json.dumps({"q": f"name contains '{q}' and trashed = false",
                                        "fields": "files(id,name,modifiedTime,size,webViewLink)",
                                        "pageSize": 50}),
                            "--format", "json"],
                           capture_output=True, text=True, timeout=120)
        for f in json.loads(p.stdout).get("files", []):
            size = f.get("size")
            out.append({"surface": "drive", "name": f.get("name"),
                        "path": f.get("name"), "size": int(size) if size else None,
                        "modified": f.get("modifiedTime"), "id": f.get("id"),
                        "link": f.get("webViewLink"), "live": True})
        return out
    except Exception:
        pass
    rclone = os.path.expanduser("~/.local/bin/rclone")
    p = subprocess.run([rclone, "lsf", "--recursive", "--files-only",
                        "--format", "psti", "--include", f"*{query}*",
                        "--ignore-case", "gdrive:"],
                       capture_output=True, text=True, timeout=300)
    for line in p.stdout.splitlines():
        parts = line.rsplit(" ", 3)
        if len(parts) < 4:
            continue
        path, size, mod, fid = parts[0], parts[1], parts[2], parts[3]
        out.append({"surface": "drive", "name": os.path.basename(path),
                    "path": path, "size": int(size) if size.lstrip("-").isdigit() else None,
                    "modified": mod, "id": fid,
                    "link": f"https://drive.google.com/file/d/{fid}/view" if fid else None,
                    "live": True})
    return out


def live_onedrive(query):
    cli = os.path.expanduser("~/workspace/skills/onedrive/bin/onedrive.py")
    out = []
    p = subprocess.run(["python3", cli, "search", query, "--limit", "50"],
                       capture_output=True, text=True, timeout=300)
    try:
        d = json.loads(p.stdout)
    except ValueError:
        return out
    for it in d.get("items", []):
        if it.get("type") != "file":
            continue
        parent = (it.get("parent") or "").replace("/drive/root:", "").strip("/")
        path = f"{parent}/{it['name']}" if parent else it["name"]
        out.append({"surface": "onedrive", "name": it["name"], "path": path,
                    "size": None, "modified": None, "live": True})
    return out


def live_all(query):
    grouped = {}
    for fn, surf in ((live_local, "local"), (live_drive, "drive"),
                     (live_onedrive, "onedrive")):
        try:
            hits = fn(query)
        except Exception as e:  # live fallback is best-effort
            print(f"  (live {surf} failed: {e})", file=sys.stderr)
            hits = []
        if hits:
            grouped.setdefault(surf, []).extend(hits)
    return grouped


def content_search(query, limit):
    rg = subprocess.run(["which", "rg"], capture_output=True, text=True).stdout.strip()
    if not rg:
        return []
    globs = ["-g", "*.md", "-g", "*.txt", "-g", "*.json", "-g", "*.py",
             "-g", "*.sh", "-g", "*.yaml", "-g", "*.yml", "-g", "*.csv",
             "-g", "*.html", "-g", "*.log", "-g", "!node_modules", "-g", "!.git"]
    p = subprocess.run([rg, "-i", "-l", "--max-count=1", "-e", query] + globs +
                       LOCAL_ROOTS, capture_output=True, text=True)
    out = []
    for path in p.stdout.splitlines()[:limit]:
        try:
            st = os.stat(path)
            mod = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
        except OSError:
            mod = None
        out.append({"surface": "local", "name": os.path.basename(path),
                    "path": path, "size": None, "modified": mod, "content": True})
    return out


def index_age_warning():
    mf = os.path.join(IDX, "manifest.json")
    if not os.path.exists(mf):
        print("WARNING: no index manifest found — run build_index.py first.",
              file=sys.stderr)
        return
    try:
        built = json.load(open(mf))["built_at"]
        age_h = (time.time() - parse_ts(built)) / 3600
        if age_h > 24:
            print(f"WARNING: index is {age_h:.0f}h old (built {built}); "
                  f"run build_index.py to refresh.", file=sys.stderr)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="Universal cached file+email search (zero-token).")
    ap.add_argument("query")
    ap.add_argument("--live", action="store_true",
                    help="also run live CLI/connector fallbacks and merge")
    ap.add_argument("--regex", action="store_true", help="treat query as regex")
    ap.add_argument("--content", action="store_true",
                    help="also search local text-file contents with rg")
    ap.add_argument("--limit", type=int, default=20, help="max results per surface")
    a = ap.parse_args()

    use_regex = a.regex or any(c in a.query for c in REGEX_HINT)
    index_age_warning()
    grouped = search_indexes(a.query, use_regex, a.limit)
    total = sum(len(v) for v in grouped.values())

    if a.live or total == 0:
        live = live_all(a.query)
        for surf, hits in live.items():
            grouped.setdefault(surf, []).extend(hits)
            total += len(hits)
        if live:
            print("(live fallback results merged, marked LIVE)")

    if total == 0 and not a.content:
        print(f'No matches for "{a.query}" across local, Drive, OneDrive, '
              f'Dropbox, Keep, email indexes.')
        return

    for surf in SURFACE_ORDER:
        hits = grouped.get(surf)
        if not hits:
            continue
        shown = hits[:a.limit]
        print(f"\n== {SURFACE_TITLES.get(surf, surf.upper())} — {len(hits)} match(es)"
              + (" (showing first " + str(a.limit) + ")" if len(hits) > a.limit else ""))
        for r in shown:
            tag = " [LIVE]" if r.get("live") else ""
            if surf == "email":
                print(fmt_email(r) + tag)
            else:
                print(fmt_file(r) + tag)

    if a.content:
        chits = content_search(a.query, a.limit)
        if chits:
            print(f"\n== LOCAL CONTENT MATCHES — {len(chits)} file(s) contain the text")
            for r in chits:
                print(fmt_file(r))


if __name__ == "__main__":
    main()
