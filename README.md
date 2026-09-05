# diskmap

A local, offline TreeSize-style disk usage tool. It scans a directory and writes a single self-contained HTML report with:

1. An interactive file-size treemap. Click a folder to zoom in.
2. Tables of the largest files, the biggest file types, and large files that haven't been touched in a while.
3. A collapsible tree listing of every folder and file, annotated with size.

Nothing leaves your machine. By default, plotly.js is bundled into the report so it opens fully offline.

## Requirements

- Python 3.8 or later
- [plotly](https://plotly.com/python/)

```
pip install plotly
```

On externally managed Linux environments you might need `pip install plotly --break-system-packages`.

## Usage

Launch the GUI:

```
python diskmap.py
```

On Windows, double-click `diskmap.cmd` to launch the GUI without a console window.

Scan from the command line:

```
python diskmap.py /path/to/folder
python diskmap.py /path/to/folder -o report.html --open
python diskmap.py /path/to/folder --exclude node_modules --exclude "*/cache/*"
python diskmap.py /path/to/folder --max-tree-depth 4
python diskmap.py /path/to/folder --cdn
```

### Options

| Option | Default | Description |
| --- | --- | --- |
| `path` | | Directory to scan. Omit to launch the GUI. |
| `-o`, `--output` | `diskmap_report.html` | Output HTML file. |
| `--exclude GLOB` | | Glob to exclude, matched on name or full path. Repeatable. |
| `--max-tree-depth N` | unlimited | Limit the depth of the tree listing. |
| `--treemap-depth N` | `6` | Limit treemap depth for performance. |
| `--max-children N` | `400` | Max entries listed per folder before collapsing. |
| `--top N` | `100` | Rows in the largest-files tables. |
| `--min-age-days N` | `365` | Age threshold for the large-and-untouched table. |
| `--cdn` | off | Load plotly.js from a CDN instead of inlining it. Much smaller file, but needs internet to view. |
| `--gui` | off | Launch the GUI even if a path is given. |
| `--open` | off | Open the report in your browser when it finishes. |

`.git`, `node_modules`, `__pycache__`, `.venv`, `venv`, and `.DS_Store` are excluded by default.

## Files

- `diskmap.py`: scanner, report generator, and CLI entry point.
- `diskmap_gui.py`: Tkinter front end. The scan runs on a worker thread so the window stays responsive and Cancel works.
- `diskmap.cmd`: Windows double-click launcher.

## Notes

With plotly inlined, reports for large trees can reach hundreds of megabytes. Use `--cdn` if you need a small file and will have internet access when viewing it.
