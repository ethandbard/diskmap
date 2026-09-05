#!/usr/bin/env python3
"""
diskmap.py — a local, offline TreeSize-style tool.

Scans a directory, then writes a single self-contained HTML report with:
  1. An interactive filesize treemap (click to zoom into subfolders)
  2. Tables of the largest files, the biggest file types, and old-and-large files
  3. A collapsible tree listing of every folder and file, annotated with size

Nothing leaves your machine. plotly.js is bundled inline by default so the
report works fully offline; --cdn produces a much smaller file that needs
internet access to view.

Usage:
    python diskmap.py                                  # launch the GUI
    python diskmap.py /path/to/folder
    python diskmap.py /path/to/folder -o report.html
    python diskmap.py /path/to/folder --exclude node_modules --exclude "*/cache/*"
    python diskmap.py /path/to/folder --max-tree-depth 4
    python diskmap.py /path/to/folder --cdn            # smaller file, needs internet

Requires: plotly
    pip install plotly
"""

import argparse
import fnmatch
import heapq
import html
import os
import sys
import time
from datetime import datetime

try:
    import plotly.graph_objects as go
except ImportError:  # reported later, so the GUI can show it in a dialog
    go = None

PLOTLY_HINT = ("This tool needs plotly. Install it with:\n"
               "  pip install plotly\n"
               "or, on externally-managed Linux environments:\n"
               "  pip install plotly --break-system-packages")

DEFAULT_EXCLUDES = [".git", "node_modules", "__pycache__", ".venv", "venv", ".DS_Store"]
SECONDS_PER_DAY = 86400


class Cancelled(Exception):
    """Raised inside the scan when a caller asks it to stop early."""


def human_size(num_bytes):
    n = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if n < 1024 or unit == "PB":
            return f"{int(n)} B" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} PB"


