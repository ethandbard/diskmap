# diskmap

A local, offline TreeSize-style disk usage tool. It scans a directory and writes a single self-contained HTML report with:

1. An animated, zoomable treemap with search, colour modes, and highlighting.
2. Tables of the largest files, the biggest file types, and large files that haven't been touched in a while.
3. A tree listing of every folder and file, annotated with size, that expands as you open it.

Nothing leaves your machine. The report has no external dependencies and opens fully offline.

## Requirements

- Python 3.8 or later. No third-party packages.

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
```

### Options

| Option | Default | Description |
| --- | --- | --- |
| `path` | | Directory to scan. Omit to launch the GUI. |
| `-o`, `--output` | `diskmap_report.html` | Output HTML file. |
| `--exclude GLOB` | | Glob to exclude, matched on name or full path. Repeatable. |
| `--max-tree-depth N` | unlimited | Limit the depth of the data written to the report. |
| `--treemap-depth N` | `4` | Folder levels the treemap draws at once. |
| `--max-children N` | `400` | Max entries kept per folder; the rest are grouped into one "more" block. |
| `--top N` | `100` | Rows in the largest-files tables. |
| `--min-age-days N` | `365` | Age threshold for the large-and-untouched table. |
| `--gui` | off | Launch the GUI even if a path is given. |
| `--open` | off | Open the report in your browser when it finishes. |

`.git`, `node_modules`, `__pycache__`, `.venv`, `venv`, and `.DS_Store` are excluded by default.

## Using the report

| Action | What it does |
| --- | --- |
| Click a tile | Zooms into that folder, or one level toward a file. Clicking a file directly in the current folder selects it. |
| Right-click, Esc, or Backspace | Zooms out one level. |
| Breadcrumbs | Jump back to any folder above the current one. |
| Type / Age / Folder | Recolours the map by file type, by last-modified age, or by top-level folder. |
| Legend chip, or a row in **By file type** | Highlights that kind of file and dims everything else. Esc clears it. |
| `/` | Focuses search. Matches pulse in the map; Enter or a click flies to the result. |
| Rows in the tables and the tree | Hover to outline the item in the map; click (or ◎ in the tree) to fly to it. |
| Inspector panel | Shows the current folder or selected item, its make-up, and its largest children. Hover a child to outline it. |

## Files

- `diskmap.py`: scanner, report generator, and CLI entry point.
- `report_template.html`: the report page. `diskmap.py` fills it with the scan as one JSON payload, and the page draws the treemap, tables, and tree itself.
- `diskmap_gui.py`: Tkinter front end. The scan runs on a worker thread so the window stays responsive and Cancel works.
- `diskmap.cmd`: Windows double-click launcher.

## Notes

Reports are mostly the scan data, about 35 bytes per file or folder. A 236,000-file scan comes to roughly 8 MB.
