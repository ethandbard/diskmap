#!/usr/bin/env python3
"""
diskmap.py — a local, offline TreeSize-style tool.

Scans a directory, then writes a single self-contained HTML report with:
  1. An animated, zoomable treemap with search, colour modes, and highlighting
  2. Tables of the largest files, the biggest file types, and old-and-large files
  3. A lazily expanded tree listing of every folder and file, annotated with size

Nothing leaves your machine. The report has no dependencies and works offline:
the page template (report_template.html) draws everything from embedded JSON.

Usage:
    python diskmap.py                                  # launch the GUI
    python diskmap.py /path/to/folder
    python diskmap.py /path/to/folder -o report.html
    python diskmap.py /path/to/folder --exclude node_modules --exclude "*/cache/*"
    python diskmap.py /path/to/folder --max-tree-depth 4

Standard library only.
"""

import argparse
import fnmatch
import heapq
import html
import json
import os
import sys
import time
from datetime import datetime

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


def encode_tree(root, max_depth=None, max_children=400):
    """Pack the scanned tree into nested lists for the report's JSON payload.

    Each node is [name, size, mtime_day, flags] with optional [children, extra]
    after it. flags: 1 folder, 2 link, 4 "more" placeholder, 8 error. extra
    holds the error text, or the hidden-item count for a "more" placeholder,
    whose size is the remainder so folder totals still add up.

    Built pre-order with an explicit stack: a record is attached to its parent
    before its own children are filled in, which works because lists are
    mutable.
    """
    def record(node):
        flags = (1 if node.is_dir else 0) | (2 if node.is_link else 0) | (8 if node.error else 0)
        rec = [node.name, node.size, int(node.mtime // SECONDS_PER_DAY) if node.mtime else 0, flags]
        if node.error:
            rec += [0, node.error]
        return rec

    root_rec = record(root)
    stack = [(root, root_rec, 0)]
    while stack:
        node, rec, depth = stack.pop()
        if not node.is_dir or not node.children:
            continue
        capped = max_depth is not None and depth >= max_depth
        shown = [] if capped else node.children[:max_children]
        kids = []
        for child in shown:
            child_rec = record(child)
            kids.append(child_rec)
            stack.append((child, child_rec, depth + 1))
        n_hidden = len(node.children) - len(shown)
        if n_hidden:
            kids.append(["", node.size - sum(c.size for c in shown), 0, 4, 0, n_hidden])
        if len(rec) == 4:
            rec.append(kids)
        else:
            rec[4] = kids
    return root_rec


def _file_rows(items, root_path, limit):
    rows = []
    for size, path, mtime in items[:limit]:
        rel = os.path.relpath(path, root_path) if path.startswith(root_path) else path
        rows.append([size, rel, int(mtime // SECONDS_PER_DAY) if mtime else 0])
    return rows


TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_template.html")


def build_report(result, root_path, max_tree_depth, treemap_depth,
                 max_children=400, top_n=100):
    """Fill report_template.html with the scan as one JSON payload.

    The page draws everything itself (canvas treemap, tables, lazy tree), so
    the report is the data plus a fixed template, not pre-rendered HTML.
    """
    root = result.root
    ranked_ext = sorted(result.by_ext.items(), key=lambda kv: kv[1][1], reverse=True)[:60]
    data = {
        "root": root_path,
        "sep": os.sep,
        "tree": encode_tree(root, max_tree_depth, max_children),
        "largest": _file_rows(result.largest, root_path, top_n),
        "old": _file_rows(result.old_large, root_path, top_n),
        "ext": [[ext, count, size] for ext, (count, size) in ranked_ext],
        "meta": {
            "files": result.total_files,
            "dirs": result.total_dirs,
            "errors": result.n_errors,
            "links": result.n_links,
            "elapsed": round(result.elapsed, 2),
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "today": int(time.time() // SECONDS_PER_DAY),
            "minAgeDays": result.min_age_days,
            "maxChildren": max_children,
            "maxTreeDepth": max_tree_depth,
            "treemapDepth": treemap_depth,
        },
    }
    # ensure_ascii keeps lone surrogates from Windows filenames writable, and
    # escaping "</" stops a filename from closing the <script> block early.
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    with open(TEMPLATE_PATH, encoding="utf-8") as fh:
        template = fh.read()
    page = template.replace("__DM_TITLE__", html.escape(str(root_path)))
    return page.replace("__DM_DATA__", payload)


def run_report(path, output, excludes, max_tree_depth, treemap_depth, max_children,
               top_n, min_age_days, progress=None, cancel=None, log=print):
    """Scan, build, and write the report. Returns the output path."""
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
                          max_children=max_children, top_n=top_n)
    out_path = os.path.abspath(output)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(report)
    log(f"Report written to {out_path}")
    return out_path


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="TreeSize-style local disk usage report (animated treemap + tables + tree).")
    parser.add_argument("path", nargs="?", help="Top-level directory to scan (omit to launch the GUI)")
    parser.add_argument("-o", "--output", default="diskmap_report.html",
                        help="Output HTML file (default: diskmap_report.html)")
    parser.add_argument("--exclude", action="append", default=[],
                        help="Glob to exclude, matched on name or full path (repeatable)")
    parser.add_argument("--max-tree-depth", type=int, default=None,
                        help="Limit tree listing depth (default: unlimited)")
    parser.add_argument("--treemap-depth", type=int, default=4,
                        help="Folder levels the treemap draws at once (default: 4)")
    parser.add_argument("--max-children", type=int, default=400,
                        help="Max entries listed per folder before collapsing (default: 400)")
    parser.add_argument("--top", type=int, default=100,
                        help="Rows in the largest-files tables (default: 100)")
    parser.add_argument("--min-age-days", type=int, default=365,
                        help="Age threshold for the large-and-untouched table (default: 365)")
    parser.add_argument("--gui", action="store_true", help="Launch the GUI even if a path is given")
    parser.add_argument("--open", action="store_true", help="Open the report when it is finished")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    if args.gui or args.path is None:
        from diskmap_gui import launch
        return launch(args)

    try:
        out_path = run_report(
            args.path, args.output, args.exclude, args.max_tree_depth,
            args.treemap_depth, args.max_children, args.top, args.min_age_days,
            progress=lambda files, dirs, path: print(
                f"  {files:,} files / {dirs:,} folders ...", end="\r", flush=True),
        )
    except OSError as exc:  # includes NotADirectoryError
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
