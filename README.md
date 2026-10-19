# Universal cached file + email search (zero-token)

One fast search across all your file surfaces, built on prebuilt
local indexes. Searching is a deterministic script over cached JSONL files:
**no model calls, no network, no tokens** in warm search (about a second).
This is the first stop for any "find a file" or "find an email" request.

## Surfaces

| Surface | Index | Source |
|---|---|---|
| Local files | `hidden_files/local.jsonl` | walk of `~/workspace`, `~/memory`, `~/dreams`, `~/hooks` (skips `.git`, `node_modules`) |
| Google Drive | `hidden_files/drive.jsonl` | `rclone lsjson --recursive` on the `gdrive` remote; records carry the Drive file ID and a `drive.google.com/file/d/<id>/view` link |
| OneDrive | `hidden_files/onedrive.jsonl` | the OneDrive connector inventory (`onedrive-cleanup` delta inventory JSON); manifest records which inventory file and its age |
| Dropbox | `hidden_files/dropbox.jsonl` | Dropbox connector `list_folder` (recursive, cursor-paginated) |
| Google Keep | `hidden_files/keep.jsonl` | `keep list`, titles + IDs only (note bodies are never indexed) |
| Email | `hidden_files/email.jsonl` | Gmail + Outlook + the agent mailbox, **headers/metadata only** (see below) |

`hidden_files/manifest.json` records per-surface record counts, build
seconds, build time, unavailable surfaces with reasons, and for email the
per-mailbox coverage (count, earliest/latest indexed date, watermark).

## Refresh

```sh
python3 ~/workspace/file-search/build_index.py
```

Rebuilds every file surface from scratch and refreshes email
**incrementally**: each mailbox stores a watermark (newest indexed date) in
the manifest, and a refresh pulls only newer mail and merges by message ID.
File surfaces are read-only against the cloud (list/search only). A surface
whose CLI is missing or unauthenticated is marked unavailable in the
manifest with the reason, and the rest still build.

## Search

```sh
~/workspace/file-search/find-file.sh "Cover Letter"
~/workspace/file-search/find-file.sh "Tax Return"
~/workspace/file-search/find-file.sh "invoice" --regex
~/workspace/file-search/find-file.sh "PSEG" --live     # live CLI/connector fallback
~/workspace/file-search/find-file.sh "standup" --content  # also rg local file contents
```

- Query is a case-insensitive substring by default; regex when the query
  contains regex metacharacters or with `--regex`.
- Matches name + path (email: subject, from, to, snippet, attachment
  names). Ranking: exact name match, then name substring, then path-only;
  ties by most recently modified. Results are grouped by surface, capped at
  20 per surface (`--limit N`) with a total count line.
- Email results show date, from, subject, and a Gmail deep link
  (`https://mail.google.com/mail/u/0/#inbox/<message-id>`) for Gmail mail.
- Prints a warning when the index is more than 24 hours old.
- `--live` (also automatic on an index miss) runs live fallbacks and merges,
  marked LIVE: local `rg --files`, Drive via the Drive API name search
  (gws CLI; an `rclone lsf --include` full-tree scan is the last-resort
  fallback and takes minutes at this Drive's size), OneDrive via the
  connector's `search`.

## Email cache design

- Headers/metadata only: message ID, date, from, to, subject, snippet,
  labels/folder, `has_attachments`, attachment filenames. **No message
  bodies are ever stored**; bodies are read live on demand when needed.
- Gmail: backfill takes the newest ~1,500 messages, paged backwards with
  `before:` queries (the `+triage` shortcut costs ~0.7 s per message and
  caps each call at ~500, so the first build is deliberately bounded; the
  manifest records the actual earliest covered date). Attachment flags come
  from a `has:attachment` list; attachment filenames are enriched for the
  newest 60 attachment-bearing messages via full metadata reads.
- Outlook: inbox + sent items, paged newest-first (the connector's list
  excludes likely bulk mail by default). No attachment filenames (not
  exposed by the list API).
- Agent mailbox: INBOX via the
  local-agent-mail CLI.

## Limitations

- **Google Photos is out of scope**: it is browser-only, with no CLI/API,
  so it never joins this search.
- Cloud content search is out of scope: cloud surfaces match on name/path
  (email: headers) only. Local file *contents* are searchable with
  `--content`.
- Gmail attachment-filename coverage is partial (enrichment cap above);
  `has_attachments` flags are complete for the indexed window.
- OneDrive freshness rides the connector inventory's own refresh cadence.
- Warm search is ~1 s; the first (cold) build takes several minutes,
  dominated by Gmail.