def format_date(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


def extended_path(path):
    r"""Windows caps paths at 260 characters unless they carry the \\?\ prefix.

    The prefix is only used for filesystem calls; nodes keep the readable path
    so the report stays legible.
    """
    if os.name != "nt":
        return path
    if path.startswith("\\\\?\\"):
        return path
    abs_path = os.path.abspath(path)
    if abs_path.startswith("\\\\"):
        return "\\\\?\\UNC" + abs_path[1:]
    return "\\\\?\\" + abs_path


def make_matcher(patterns):
    """Match a pattern against the entry name, and against the full path both
    natively and posix-style, so */cache/* works on every platform."""
    if not patterns:
        return lambda name, path: False

    def matches(name, path):
        posix = path.replace("\\", "/")
        for pat in patterns:
            if fnmatch.fnmatch(name, pat):
                return True
            if ("/" in pat or "\\" in pat) and (
                fnmatch.fnmatch(posix, pat.replace("\\", "/"))
                or fnmatch.fnmatch(path, pat)
            ):
                return True
        return False

    return matches


class Node:
    __slots__ = ("name", "path", "is_dir", "is_link", "size", "mtime", "children", "error")

    def __init__(self, name, path, is_dir, is_link=False):
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.is_link = is_link
        self.size = 0
        self.mtime = 0
        self.children = []
        self.error = None


class ScanResult:
    __slots__ = ("root", "total_files", "total_dirs", "n_errors", "n_links",
                 "largest", "old_large", "by_ext", "elapsed", "min_age_days")

    def __init__(self):
        self.root = None
        self.total_files = 0
        self.total_dirs = 0
        self.n_errors = 0
        self.n_links = 0
        self.largest = []
        self.old_large = []
        self.by_ext = {}
        self.elapsed = 0.0
        self.min_age_days = 365


def _is_link(entry):
    try:
        if entry.is_symlink():
            return True
    except OSError:
        return False
    junction = getattr(entry, "is_junction", None)
    if junction is None:
        return False
    try:
        return junction()
    except OSError:
        return False


def scan(root_path, exclude_patterns, progress=None, cancel=None,
         top_n=100, min_age_days=365):
    """Walk root_path iteratively, returning a ScanResult.

    Iterative rather than recursive so deeply nested trees cannot exhaust the
    stack, and os.scandir rather than iterdir+stat so each entry costs one
    syscall instead of two.
    """
    started = time.monotonic()
    root_path = os.path.abspath(root_path)
    excluded = make_matcher(exclude_patterns)
    result = ScanResult()
    result.min_age_days = min_age_days

    old_cutoff = time.time() - min_age_days * SECONDS_PER_DAY
    largest = []      # min-heaps of (size, path, mtime), capped at top_n
    old_large = []
    by_ext = {}

    root = Node(os.path.basename(root_path) or root_path, root_path, True)
    try:
        root.mtime = os.stat(extended_path(root_path)).st_mtime
    except OSError:
        pass

    dirs_in_order = [root]
    stack = [root]
    last_tick = 0.0

    while stack:
        node = stack.pop()
        if cancel is not None and cancel.is_set():
            raise Cancelled()

        now = time.monotonic()
        if progress is not None and now - last_tick > 0.15:
            last_tick = now
            progress(result.total_files, result.total_dirs, node.path)

        try:
            with os.scandir(extended_path(node.path)) as it:
                entries = list(it)
        except PermissionError:
            node.error = "permission denied"
            result.n_errors += 1
            continue
        except OSError as exc:
            node.error = exc.strerror or str(exc)
            result.n_errors += 1
            continue

        entries.sort(key=lambda e: e.name.lower())
        for entry in entries:
            entry_path = os.path.join(node.path, entry.name)
            if excluded(entry.name, entry_path):
                continue

            link = _is_link(entry)
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False

            child = Node(entry.name, entry_path, is_dir and not link, link)
            try:
                info = entry.stat(follow_symlinks=False)
                child.mtime = info.st_mtime
                if not child.is_dir:
                    child.size = info.st_size
            except OSError as exc:
                child.error = exc.strerror or str(exc)
                result.n_errors += 1

            node.children.append(child)

            if link:
                # Listed at zero bytes: following them would double-count or
                # loop, but hiding them makes a 40GB junction invisible.
                result.n_links += 1
            elif child.is_dir:
                result.total_dirs += 1
                dirs_in_order.append(child)
                stack.append(child)
            else:
                result.total_files += 1
                ext = os.path.splitext(entry.name)[1].lower() or "(no extension)"
                slot = by_ext.get(ext)
                if slot is None:
                    by_ext[ext] = [1, child.size]
                else:
                    slot[0] += 1
                    slot[1] += child.size
                item = (child.size, entry_path, child.mtime)
                if len(largest) < top_n:
                    heapq.heappush(largest, item)
                elif child.size > largest[0][0]:
                    heapq.heapreplace(largest, item)
                if child.mtime and child.mtime < old_cutoff:
                    if len(old_large) < top_n:
                        heapq.heappush(old_large, item)
                    elif child.size > old_large[0][0]:
                        heapq.heapreplace(old_large, item)

    # Children are discovered after their parents, so walking the list
    # backwards totals every subtree bottom-up without recursion.
    for node in reversed(dirs_in_order):
        node.size = sum(c.size for c in node.children)
        node.children.sort(key=lambda c: c.size, reverse=True)

    result.root = root
    result.total_dirs += 1  # count the root itself
    result.largest = sorted(largest, reverse=True)
    result.old_large = sorted(old_large, reverse=True)
    result.by_ext = by_ext
    result.elapsed = time.monotonic() - started
    return result


def flatten_for_treemap(root, max_depth, max_children):
    """Build ids/labels/parents/values arrays for the plotly treemap.

    Anything past max_depth, or past the first max_children entries of a
    folder, collapses into a single "..." block sized by the remainder so the
    areas still add up under branchvalues="total".
    """
    ids, labels, parents, values, hovertext = [], [], [], [], []

    def add(node_id, label, parent_id, value, hover):
        ids.append(node_id)
        labels.append(label)
        parents.append(parent_id)
        values.append(value)
        hovertext.append(hover)

    def add_remainder(node_id, remainder, n_hidden):
        if remainder <= 0 or n_hidden <= 0:
            return
        add(node_id + "/\x00more", "...", node_id, remainder,
            f"{n_hidden:,} more items not expanded<br>{human_size(remainder)}")

    stack = [(root, "", 0)]
    while stack:
        node, parent_id, depth = stack.pop()
        node_id = node.path
        suffix = "/" if node.is_dir else ""
        add(node_id, node.name + suffix, parent_id, max(node.size, 0),
            f"{html.escape(node.path)}<br>{human_size(node.size)}")

        if not node.is_dir or not node.children:
            continue

        if depth >= max_depth:
            add_remainder(node_id, node.size, len(node.children))
            continue

        shown_bytes = 0
        n_shown = 0
        for child in node.children[:max_children]:
            if child.size <= 0 and not child.children:
                continue
            stack.append((child, node_id, depth + 1))
            shown_bytes += child.size
            n_shown += 1
        add_remainder(node_id, node.size - shown_bytes, len(node.children) - n_shown)

    return ids, labels, parents, values, hovertext


def build_treemap_html(root, max_depth, max_children, inline=True):
    if go is None:
        raise RuntimeError(PLOTLY_HINT)
    ids, labels, parents, values, hovertext = flatten_for_treemap(root, max_depth, max_children)
    fig = go.Figure(go.Treemap(
        ids=ids,
        labels=labels,
        parents=parents,
        values=values,
        branchvalues="total",
        hovertext=hovertext,
        hoverinfo="text",
        text=[human_size(v) for v in values],
        textinfo="label+text",
        marker=dict(
            colors=values,
            colorscale=[[0, "#12314F"], [0.5, "#4FB3D9"], [1, "#E8A33D"]],
            line=dict(width=1, color="#081522"),
        ),
        pathbar=dict(visible=True, textfont=dict(size=13)),
        root_color="#081522",
    ))
    fig.update_layout(
        margin=dict(t=10, l=4, r=4, b=4),
        height=640,
        paper_bgcolor="#081522",
        font=dict(color="#F2EFE9", family="Inter, sans-serif", size=12),
    )
    return fig.to_html(full_html=False, div_id="treemap-div",
                       include_plotlyjs="inline" if inline else "cdn")


def _file_html(node):
    err = f' <span class="dm-err">({html.escape(node.error)})</span>' if node.error else ""
    link = ' <span class="dm-link">link</span>' if node.is_link else ""
    date = format_date(node.mtime)
    date_html = f'<span class="dm-date">{date}</span>' if date else ""
    return (f'<div class="dm-file"><span class="dm-name">{html.escape(node.name)}</span> '
            f'<span class="dm-size">{human_size(node.size)}</span>{date_html}{link}{err}</div>')


def build_tree_html(root, max_depth=None, max_children=400):
    """Collapsible <details> tree, largest-first (the scan already sorted it).

    Built with an explicit stack, and capped by depth and by children per
    folder, so a scan with a million files cannot produce HTML no browser will
    open.
    """
    out = []
    stack = [("node", root, 0)]
    while stack:
        kind, payload, depth = stack.pop()
        if kind == "raw":
            out.append(payload)
            continue

        node = payload
        if not node.is_dir:
            out.append(_file_html(node))
            continue

        count = len(node.children)
        capped = max_depth is not None and depth >= max_depth
        err = f' <span class="dm-err">({html.escape(node.error)})</span>' if node.error else ""
        note = ""
        if capped and count:
            note = f'<span class="dm-count">depth limit — {count:,} not shown</span>'
        out.append(
            f'<details class="dm-dir"{" open" if depth < 1 else ""}>'
            f'<summary><span class="dm-name">{html.escape(node.name)}/</span> '
            f'<span class="dm-size">{human_size(node.size)}</span>'
            f'<span class="dm-count">{count:,} item{"" if count == 1 else "s"}</span>'
            f'{note}{err}</summary><div class="dm-children">'
        )

        # Pushed in reverse: the closing tag pops last, children pop in order.
        stack.append(("raw", "</div></details>", 0))
        if not capped:
            shown = node.children[:max_children]
            hidden = node.children[max_children:]
            if hidden:
                hidden_bytes = sum(c.size for c in hidden)
                stack.append(("raw",
                              f'<div class="dm-file dm-more">… {len(hidden):,} more items '
                              f'<span class="dm-size">{human_size(hidden_bytes)}</span></div>', 0))
            for child in reversed(shown):
                stack.append(("node", child, depth + 1))

    return "".join(out)


def _table(rows, headers):
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = []
    for row in rows:
        cells = "".join(f"<td>{c}</td>" for c in row)
        body.append(f"<tr>{cells}</tr>")
    if not body:
        body.append(f'<tr><td colspan="{len(headers)}" class="dm-empty">Nothing to show.</td></tr>')
    return f'<table class="dm-table"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'


def build_largest_table(items, root_path, limit):
    rows = []
    for size, path, mtime in items[:limit]:
        rel = os.path.relpath(path, root_path) if path.startswith(root_path) else path
        rows.append((f'<span class="dm-num">{human_size(size)}</span>',
                     f'<span class="dm-path">{html.escape(rel)}</span>',
                     f'<span class="dm-dim">{format_date(mtime)}</span>'))
    return _table(rows, ["Size", "File", "Modified"])


def build_ext_table(by_ext, total_size, limit):
    ranked = sorted(by_ext.items(), key=lambda kv: kv[1][1], reverse=True)[:limit]
    rows = []
    for ext, (count, size) in ranked:
        pct = (size / total_size * 100) if total_size else 0
        bar = (f'<span class="dm-bar"><span style="width:{min(pct, 100):.1f}%"></span></span>'
               f'<span class="dm-dim">{pct:.1f}%</span>')
        rows.append((f'<span class="dm-path">{html.escape(ext)}</span>',
                     f'<span class="dm-num">{human_size(size)}</span>',
                     f'<span class="dm-dim">{count:,}</span>', bar))
    return _table(rows, ["Type", "Size", "Files", "Share"])


REPORT_CSS = """
<style>
  :root{
    --ink:#0B1E33; --ink-soft:#12314F; --paper:#F2EFE9; --line:#2A4A6B;
    --cyan:#4FB3D9; --amber:#E8A33D; --slate:#7C90A3; --danger:#D9694F;
    --mono:'SFMono-Regular',Menlo,Consolas,monospace; --body:'Inter',-apple-system,sans-serif;
  }
  *{box-sizing:border-box;}
  body{margin:0;background:#081522;color:var(--paper);font-family:var(--body);}
  .dm-wrap{max-width:1080px;margin:0 auto;padding:28px 22px 60px;}
  .dm-header{border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:20px;}
  .dm-title{font-size:24px;font-weight:700;margin:0;}
  .dm-title span{color:var(--cyan);}
  .dm-sub{font-family:var(--mono);font-size:12px;color:var(--slate);margin-top:6px;word-break:break-all;}
  .dm-stats{display:flex;gap:24px;flex-wrap:wrap;margin:14px 0 22px;font-family:var(--mono);font-size:12.5px;}
  .dm-stat b{display:block;color:var(--cyan);font-size:16px;}
  .dm-section-title{font-size:13px;text-transform:uppercase;letter-spacing:.1em;color:var(--cyan);margin:26px 0 10px;}
  .dm-section-note{font-family:var(--mono);font-size:11px;color:var(--slate);margin:-6px 0 10px;}
  .dm-panel{background:rgba(18,49,79,0.45);border:1px solid var(--line);border-radius:4px;padding:10px;}
  .dm-panel-scroll{max-height:420px;overflow:auto;}
  .dm-table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12px;}
  .dm-table th{text-align:left;font-weight:500;color:var(--slate);font-size:10.5px;text-transform:uppercase;
    letter-spacing:.08em;padding:4px 8px;border-bottom:1px solid var(--line);position:sticky;top:0;background:#0D2338;}
  .dm-table td{padding:3px 8px;border-bottom:1px solid rgba(124,144,163,0.12);vertical-align:middle;}
  .dm-table tr:hover td{background:rgba(79,179,217,0.07);}
  .dm-num{color:var(--amber);white-space:nowrap;}
  .dm-path{word-break:break-all;}
  .dm-dim{color:var(--slate);white-space:nowrap;}
  .dm-empty{color:var(--slate);text-align:center;padding:14px;}
  .dm-bar{display:inline-block;width:110px;height:7px;background:rgba(124,144,163,0.2);border-radius:2px;
    overflow:hidden;margin-right:8px;vertical-align:middle;}
  .dm-bar span{display:block;height:100%;background:var(--cyan);}
  details.dm-dir{margin-left:16px;border-left:1px solid rgba(124,144,163,0.25);padding-left:10px;}
  details.dm-dir > summary{cursor:pointer;padding:3px 0;list-style:none;}
  details.dm-dir > summary::-webkit-details-marker{display:none;}
  details.dm-dir > summary::before{content:"\\25B8 ";color:var(--slate);}
  details.dm-dir[open] > summary::before{content:"\\25BE ";color:var(--cyan);}
  .dm-file{margin-left:16px;padding:2px 0 2px 10px;border-left:1px solid rgba(124,144,163,0.1);}
  .dm-more{color:var(--slate);font-family:var(--mono);font-size:11.5px;}
  .dm-name{font-family:var(--mono);font-size:12.5px;}
  .dm-size{font-family:var(--mono);font-size:11px;color:var(--amber);margin-left:8px;}
  .dm-count{font-family:var(--mono);font-size:10.5px;color:var(--slate);margin-left:8px;}
  .dm-date{font-family:var(--mono);font-size:10.5px;color:var(--slate);margin-left:8px;}
  .dm-link{font-family:var(--mono);font-size:10.5px;color:var(--cyan);margin-left:6px;}
  .dm-err{font-family:var(--mono);font-size:10.5px;color:var(--danger);margin-left:6px;}
  .dm-footnote{font-family:var(--mono);font-size:10.5px;color:var(--slate);margin-top:26px;line-height:1.6;}
  .dm-toolbar{display:flex;gap:8px;margin-bottom:12px;}
  .dm-btn{font-family:var(--mono);font-size:11px;background:transparent;border:1px solid var(--line);
    color:var(--paper);padding:6px 10px;border-radius:2px;cursor:pointer;}
  .dm-btn:hover{border-color:var(--cyan);color:var(--cyan);}
</style>
"""

REPORT_JS = """
<script>
  function dmExpandAll(open){
    document.querySelectorAll('#tree-root details').forEach(function(d){ d.open = open; });
  }
</script>
"""


def build_report(result, root_path, max_tree_depth, treemap_depth,
                 max_children=400, top_n=100, inline_plotly=True):
    root = result.root
    tree_html = build_tree_html(root, max_depth=max_tree_depth, max_children=max_children)
    treemap_html = build_treemap_html(root, treemap_depth, max_children, inline=inline_plotly)
    largest_html = build_largest_table(result.largest, root_path, top_n)
    old_html = build_largest_table(result.old_large, root_path, top_n)
    ext_html = build_ext_table(result.by_ext, root.size, 25)

    depth_note = (f"Capped at depth {max_tree_depth}. "
                  if max_tree_depth is not None else "")
    notes = []
    if result.n_errors:
        notes.append(f"{result.n_errors:,} unreadable entries")
    if result.n_links:
        notes.append(f"{result.n_links:,} symlinks/junctions listed at 0 B")
    note_line = " · ".join(notes)

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>diskmap — {html.escape(str(root_path))}</title>
{REPORT_CSS}
</head><body>
<div class="dm-wrap">
  <div class="dm-header">
    <p class="dm-title">disk<span>map</span></p>
    <div class="dm-sub">{html.escape(str(root_path))}</div>
  </div>

  <div class="dm-stats">
    <div class="dm-stat"><b>{human_size(root.size)}</b>total size</div>
    <div class="dm-stat"><b>{result.total_files:,}</b>files</div>
    <div class="dm-stat"><b>{result.total_dirs:,}</b>folders</div>
    <div class="dm-stat"><b>{result.elapsed:.1f}s</b>scan time</div>
  </div>

  <div class="dm-section-title">Treemap — click a block to zoom in</div>
  <div class="dm-panel">{treemap_html}</div>

  <div class="dm-section-title">Largest files</div>
  <div class="dm-panel dm-panel-scroll">{largest_html}</div>

  <div class="dm-section-title">Space by file type</div>
  <div class="dm-panel dm-panel-scroll">{ext_html}</div>

  <div class="dm-section-title">Large and untouched</div>
  <div class="dm-section-note">Biggest files not modified in over {result.min_age_days} days.</div>
  <div class="dm-panel dm-panel-scroll">{old_html}</div>

  <div class="dm-section-title">Tree listing — sorted largest first</div>
  <div class="dm-toolbar">
    <button class="dm-btn" onclick="dmExpandAll(true)">Expand all</button>
    <button class="dm-btn" onclick="dmExpandAll(false)">Collapse all</button>
  </div>
  <div class="dm-panel" id="tree-root">{tree_html}</div>

  <div class="dm-footnote">
    Generated locally by diskmap.py. {depth_note}Folders show at most {max_children:,} children.<br/>
    Symlinks and junctions are listed but not followed. {note_line}
  </div>
</div>
{REPORT_JS}
</body></html>"""


def run_report(path, output, excludes, max_tree_depth, treemap_depth, max_children,
               top_n, min_age_days, inline_plotly, progress=None, cancel=None,
               log=print):
    """Scan, build, and write the report. Returns the output path."""
    if go is None:
        raise RuntimeError(PLOTLY_HINT)
    root_path = os.path.abspath(path)
    if not os.path.isdir(root_path):
        raise NotADirectoryError(f"'{path}' is not a directory.")

    patterns = list(dict.fromkeys(DEFAULT_EXCLUDES + list(excludes)))
    log(f"Scanning {root_path} ...")
    result = scan(root_path, patterns, progress=progress, cancel=cancel,
                  top_n=top_n, min_age_days=min_age_days)
    log(f"Found {human_size(result.root.size)} in {result.total_files:,} files "
        f"({result.elapsed:.1f}s). Building report ...")

    report = build_report(result, root_path, max_tree_depth, treemap_depth,
                          max_children=max_children, top_n=top_n,
                          inline_plotly=inline_plotly)
    out_path = os.path.abspath(output)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(report)
    log(f"Report written to {out_path}")
    return out_path


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="TreeSize-style local disk usage report (treemap + tables + tree).")
    parser.add_argument("path", nargs="?", help="Top-level directory to scan (omit to launch the GUI)")
    parser.add_argument("-o", "--output", default="diskmap_report.html",
                        help="Output HTML file (default: diskmap_report.html)")
    parser.add_argument("--exclude", action="append", default=[],
                        help="Glob to exclude, matched on name or full path (repeatable)")
    parser.add_argument("--max-tree-depth", type=int, default=None,
                        help="Limit tree listing depth (default: unlimited)")
    parser.add_argument("--treemap-depth", type=int, default=6,
                        help="Limit treemap depth for performance (default: 6)")
    parser.add_argument("--max-children", type=int, default=400,
                        help="Max entries listed per folder before collapsing (default: 400)")
    parser.add_argument("--top", type=int, default=100,
                        help="Rows in the largest-files tables (default: 100)")
    parser.add_argument("--min-age-days", type=int, default=365,
                        help="Age threshold for the large-and-untouched table (default: 365)")
    parser.add_argument("--cdn", action="store_true",
                        help="Load plotly.js from a CDN instead of inlining it (smaller file, needs internet)")
    parser.add_argument("--gui", action="store_true", help="Launch the GUI even if a path is given")
    parser.add_argument("--open", action="store_true", help="Open the report when it is finished")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    if args.gui or args.path is None:
        from diskmap_gui import launch
        return launch(args)

    if go is None:
        print(PLOTLY_HINT, file=sys.stderr)
        return 1

    try:
        out_path = run_report(
            args.path, args.output, args.exclude, args.max_tree_depth,
            args.treemap_depth, args.max_children, args.top, args.min_age_days,
            inline_plotly=not args.cdn,
            progress=lambda files, dirs, path: print(
                f"  {files:,} files / {dirs:,} folders ...", end="\r", flush=True),
        )
    except (NotADirectoryError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130

    print("Open it in a browser to explore.")
    if args.open:
        import webbrowser
        webbrowser.open(f"file:///{out_path.replace(os.sep, '/')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
