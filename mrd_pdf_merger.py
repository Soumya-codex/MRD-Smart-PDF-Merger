import re
import os
import queue
import tempfile
import threading
import uuid
import tkinter as tk
from difflib import SequenceMatcher
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from collections import defaultdict

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:
    PdfReader = PdfWriter = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_FILES = None
    TkinterDnD = None
    DND_AVAILABLE = False

try:
    from send2trash import send2trash
except ImportError:
    send2trash = None


# Words commonly found in filenames that describe document type, not patient identity.
DOC_LABELS = {
    "COVER", "SCAN", "SCANNED", "COPY", "FINAL", "PDF", "REPORT",
    "DOCUMENT", "DOC", "DISCHARGE", "SUMMARY", "TUMOUR", "TUMOR", "BOARD",
    "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9"
}
GENERIC_PREFIXES = {"BABY", "OF"}
DOCUMENT_PHRASES = (
    "DISCHARGE SUMMARY", "TUMOUR BOARD", "TUMOR BOARD",
)


def get_name_tokens(path_or_name):
    """Extract patient-name tokens while removing known document labels/suffixes."""
    stem = Path(path_or_name).stem.upper()

    for phrase in DOCUMENT_PHRASES:
        stem = re.sub(rf"\b{phrase}\b", " ", stem)

    # Numbered document labels and identifiers/dates are not patient names.
    stem = re.sub(r"\b(?:S\d+|SCAN(?:NED)?\d+)\b", " ", stem)
    stem = re.sub(r"\b[A-Z0-9]*\d[A-Z0-9]*\b", " ", stem)
    stem = re.sub(r"\b\d+\b", " ", stem)
    stem = re.sub(r"[^A-Z]+", " ", stem)
    tokens = re.findall(r"[A-Z]+", stem)
    tokens = [t for t in tokens if t not in DOC_LABELS]
    while tokens and tokens[0] in GENERIC_PREFIXES:
        tokens.pop(0)
    return tokens


def patient_key(path):
    return tuple(get_name_tokens(path.name))


def make_groups(files):
    """Create exact groups plus high-confidence, review-only fuzzy suggestions."""
    buckets = defaultdict(list)
    for p in files:
        key = patient_key(p)
        if key:
            buckets[key].append(p)
        else:
            # Files with no usable alphabetic name remain individually visible.
            buckets[("__UNMATCHED__", p.name)].append(p)

    groups = []
    matched_keys = set()
    named_keys = [key for key in buckets if key[0] != "__UNMATCHED__"]

    # Exact complete cleaned names are grouped without relying on folder names.
    for key in named_keys:
        if len(buckets[key]) > 1:
            groups.append({
                "key": key,
                "files": sorted(buckets[key], key=lambda path: (path.parent.as_posix().lower(), path.name.lower())),
                "level": "Exact Match",
                "confidence": 1.0,
            })
            matched_keys.add(key)

    # Fuzzy comparison is only used for complete names with at least two tokens.
    # A mutual-best thresholded pair is suggested for review, never auto-confirmed.
    singleton_keys = [key for key in named_keys if key not in matched_keys]
    similarities = {}
    for first_index, first_key in enumerate(singleton_keys):
        first_name = " ".join(first_key)
        if len(first_key) < 2:
            continue
        for second_key in singleton_keys[first_index + 1:]:
            if len(second_key) < 2:
                continue
            second_name = " ".join(second_key)
            score = SequenceMatcher(None, first_name, second_name).ratio()
            token_scores = [
                SequenceMatcher(None, left, right).ratio()
                for left, right in zip(first_key, second_key)
            ]
            changed_tokens = [
                (left, right, token_score)
                for left, right, token_score in zip(first_key, second_key, token_scores)
                if left != right
            ]
            safe_single_typo = (
                len(changed_tokens) == 1
                and min(len(changed_tokens[0][0]), len(changed_tokens[0][1])) >= 4
                and changed_tokens[0][2] >= 0.78
            )
            if score >= 0.84 and len(first_key) == len(second_key) and safe_single_typo:
                similarities[first_key, second_key] = score

    best_for_key = {}
    for (first_key, second_key), score in similarities.items():
        best_for_key[first_key] = max(score, best_for_key.get(first_key, 0))
        best_for_key[second_key] = max(score, best_for_key.get(second_key, 0))
    for (first_key, second_key), score in similarities.items():
        if score != best_for_key[first_key] or score != best_for_key[second_key]:
            continue
        if first_key in matched_keys or second_key in matched_keys:
            continue
        files_for_pair = sorted(
            buckets[first_key] + buckets[second_key],
            key=lambda path: (path.parent.as_posix().lower(), path.name.lower()),
        )
        groups.append({
            "key": first_key,
            "files": files_for_pair,
            "level": f"Possible Match ({score:.0%}) — Review Required",
            "confidence": score,
        })
        matched_keys.update((first_key, second_key))

    # Keep all remaining files visible as individual review cards; never match
    # on the first name alone or assume that a singleton belongs to another group.
    for key, items in buckets.items():
        if key[0] == "__UNMATCHED__":
            display_key = (items[0].stem.upper(),)
            groups.append({"key": display_key, "files": items, "level": "Review Required", "confidence": 0.0})
        elif key not in matched_keys:
            groups.append({
                "key": key,
                "files": sorted(items, key=lambda path: (path.parent.as_posix().lower(), path.name.lower())),
                "level": "Review Required",
                "confidence": 0.0,
            })

    groups.sort(key=lambda group: " ".join(group["key"]).lower())
    return groups


def validate_page_range(start, end, total):
    """Return a useful validation error, or None for a valid inclusive range."""
    if start < 1 or end < start or end > total:
        return f"page range must be between 1 and {total}"
    return None


def cleanup_paths_from_successes(successes):
    """Return unique source paths included in completed merged outputs only."""
    candidates = {}
    seen = set()
    for result in successes:
        for path in result[1]:
            resolved_path = str(path.resolve()).casefold()
            if resolved_path not in seen:
                candidates[str(path)] = path
                seen.add(resolved_path)
    return candidates


def accumulate_cleanup_candidates(existing, successes):
    """Keep prior successful sources and append new unique successful sources."""
    candidates = dict(existing)
    seen = {str(path.resolve()).casefold() for path in candidates.values()}
    for path in cleanup_paths_from_successes(successes).values():
        key = str(path.resolve()).casefold()
        if key not in seen:
            candidates[str(path)] = path
            seen.add(key)
    return candidates


def move_cleanup_sources_to_trash(candidates, trash_function):
    """Move only listed cleanup sources, returning successful paths and errors."""
    moved = []
    failures = []
    for path in candidates:
        if not path.is_file():
            failures.append((path, "file is missing or is not a regular file"))
            continue
        try:
            trash_function(str(path))
            moved.append(path)
        except Exception as exc:
            failures.append((path, str(exc)))
    return moved, failures


def assign_source_to_group(source, groups, target_group):
    """Move a source path into one group while preventing duplicate assignment."""
    source_key = str(source.resolve()).casefold()
    if any(str(path.resolve()).casefold() == source_key for path in target_group["files"]):
        return False
    for group in groups:
        if group is target_group:
            continue
        group["files"] = [
            path for path in group["files"]
            if str(path.resolve()).casefold() != source_key
        ]
    target_group["files"].append(source)
    return True


def merge_pdf_selection(selection, output_dir, group_name, progress_callback=None):
    """Validate and merge (path, inclusive-start, inclusive-end) rows atomically."""
    if PdfReader is None or PdfWriter is None:
        raise RuntimeError("pypdf is not installed")
    if not selection:
        raise ValueError("No checked PDFs are available to merge.")

    validated = []
    validated_paths = []
    output_path = output_dir.resolve()
    for path, start, end in selection:
        if not path.is_file():
            raise FileNotFoundError(f"Source PDF not found: {path}")
        source_path = path.resolve()
        if source_path.parent == output_path or output_path in source_path.parents:
            raise ValueError(
                f"Output folder must be separate from original PDFs: {output_path}"
            )
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            raise ValueError(f"{path.name}: password-protected PDF")
        total = len(reader.pages)
        range_error = validate_page_range(start, end, total)
        if range_error:
            raise ValueError(f"{path.name}: {range_error}")
        validated.append((path, reader, start, end))
        validated_paths.append(path)

    writer = PdfWriter()
    total_selected_pages = sum(end - start + 1 for _path, _reader, start, end in validated)
    completed_pages = 0
    for _path, reader, start, end in validated:
        for page_index in range(start - 1, end):
            writer.add_page(reader.pages[page_index])
            completed_pages += 1
            if progress_callback:
                progress_callback(completed_pages, total_selected_pages)

    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[<>:"/\\\\|?*]', "_", group_name).strip(" .")
    if not safe_name or safe_name == "__UNMATCHED__":
        safe_name = "Manual_Group"

    fd, temp_name = tempfile.mkstemp(prefix=".mrd-merge-", suffix=".tmp", dir=output_dir)
    os.close(fd)
    temp_path = Path(temp_name)
    reserved_output = None
    try:
        with temp_path.open("wb") as handle:
            writer.write(handle)
            handle.flush()
            os.fsync(handle.fileno())

        suffix = 1
        while True:
            name_suffix = "" if suffix == 1 else f"_{suffix}"
            candidate = output_dir / f"{safe_name}_MERGED{name_suffix}.pdf"
            try:
                # Exclusive creation prevents overwriting even if another run races us.
                with candidate.open("xb"):
                    pass
                reserved_output = candidate
                break
            except FileExistsError:
                suffix += 1

        os.replace(temp_path, reserved_output)
        return reserved_output, validated_paths
    except Exception:
        if reserved_output is not None:
            try:
                reserved_output.unlink()
            except OSError:
                pass
        raise
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


class MRDPDFMerger:
    def __init__(self, root):
        self.root = root
        self.root.title("MRD Smart PDF Merger")
        self.root.geometry("1280x820")
        self.root.minsize(1000, 640)

        self.files = []
        self.source_roots = []
        self.source_directories = set()
        self._tree_file_paths = {}
        self._tree_folder_paths = {}
        self.page_counts = {}
        self._auto_unassigned = set()
        self.groups = []
        self.group_widgets = []
        self.manual_groups = []
        self.manual_group_widgets = []
        self._merge_queue = queue.Queue()
        self._merge_running = False
        self.dnd_enabled = False
        self.output_dir = None
        # Candidates accumulate through the current source-processing session.
        self.cleanup_candidates = {}
        self._build_ui()
        self._enable_drag_drop()
        self.rebuild_groups(recompute=False)

    def _build_ui(self):
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10))
        style.configure("TFrame", background="#f3f6fa")
        style.configure("TLabel", background="#f3f6fa", foreground="#203047")
        style.configure("TLabelframe", background="#ffffff", bordercolor="#d9e2ef")
        style.configure("TLabelframe.Label", background="#ffffff", foreground="#17365d",
                        font=("Segoe UI", 10, "bold"))
        style.configure("TButton", padding=(9, 6), background="#e8eef7", foreground="#17365d")
        style.map("TButton", background=[("active", "#d9e6f5")])
        style.configure("Small.TButton", padding=(4, 2), font=("Segoe UI", 9))
        style.configure("SmallPrimary.TButton", padding=(4, 2), font=("Segoe UI", 9),
                        background="#1f5f9f", foreground="#ffffff")
        style.map("SmallPrimary.TButton", background=[("active", "#174b80")])
        style.configure("Primary.TButton", background="#1f5f9f", foreground="#ffffff")
        style.map("Primary.TButton", background=[("active", "#174b80")])
        style.configure("Danger.TButton", foreground="#a52b2b")
        style.configure("Treeview", rowheight=27, font=("Segoe UI", 9),
                        background="#ffffff", fieldbackground="#ffffff")
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"),
                        background="#e8eef7", foreground="#17365d")
        self.root.configure(background="#f3f6fa")

        top = ttk.Frame(self.root, padding=12)
        top.pack(fill="x")

        ttk.Label(top, text="MRD Smart PDF Merger",
                  font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(
            top,
            text="Review automatic patient matches or create manual groups, set page ranges and order, "
                 "then create a separate PDF for each group."
        ).pack(anchor="w", pady=(3, 10))

        toolbar = ttk.Frame(top)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Add Files", command=self.add_files).pack(side="left")
        ttk.Button(toolbar, text="Add Folder", command=self.add_folder).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Clear", command=self.clear_all).pack(side="left", padx=8)

        self.file_count_label = ttk.Label(toolbar, text="0 PDFs loaded")
        self.file_count_label.pack(side="left", padx=(12, 4))
        self.total_pages_var = tk.StringVar(value="0 pages")
        ttk.Label(toolbar, textvariable=self.total_pages_var).pack(side="left", padx=6)

        self.output_var = tk.StringVar(value="Merged_PDFs (inside selected folder)")
        ttk.Button(toolbar, text="Output Folder", command=self.choose_output).pack(side="right")

        self.drop_area = tk.Label(
            self.root,
            text="Drag and drop PDF files or a folder here\n(or use Add Files / Add Folder)",
            relief="groove", bd=1, padx=8, pady=10,
            font=("Segoe UI", 10), fg="#555"
        )
        self.drop_area.pack(fill="x", padx=12, pady=(0, 8))
        if not DND_AVAILABLE:
            self.drop_area.config(
                text="Drag and drop support needs tkinterdnd2. Use Add Files / Add Folder, "
                     "or install requirements.txt to enable drag and drop."
            )

        # Main workspace: left source/cleanup panel and right patient matching cards.
        bottom = ttk.Frame(self.root, padding=(12, 8), relief="solid")
        bottom.pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value="Add PDF files or select a folder to begin.")
        ttk.Label(bottom, textvariable=self.status_var, wraplength=280).pack(
            side="left", fill="x", expand=True
        )
        ttk.Label(bottom, textvariable=self.output_var, foreground="#5c6b7a",
                  wraplength=220).pack(side="left", padx=8)
        self.merge_progress = ttk.Progressbar(bottom, mode="determinate", length=120, maximum=100)
        self.merge_progress.pack(side="right", padx=(8, 10))
        self.merge_all_button = ttk.Button(
            bottom, text="Merge All Groups", style="Primary.TButton", command=self.merge_all
        )
        self.merge_all_button.pack(side="right")

        workspace = ttk.Panedwindow(self.root, orient="horizontal")
        workspace.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        left_panel = ttk.Frame(workspace, padding=(0, 0, 8, 0))
        right_panel = ttk.Frame(workspace)
        workspace.add(left_panel, weight=2)
        workspace.add(right_panel, weight=3)
        # Set the initial divider near the requested 40/60 proportion.
        self.root.after(250, lambda: workspace.sashpos(0, int(self.root.winfo_width() * 0.4)))

        # Left upper section: folder hierarchy and every discovered source PDF.
        all_box = ttk.LabelFrame(left_panel, text="Original PDFs by folder", padding=7)
        all_box.pack(fill="both", expand=True, pady=(0, 7))
        self.all_count_var = tk.StringVar(value="0 files")
        ttk.Label(all_box, textvariable=self.all_count_var, foreground="#555").pack(anchor="w")
        all_list_wrap = ttk.Frame(all_box)
        all_list_wrap.pack(fill="both", expand=True, pady=(5, 0))
        all_list_wrap.rowconfigure(0, weight=1)
        all_list_wrap.columnconfigure(0, weight=1)

        style.configure("SourceFiles.Treeview", rowheight=27, font=("Segoe UI", 9))
        style.configure("SourceFiles.Treeview.Heading", font=("Segoe UI", 9, "bold"))
        self.all_files_list = ttk.Treeview(
            all_list_wrap,
            columns=("source",),
            show="tree headings",
            height=12,
            style="SourceFiles.Treeview"
        )
        self.all_files_list.heading("#0", text="Folder / PDF")
        self.all_files_list.heading("source", text="Source folder")
        self.all_files_list.column("#0", width=290, minwidth=170, stretch=True)
        self.all_files_list.column("source", width=260, minwidth=150, stretch=True)
        self.all_files_list.bind("<<TreeviewSelect>>", self._on_source_selection)

        all_vscroll = ttk.Scrollbar(
            all_list_wrap, orient="vertical", command=self.all_files_list.yview
        )
        all_hscroll = ttk.Scrollbar(
            all_list_wrap, orient="horizontal", command=self.all_files_list.xview
        )
        self.all_files_list.configure(
            yscrollcommand=all_vscroll.set,
            xscrollcommand=all_hscroll.set
        )
        self.all_files_list.grid(row=0, column=0, sticky="nsew")
        all_vscroll.grid(row=0, column=1, sticky="ns")
        all_hscroll.grid(row=1, column=0, sticky="ew")
        ttk.Label(
            all_box,
            text="Select a PDF leaf, then use “Add selected from left” on a group.",
            foreground="#555", wraplength=480
        ).pack(anchor="w", pady=(5, 0))
        self.selected_source_var = tk.StringVar(value="No source PDF selected.")
        ttk.Label(all_box, textvariable=self.selected_source_var, foreground="#17365d",
                  wraplength=500).pack(anchor="w", pady=(3, 0))

        # Left lower section: cleanup candidates from successful merges only.
        cleanup_box = ttk.LabelFrame(left_panel, text="Original PDFs ready for cleanup", padding=7)
        cleanup_box.pack(fill="both", expand=True)
        ttk.Label(
            cleanup_box,
            text="Only PDFs used in successfully created merged files appear here.",
            foreground="#555", wraplength=480
        ).pack(anchor="w")
        cleanup_tools = ttk.Frame(cleanup_box)
        cleanup_tools.pack(fill="x", pady=(5, 2))
        ttk.Button(cleanup_tools, text="Select all", command=lambda: self.set_cleanup_checks(True)).pack(side="left")
        ttk.Button(cleanup_tools, text="Select none", command=lambda: self.set_cleanup_checks(False)).pack(side="left", padx=5)
        self.cleanup_clear_button = ttk.Button(
            cleanup_tools, text="Clean all",
            style="Danger.TButton", command=self.cleanup_all_originals
        )
        self.cleanup_clear_button.pack(side="left")

        cleanup_list_wrap = ttk.Frame(cleanup_box)
        cleanup_list_wrap.pack(fill="both", expand=True)
        self.cleanup_canvas = tk.Canvas(
            cleanup_list_wrap, highlightthickness=0, height=150, background="#f3f6fa"
        )
        cleanup_scroll = ttk.Scrollbar(cleanup_list_wrap, orient="vertical", command=self.cleanup_canvas.yview)
        self.cleanup_canvas.configure(yscrollcommand=cleanup_scroll.set)
        self.cleanup_canvas.pack(side="left", fill="both", expand=True)
        cleanup_scroll.pack(side="right", fill="y")
        self.cleanup_rows_frame = ttk.Frame(self.cleanup_canvas)
        self.cleanup_canvas_window = self.cleanup_canvas.create_window(
            (0, 0), window=self.cleanup_rows_frame, anchor="nw"
        )
        self.cleanup_rows_frame.bind(
            "<Configure>",
            lambda _e: self.cleanup_canvas.configure(scrollregion=self.cleanup_canvas.bbox("all"))
        )
        self.cleanup_canvas.bind(
            "<Configure>",
            lambda e: self.cleanup_canvas.itemconfigure(self.cleanup_canvas_window, width=e.width)
        )
        self.cleanup_empty_var = tk.StringVar(value="No cleanup candidates yet. Merge PDFs first.")
        self.cleanup_empty_label = ttk.Label(cleanup_box, textvariable=self.cleanup_empty_var, foreground="#666")
        self.cleanup_empty_label.pack(anchor="w", pady=(3, 5))
        self.cleanup_button = ttk.Button(
            cleanup_box, text="Move selected to Recycle Bin",
            command=self.cleanup_selected_originals
        )
        self.cleanup_button.pack(fill="x", pady=(0, 4))
        # Right side: scrollable patient cards (same matching and merge controls as before).
        cards_outer = ttk.Frame(right_panel)
        cards_outer.pack(fill="both", expand=True)
        cards_outer.rowconfigure(0, weight=1)
        cards_outer.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(cards_outer, highlightthickness=0, background="#f3f6fa")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(cards_outer, orient="vertical", command=self.canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal_scroll = ttk.Scrollbar(cards_outer, orient="horizontal", command=self.canvas.xview)
        horizontal_scroll.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal_scroll.set)
        self.cards_frame = ttk.Frame(self.canvas)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.cards_frame, anchor="nw")
        self.cards_frame.bind("<Configure>", self._update_scroll)
        self.canvas.bind("<Configure>", self._resize_cards)

        self._install_wheel_scrolling()

    def _enable_drag_drop(self):
        if not DND_AVAILABLE:
            return
        try:
            self.all_files_list.drag_source_register(1, DND_FILES)
            self.all_files_list.dnd_bind("<<DragInitCmd>>", self._begin_source_drag)
            self.drop_area.drop_target_register(DND_FILES)
            self.drop_area.dnd_bind("<<Drop>>", self._on_drop)
            self.canvas.drop_target_register(DND_FILES)
            self.canvas.dnd_bind("<<Drop>>", self._on_drop)
            self.dnd_enabled = True
        except Exception as exc:
            self.dnd_enabled = False
            self.drop_area.config(
                text="Drag and drop is unavailable in this window. Use Add Files / Add Folder "
                     "and the select-and-add controls."
            )
            self.status_var.set(f"Drag-and-drop could not be enabled: {exc}")

    def _on_drop(self, event):
        try:
            paths = self.root.tk.splitlist(event.data)
            self._add_paths([Path(p) for p in paths])
        except Exception as exc:
            messagebox.showerror("Drop error", str(exc))

    def _update_scroll(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize_cards(self, event):
        self.canvas.itemconfigure(
            self.canvas_window,
            width=max(event.width, self.cards_frame.winfo_reqwidth())
        )

    def add_files(self):
        selected = filedialog.askopenfilenames(
            title="Select PDF files",
            filetypes=[("PDF files", "*.pdf")]
        )
        if selected:
            self._add_paths([Path(p) for p in selected])

    def add_folder(self):
        selected = filedialog.askdirectory(title="Select folder containing PDFs")
        if selected:
            # Recursive scan includes PDFs in nested/date-wise folders.
            self._add_paths([Path(selected)])

    @staticmethod
    def _path_key(path):
        return str(path.resolve()).casefold()

    def _register_source_root(self, root):
        root = root.resolve()
        containing_root = next(
            (existing for existing in self.source_roots if existing == root or existing in root.parents),
            None
        )
        if containing_root is not None:
            current = root
            while True:
                self.source_directories.add(current)
                if current == containing_root or current.parent == current:
                    break
                current = current.parent
            return
        self.source_roots = [
            existing for existing in self.source_roots if root not in existing.parents
        ]
        self.source_roots.append(root)
        self.source_directories.add(root)

    def _add_paths(self, paths):
        found = []
        scan_errors = []
        output_path = self.output_dir.resolve() if self.output_dir else None
        for path in paths:
            if path.is_dir():
                try:
                    is_output_root = output_path is not None and path.resolve() == output_path
                except OSError:
                    is_output_root = False
                if path.name.lower() == "merged_pdfs" or is_output_root:
                    continue
                self._register_source_root(path)
                for current, directories, filenames in os.walk(
                    path, onerror=lambda error: scan_errors.append(str(error))
                ):
                    current_path = Path(current)
                    self.source_directories.add(current_path.resolve())
                    kept_directories = []
                    for directory in directories:
                        candidate = current_path / directory
                        try:
                            is_output = output_path is not None and candidate.resolve() == output_path
                        except OSError:
                            is_output = False
                        if directory.lower() != "merged_pdfs" and not is_output:
                            kept_directories.append(directory)
                            self.source_directories.add(candidate.resolve())
                    directories[:] = kept_directories
                    found.extend(
                        current_path / filename for filename in filenames
                        if Path(filename).suffix.lower() == ".pdf"
                    )
            elif path.is_file() and path.suffix.lower() == ".pdf":
                found.append(path)
                self._register_source_root(path.parent)
            else:
                scan_errors.append(f"Not a readable PDF or folder: {path}")

        existing = {p.resolve() for p in self.files}
        added = 0
        for p in found:
            try:
                resolved = p.resolve()
            except OSError:
                resolved = p
            if any(parent.name.lower() == "merged_pdfs" for parent in resolved.parents):
                continue
            if output_path is not None and (resolved == output_path or output_path in resolved.parents):
                continue
            if resolved not in existing:
                self.files.append(p)
                existing.add(resolved)
                added += 1

        self.files.sort(key=lambda p: (p.parent.as_posix().lower(), p.name.lower()))
        if self.files and self.output_dir is None:
            # Default output is a Merged_PDFs folder next to the first imported source.
            self.output_dir = self.files[0].parent / "Merged_PDFs"
            self.output_var.set(str(self.output_dir))
        self.rebuild_groups()
        self.file_count_label.config(text=f"{len(self.files)} PDFs loaded ({added} newly added)")
        self._update_total_page_count()
        status = (f"Loaded {len(self.files)} PDF(s). "
                  f"{len(self.groups)} automatic patient group(s)/unmatched item(s) found.")
        if scan_errors:
            status += f" {len(scan_errors)} path(s) could not be added."
            messagebox.showwarning("Some paths were not added", "\n".join(scan_errors[:10]))
        self.status_var.set(status)

    def choose_output(self):
        if self._merge_running:
            messagebox.showwarning("Merge in progress", "Wait for merging to finish before changing the output folder.")
            return
        chosen = filedialog.askdirectory(title="Choose output folder")
        if chosen:
            output_path = Path(chosen).resolve()
            overlapping_sources = [
                source for source in self.files
                if source.resolve() == output_path
                or source.parent.resolve() == output_path
                or output_path in source.resolve().parents
            ]
            if overlapping_sources:
                messagebox.showerror(
                    "Choose a separate output folder",
                    "The output folder must not contain original PDFs. Choose a separate folder "
                    "(for example, a Merged_PDFs subfolder)."
                )
                return
            self.output_dir = output_path
            self.output_var.set(str(self.output_dir))

    def clear_all(self):
        if self._merge_running:
            messagebox.showwarning("Merge in progress", "Wait for the current merge operation to finish.")
            return
        self.files.clear()
        self.source_roots.clear()
        self.source_directories.clear()
        self.page_counts.clear()
        self._auto_unassigned.clear()
        self.groups.clear()
        self.manual_groups.clear()
        self.cleanup_candidates.clear()
        self.output_dir = None
        self.output_var.set("Merged_PDFs (inside selected folder)")
        self.file_count_label.config(text="0 PDFs loaded")
        self.total_pages_var.set("0 pages")
        self.rebuild_groups()
        self.status_var.set("Cleared. Add PDF files or a folder to begin.")

    def _install_wheel_scrolling(self):
        self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-4>", lambda event: self._on_mousewheel(event, -1), add="+")
        self.root.bind_all("<Button-5>", lambda event: self._on_mousewheel(event, 1), add="+")

    def _on_mousewheel(self, event, fallback_direction=None):
        pointer_x = getattr(event, "x_root", 0)
        pointer_y = getattr(event, "y_root", 0)
        widget_path = str(getattr(event, "widget", ""))
        tree_prefix = str(self.all_files_list)
        if widget_path == tree_prefix or widget_path.startswith(tree_prefix + "."):
            target = self.all_files_list
        elif self._point_inside(self.cleanup_canvas, pointer_x, pointer_y):
            target = self.cleanup_canvas
        elif self._point_inside(self.canvas, pointer_x, pointer_y):
            target = self.canvas
        else:
            return None

        delta = getattr(event, "delta", 0)
        if fallback_direction is not None:
            units = fallback_direction
        elif delta:
            units = int(-delta / 120)
            if units == 0:
                units = -1 if delta > 0 else 1
        else:
            return None
        target.yview_scroll(units, "units")
        return "break"

    @staticmethod
    def _point_inside(widget, x, y):
        if not widget.winfo_ismapped():
            return False
        left, top = widget.winfo_rootx(), widget.winfo_rooty()
        return left <= x <= left + widget.winfo_width() and top <= y <= top + widget.winfo_height()

    def refresh_all_files_list(self):
        selected_path = None
        current_selection = self.all_files_list.selection()
        if current_selection and current_selection[0] in self._tree_file_paths:
            selected_path = self._tree_file_paths[current_selection[0]]
        open_folders = {
            self._tree_folder_paths[iid]
            for iid in self._tree_folder_paths
            if self.all_files_list.item(iid, "open")
        }
        had_tree = bool(self._tree_folder_paths)
        for item in self.all_files_list.get_children():
            self.all_files_list.delete(item)
        self._tree_file_paths = {}
        self._tree_folder_paths = {}
        folder_ids = {}
        next_folder_id = 0

        roots = sorted(self.source_roots, key=lambda path: (len(path.parts), str(path).casefold()))
        if not roots:
            roots = sorted({path.parent.resolve() for path in self.files}, key=lambda path: str(path).casefold())

        def file_count(folder):
            return sum(
                1 for path in self.files
                if path.resolve().parent == folder or folder in path.resolve().parents
            )

        all_directories = set(self.source_directories)
        all_directories.update(path.parent.resolve() for path in self.files)
        for root in roots:
            all_directories.add(root)
        for folder in sorted(all_directories, key=lambda path: (len(path.parts), str(path).casefold())):
            containing_roots = [
                root for root in roots if folder == root or root in folder.parents
            ]
            if not containing_roots:
                continue
            root = max(containing_roots, key=lambda path: len(path.parts))
            if folder == root:
                parent_iid = ""
                label = folder.name or str(folder)
            else:
                parent_iid = folder_ids.get(folder.parent, "")
                label = folder.name
            count = file_count(folder)
            iid = f"dir:{next_folder_id}"
            next_folder_id += 1
            display = f"{label} ({count} PDF{'s' if count != 1 else ''})"
            self.all_files_list.insert(
                parent_iid, "end", iid=iid, text=display, values=(str(folder),),
                open=(folder in open_folders if had_tree else True)
            )
            folder_ids[folder] = iid
            self._tree_folder_paths[iid] = folder

        for index, path in enumerate(self.files):
            parent = path.parent.resolve()
            parent_iid = folder_ids.get(parent, "")
            iid = f"file:{index}"
            self.all_files_list.insert(
                parent_iid, "end", iid=iid, text=path.name, values=(str(path.parent),)
            )
            self._tree_file_paths[iid] = path
        if selected_path is not None:
            selected_iid = next(
                (iid for iid, path in self._tree_file_paths.items()
                 if self._same_path(path, selected_path)),
                None
            )
            if selected_iid:
                self.all_files_list.selection_set(selected_iid)
        self.all_count_var.set(f"{len(self.files)} file(s) found")
        if not self.all_files_list.selection():
            self.selected_source_var.set("No source PDF selected.")

    def _on_source_selection(self, _event=None):
        selection = self.all_files_list.selection()
        if not selection:
            self.selected_source_var.set("No source PDF selected.")
            return
        if selection[0] not in self._tree_file_paths:
            folder = self._tree_folder_paths.get(selection[0])
            self.selected_source_var.set(f"Folder: {folder}" if folder else "No source PDF selected.")
            return
        path = self._tree_file_paths[selection[0]]
        self.selected_source_var.set(f"Selected: {path.name}  |  {path.parent}")
        if self.dnd_enabled:
            self.all_files_list.drag_source_register(1, DND_FILES)

    def _begin_source_drag(self, _event=None):
        selection = self.all_files_list.selection()
        if not selection or selection[0] not in self._tree_file_paths:
            return None
        path = self._tree_file_paths[selection[0]]
        return ("copy", DND_FILES, self.root.tk.call("list", str(path)))

    def _selected_source(self):
        selection = self.all_files_list.selection()
        if not selection:
            messagebox.showinfo("Select a source PDF", "First select a PDF in the original-file list.")
            return None
        source = self._tree_file_paths.get(selection[0])
        if source is None:
            if selection[0] in self._tree_folder_paths:
                messagebox.showinfo("Select a PDF", "Select an individual PDF inside the folder.")
            else:
                messagebox.showerror("Selection error", "The selected source PDF is no longer available.")
            return None
        if not source.is_file():
            messagebox.showerror("Missing file", f"This source PDF no longer exists:\n{source}")
            return None
        return source

    def _update_total_page_count(self):
        known = [self.page_counts[self._path_key(path)] for path in self.files
                 if self._path_key(path) in self.page_counts]
        total = sum(count or 0 for count in known)
        if len(known) < len(self.files):
            self.total_pages_var.set(f"{total} pages counted")
        else:
            self.total_pages_var.set(f"{total} pages")

    @staticmethod
    def _same_path(first, second):
        try:
            return first.resolve() == second.resolve()
        except OSError:
            return first == second

    def _find_assignment(self, source):
        for group in self.groups:
            if any(self._same_path(path, source) for path in group["files"]):
                return "automatic", group
        for group in self.manual_groups:
            if any(self._same_path(path, source) for path in group["files"]):
                return "manual", group
        return None, None

    @staticmethod
    def _group_label(kind, group):
        if kind == "manual":
            return group["name_var"].get().strip() or "Unnamed manual group"
        return " ".join(group["key"])

    def _assign_file(self, source, target_kind, target_group):
        if not any(self._same_path(path, source) for path in self.files):
            messagebox.showerror("Source not loaded", "Add this PDF to the original-file list first.")
            return False
        existing_kind, previous_group = self._find_assignment(source)
        if previous_group is target_group:
            messagebox.showinfo("Already added", "This PDF is already assigned to this group.")
            return False
        if previous_group is not None:
            old_label = self._group_label(existing_kind, previous_group)
            new_label = self._group_label(target_kind, target_group)
            if not messagebox.askyesno(
                "Move PDF to another group",
                f"This PDF is currently assigned to {old_label}.\n\nMove it to {new_label} instead?"
            ):
                return False
        all_groups = self.groups + self.manual_groups
        if not assign_source_to_group(source, all_groups, target_group):
            messagebox.showinfo("Already added", "This PDF is already assigned to this group.")
            return False
        self.rebuild_groups(recompute=False)
        self.status_var.set(f"Assigned {source.name} to {self._group_label(target_kind, target_group)}.")
        return True

    def add_selected_to_manual(self, group):
        source = self._selected_source()
        if source is not None:
            self._assign_file(source, "manual", group)

    def browse_into_manual(self, group):
        selected = filedialog.askopenfilenames(
            title="Select PDF files for this manual group",
            filetypes=[("PDF files", "*.pdf")]
        )
        for raw_path in selected:
            source = Path(raw_path)
            if not any(self._same_path(source, path) for path in self.files):
                self._add_paths([source])
            self._assign_file(source, "manual", group)

    def create_manual_group(self):
        group = {
            "id": uuid.uuid4().hex,
            "name_var": tk.StringVar(value=f"Manual Group {len(self.manual_groups) + 1}"),
            "files": []
        }
        self.manual_groups.append(group)
        self.rebuild_groups(recompute=False)
        self.status_var.set("Created an empty manual merge group.")

    def delete_manual_group(self, group):
        if group["files"] and not messagebox.askyesno(
            "Delete manual group",
            f"Delete '{self._group_label('manual', group)}' and unassign its "
            f"{len(group['files'])} PDF(s)? Originals will remain in the source list."
        ):
            return
        self.manual_groups = [item for item in self.manual_groups if item is not group]
        self.rebuild_groups(recompute=True)
        self.status_var.set("Manual group deleted; source PDFs were not changed.")

    def remove_from_manual(self, group, source):
        group["files"] = [path for path in group["files"] if not self._same_path(path, source)]
        self.rebuild_groups(recompute=True)
        self.status_var.set(f"Unassigned {source.name}; the original file was not changed.")

    def remove_from_automatic(self, group, source):
        source_key = self._path_key(source)
        group["files"] = [path for path in group["files"] if not self._same_path(path, source)]
        self._auto_unassigned.add(source_key)
        self.rebuild_groups(recompute=True)
        self.status_var.set(f"Removed {source.name} from automatic matching; the original remains loaded.")

    def set_manual_checks(self, group, checked):
        for widget in self.manual_group_widgets:
            if widget["group"] is group:
                for row in widget["rows"]:
                    row["use"].set(checked)
                return

    def _drop_into_group(self, event, kind, group):
        try:
            dropped = [Path(raw) for raw in self.root.tk.splitlist(event.data)]
            for path in dropped:
                if path.is_dir() or path.suffix.lower() != ".pdf":
                    continue
                if not any(self._same_path(path, source) for source in self.files):
                    messagebox.showinfo(
                        "PDF not in source list",
                        "Add PDFs to the master list first, then drag the selected row into a group."
                    )
                    continue
                self._assign_file(path, kind, group)
        except Exception as exc:
            messagebox.showerror("Drop error", f"Could not assign dropped PDF(s): {exc}")

    def refresh_cleanup_panel(self):
        for child in self.cleanup_rows_frame.winfo_children():
            child.destroy()
        self.cleanup_vars = {}
        candidates = list(self.cleanup_candidates.values())
        self.cleanup_empty_var.set(
            f"{len(candidates)} original PDF(s) eligible for cleanup."
            if candidates else "No cleanup candidates yet. Merge PDFs first."
        )
        for path in sorted(candidates, key=lambda p: (p.parent.as_posix().lower(), p.name.lower())):
            var = tk.BooleanVar(value=True)
            self.cleanup_vars[str(path)] = var
            row = ttk.Frame(self.cleanup_rows_frame)
            row.pack(fill="x", anchor="w")
            ttk.Checkbutton(row, variable=var).pack(side="left")
            ttk.Label(row, text=path.name, wraplength=260).pack(side="left", fill="x", expand=True)
            ttk.Label(row, text=str(path.parent), foreground="#777", wraplength=260).pack(
                side="left", padx=(4, 0)
            )
        if hasattr(self, "cleanup_button"):
            self.cleanup_button.config(
                state=("normal" if candidates else "disabled")
            )
            self.cleanup_clear_button.config(
                state=("normal" if candidates else "disabled")
            )

    def set_cleanup_checks(self, checked):
        for var in getattr(self, "cleanup_vars", {}).values():
            var.set(checked)

    def cleanup_selected_originals(self):
        selected = [
            Path(raw) for raw, var in getattr(self, "cleanup_vars", {}).items()
            if var.get()
        ]
        if not selected:
            messagebox.showinfo("Nothing selected", "Select at least one cleanup candidate.")
            return
        if send2trash is None:
            messagebox.showerror(
                "Recycle Bin support missing",
                "Install the required packages with:\npy -m pip install -r requirements.txt"
            )
            return

        existing = [p for p in selected if p.exists() and p.is_file()]
        if not existing:
            messagebox.showinfo("No files found", "The selected source PDFs no longer exist.")
            return
        names = "\n".join(f"{p.name}\n  {p}" for p in existing[:12])
        if len(existing) > 12:
            names += f"\n... and {len(existing) - 12} more"
        if not messagebox.askyesno(
            "Confirm cleanup",
            f"Move {len(existing)} original PDF(s) to the Windows Recycle Bin?\n\n"
            f"{names}\n\nMerged output PDFs will remain untouched."
        ):
            return

        moved, failed = [], []
        for path in existing:
            try:
                send2trash(str(path))
                moved.append(path)
                self.cleanup_candidates.pop(str(path), None)
                # Remove deleted originals from the source list and patient cards.
                self.files = [p for p in self.files if not self._same_path(p, path)]
                self.page_counts.pop(self._path_key(path), None)
                self._auto_unassigned.discard(self._path_key(path))
                for group in self.manual_groups:
                    group["files"] = [
                        source for source in group["files"] if not self._same_path(source, path)
                    ]
            except Exception as exc:
                failed.append(f"{path.name}: {exc}")

        self.rebuild_groups()
        self.file_count_label.config(text=f"{len(self.files)} PDFs loaded")
        self._update_total_page_count()
        message = f"Moved {len(moved)} original PDF(s) to the Recycle Bin."
        if failed:
            message += "\n\nCould not move:\n" + "\n".join(failed[:10])
        self.status_var.set(message)
        messagebox.showinfo("Cleanup finished", message)

    def cleanup_all_originals(self):
        """Confirm and send every listed cleanup source to the Recycle Bin."""
        candidates = list(self.cleanup_candidates.values())
        if not candidates:
            messagebox.showinfo("Cleanup is empty", "There are no original PDFs listed for cleanup.")
            return
        if send2trash is None:
            messagebox.showerror(
                "Recycle Bin support missing",
                "Install the required packages with:\npy -m pip install -r requirements.txt"
            )
            return

        source_list = "\n".join(f"{path.name}\n  {path}" for path in candidates[:12])
        if len(candidates) > 12:
            source_list += f"\n... and {len(candidates) - 12} more"
        if not messagebox.askyesno(
            "Confirm cleanup of all listed originals",
            f"Move all {len(candidates)} original PDF(s) in the Cleanup list "
            "to the Windows Recycle Bin?\n\n"
            f"{source_list}\n\nGenerated merged output PDFs and files not listed "
            "for cleanup will not be moved."
        ):
            return

        moved, failures = move_cleanup_sources_to_trash(candidates, send2trash)
        for path in moved:
            self.cleanup_candidates.pop(str(path), None)
            self.files = [source for source in self.files if not self._same_path(source, path)]
            self.page_counts.pop(self._path_key(path), None)
            self._auto_unassigned.discard(self._path_key(path))
            for group in self.manual_groups:
                group["files"] = [
                    source for source in group["files"] if not self._same_path(source, path)
                ]

        self.rebuild_groups()
        self.file_count_label.config(text=f"{len(self.files)} PDFs loaded")
        self._update_total_page_count()
        summary = f"Moved {len(moved)} of {len(candidates)} listed original PDF(s) to the Recycle Bin."
        if failures:
            summary += "\n\nNot moved (still listed for cleanup):\n" + "\n".join(
                f"{path.name} ({path}): {error}" for path, error in failures[:10]
            )
            if len(failures) > 10:
                summary += f"\n... and {len(failures) - 10} more failure(s)"
        self.status_var.set(summary)
        if failures:
            messagebox.showwarning("Cleanup completed with errors", summary)
        else:
            messagebox.showinfo("Cleanup finished", summary)

    def rebuild_groups(self, recompute=True):
        self.refresh_all_files_list()
        self.refresh_cleanup_panel()
        if recompute:
            manual_paths = {
                self._path_key(path) for group in self.manual_groups for path in group["files"]
            }
            self.groups = make_groups([
                path for path in self.files
                if self._path_key(path) not in manual_paths
                and self._path_key(path) not in self._auto_unassigned
            ])
        for child in self.cards_frame.winfo_children():
            child.destroy()
        self.group_widgets.clear()
        self.manual_group_widgets.clear()

        automatic_header = ttk.Frame(self.cards_frame, padding=(4, 8))
        automatic_header.pack(fill="x")
        ttk.Label(automatic_header, text="Automatic patient matching",
                  font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(automatic_header, text="Suggested from filenames; review matches before merging.",
                  foreground="#5c6b7a").pack(anchor="w", pady=(2, 0))
        if not self.groups:
            ttk.Label(self.cards_frame, text="No automatic matches yet. Unmatched PDFs remain in the source list.",
                      foreground="#5c6b7a").pack(anchor="w", padx=8, pady=8)

        for number, group in enumerate(self.groups, start=1):
            self._create_group_card(number, group)

        manual_header = ttk.Frame(self.cards_frame, padding=(4, 12, 4, 5))
        manual_header.pack(fill="x")
        ttk.Label(manual_header, text="Any Other Files to Merge",
                  font=("Segoe UI", 13, "bold")).pack(side="left")
        ttk.Button(manual_header, text="Create Manual Merge Group",
                   style="Primary.TButton", command=self.create_manual_group).pack(side="right")
        ttk.Label(
            self.cards_frame,
            text="Group unrelated filenames for one patient. Select a source PDF on the left, or browse to add one.",
            foreground="#5c6b7a", wraplength=800
        ).pack(anchor="w", padx=8, pady=(0, 5))
        if not self.manual_groups:
            ttk.Label(
                self.cards_frame,
                text="No manual groups yet. Create a group when filename matching is not appropriate.",
                foreground="#667789", padding=8
            ).pack(anchor="w")
        for manual_group in self.manual_groups:
            self._create_manual_group_card(manual_group)

    def _create_group_card(self, number, group):
        card = ttk.LabelFrame(
            self.cards_frame,
            text=f"  {number}. {' '.join(group['key'])}  —  {group['level']}  ",
            padding=7
        )
        card.pack(fill="x", padx=5, pady=7)

        header = ttk.Frame(card)
        header.pack(fill="x", pady=(0, 4))
        count_var = tk.StringVar(value=f"{len(group['files'])} PDF(s) in this group")
        ttk.Label(header, textvariable=count_var, font=("Segoe UI", 9, "bold")).pack(side="left")
        status_var = tk.StringVar(
            value="Ready to Merge" if group["level"] == "Exact Match" else "Review Required"
        )
        ttk.Label(header, textvariable=status_var, foreground="#356b50").pack(side="left", padx=(8, 0))
        ttk.Button(header, text="Select All", style="Small.TButton",
                   command=lambda gidx=number-1: self.set_group_checks(gidx, True)).pack(side="right", padx=2)
        ttk.Button(header, text="Uncheck All", style="Small.TButton",
                   command=lambda gidx=number-1: self.set_group_checks(gidx, False)).pack(side="right", padx=2)
        ttk.Button(header, text="Add from Left", style="Small.TButton",
                   command=lambda gidx=number-1: self.add_selected_left_file(gidx)).pack(side="right", padx=2)
        ttk.Button(header, text="Merge This Group", style="SmallPrimary.TButton",
                   command=lambda group_ref=group: self.merge_one_group("automatic", group_ref)
                   ).pack(side="right", padx=2)
        if self.dnd_enabled:
            card.drop_target_register(DND_FILES)
            card.dnd_bind("<<Drop>>", lambda event, target=group: self._drop_into_group(event, "automatic", target))

        # Table header
        table = ttk.Frame(card)
        table.pack(fill="x")
        widths = [0, 1, 0, 0, 0, 0, 0, 0, 0]
        headers = ["Use", "File name", "Pages", "From", "To", "Status", "Up", "Down", "Remove"]
        for col, (title, weight) in enumerate(zip(headers, widths)):
            ttk.Label(table, text=title, font=("Segoe UI", 9, "bold")).grid(
                row=0, column=col, sticky="w", padx=4, pady=3
            )

        rows = []
        for row_index, path in enumerate(group["files"], start=1):
            use_var = tk.BooleanVar(value=group["level"] == "Exact Match")
            from_var = tk.StringVar(value="1")
            to_var = tk.StringVar(value="…")
            page_var = tk.StringVar(value="Reading…")
            status_var = tk.StringVar(value="Loading")
            row_frame = ttk.Frame(table)
            row_frame.grid(row=row_index, column=0, columnspan=9, sticky="ew")
            for col, weight in enumerate(widths):
                row_frame.columnconfigure(col, weight=weight)

            ttk.Checkbutton(row_frame, variable=use_var).grid(row=0, column=0, padx=4, sticky="w")
            name_label = ttk.Label(row_frame, text=path.name, width=38)
            name_label.grid(row=0, column=1, padx=4, sticky="w")
            ttk.Label(row_frame, textvariable=page_var, width=10).grid(row=0, column=2, padx=4, sticky="w")
            from_entry = ttk.Entry(row_frame, textvariable=from_var, width=7)
            from_entry.grid(row=0, column=3, padx=4, sticky="w")
            to_entry = ttk.Entry(row_frame, textvariable=to_var, width=7)
            to_entry.grid(row=0, column=4, padx=4, sticky="w")
            ttk.Label(row_frame, textvariable=status_var, width=12).grid(row=0, column=5, padx=4, sticky="w")

            up_btn = ttk.Button(row_frame, text="↑", width=3)
            up_btn.grid(row=0, column=6, padx=3)
            down_btn = ttk.Button(row_frame, text="↓", width=3)
            down_btn.grid(row=0, column=7, padx=3)
            ttk.Button(row_frame, text="Remove", style="Small.TButton",
                       command=lambda target=group, source=path:
                       self.remove_from_automatic(target, source)
                       ).grid(row=0, column=8, padx=3)

            row_data = {
                "path": path, "use": use_var, "from": from_var, "to": to_var,
                "pages": page_var, "status": status_var, "frame": row_frame,
                "name": name_label, "group": group
            }
            rows.append(row_data)
            up_btn.config(command=lambda r=row_data, rs=rows: self.move_row(rs, r, -1))
            down_btn.config(command=lambda r=row_data, rs=rows: self.move_row(rs, r, 1))

            self._read_page_count(row_data)

        self.group_widgets.append({"group": group, "rows": rows, "card": card,
                                   "count": count_var, "status": status_var})
        for row in rows:
            row["use"].trace_add(
                "write", lambda *_args, target=self.group_widgets[-1]: self._update_group_summary(target)
            )
            row["from"].trace_add(
                "write", lambda *_args, target=self.group_widgets[-1]: self._update_group_summary(target)
            )
            row["to"].trace_add(
                "write", lambda *_args, target=self.group_widgets[-1]: self._update_group_summary(target)
            )
        self._update_group_summary(self.group_widgets[-1])

    def _create_manual_group_card(self, group):
        card = ttk.LabelFrame(self.cards_frame, text="  Manual Group  ", padding=10)
        card.pack(fill="x", padx=5, pady=6)
        header = ttk.Frame(card)
        header.pack(fill="x")
        ttk.Label(header, text="Patient / group name:").pack(side="left")
        name_entry = ttk.Entry(header, textvariable=group["name_var"], width=28)
        name_entry.pack(side="left", padx=(5, 10), fill="x", expand=True)
        summary = tk.StringVar()
        ttk.Label(header, textvariable=summary, foreground="#356b50").pack(side="left")
        status_var = tk.StringVar(value="Ready to Merge" if group["files"] else "Needs Review")
        ttk.Label(header, textvariable=status_var, foreground="#356b50").pack(side="right", padx=5)
        actions = ttk.Frame(card)
        actions.pack(fill="x", pady=(6, 7))
        ttk.Button(actions, text="Select all", style="Small.TButton",
                   command=lambda target=group: self.set_manual_checks(target, True)).pack(side="left")
        ttk.Button(actions, text="Select none", style="Small.TButton",
                   command=lambda target=group: self.set_manual_checks(target, False)).pack(side="left", padx=4)
        ttk.Button(actions, text="Delete Group", style="Small.TButton",
                   command=lambda target=group: self.delete_manual_group(target)).pack(side="right")
        ttk.Button(actions, text="Merge This Group", style="SmallPrimary.TButton",
                   command=lambda target=group: self.merge_one_group("manual", target)).pack(side="right", padx=2)
        ttk.Button(actions, text="Add PDF", style="Small.TButton",
                   command=lambda target=group: self.browse_into_manual(target)).pack(side="right", padx=2)
        ttk.Button(actions, text="Add from Left", style="Small.TButton",
                   command=lambda target=group: self.add_selected_to_manual(target)).pack(side="right", padx=2)

        table = ttk.Frame(card)
        table.pack(fill="x")
        headers = ["Use", "PDF filename / source folder", "Total pages", "From", "To", "Status", "Order", ""]
        weights = [0, 1, 0, 0, 0, 0, 0, 0]
        for column, (title, weight) in enumerate(zip(headers, weights)):
            ttk.Label(table, text=title, font=("Segoe UI", 9, "bold")).grid(
                row=0, column=column, sticky="w", padx=4, pady=3
            )
            table.columnconfigure(column, weight=weight)

        rows = []
        for row_index, path in enumerate(group["files"], start=1):
            use_var = tk.BooleanVar(value=True)
            from_var = tk.StringVar(value="1")
            to_var = tk.StringVar(value="…")
            page_var = tk.StringVar(value="Reading…")
            status_var = tk.StringVar(value="Loading")
            row_frame = ttk.Frame(table)
            row_frame.grid(row=row_index, column=0, columnspan=8, sticky="ew", pady=2)
            row_frame.columnconfigure(1, weight=1)
            ttk.Checkbutton(row_frame, variable=use_var).grid(row=0, column=0, rowspan=2, padx=4)
            labels = ttk.Frame(row_frame)
            labels.grid(row=0, column=1, rowspan=2, sticky="ew", padx=4)
            ttk.Label(labels, text=path.name, font=("Segoe UI", 9, "bold")).pack(anchor="w")
            ttk.Label(labels, text=str(path), foreground="#68788a").pack(anchor="w")
            ttk.Label(row_frame, textvariable=page_var, width=9).grid(row=0, column=2, rowspan=2, padx=4)
            ttk.Entry(row_frame, textvariable=from_var, width=6).grid(row=0, column=3, rowspan=2, padx=4)
            ttk.Entry(row_frame, textvariable=to_var, width=6).grid(row=0, column=4, rowspan=2, padx=4)
            ttk.Label(row_frame, textvariable=status_var, width=12).grid(row=0, column=5, rowspan=2, padx=4)
            order_buttons = ttk.Frame(row_frame)
            order_buttons.grid(row=0, column=6, rowspan=2, padx=3)
            up_button = ttk.Button(order_buttons, text="Up", width=4)
            down_button = ttk.Button(order_buttons, text="Down", width=5)
            up_button.pack(side="left", padx=(0, 2))
            down_button.pack(side="left")
            ttk.Button(row_frame, text="Remove", width=7,
                       command=lambda target=group, source=path: self.remove_from_manual(target, source)
                       ).grid(row=0, column=7, rowspan=2, padx=4)
            row = {
                "path": path, "use": use_var, "from": from_var, "to": to_var,
                "pages": page_var, "status": status_var, "frame": row_frame,
                "group": group
            }
            rows.append(row)
            up_button.config(command=lambda item=row, items=rows: self.move_row(items, item, -1))
            down_button.config(command=lambda item=row, items=rows: self.move_row(items, item, 1))
            self._read_page_count(row)

        widget = {"group": group, "rows": rows, "card": card,
                  "summary": summary, "status": status_var}
        self.manual_group_widgets.append(widget)
        for row in rows:
            row["use"].trace_add("write", lambda *_args, target=widget: self._update_group_summary(target))
            row["from"].trace_add("write", lambda *_args, target=widget: self._update_group_summary(target))
            row["to"].trace_add("write", lambda *_args, target=widget: self._update_group_summary(target))
        self._update_group_summary(widget)

        if self.dnd_enabled:
            card.drop_target_register(DND_FILES)
            card.dnd_bind(
                "<<Drop>>",
                lambda event, target=group: self._drop_into_group(event, "manual", target)
            )

    def _update_group_summary(self, group_ui):
        if "count" in group_ui:
            prefix = f"{len(group_ui['group']['files'])} PDF(s)"
        else:
            prefix = f"{len(group_ui['group']['files'])} PDF(s)"
        selected_pages = 0
        for row in group_ui["rows"]:
            if not row["use"].get():
                continue
            try:
                total = int(row["pages"].get())
                start = int(row["from"].get().strip())
                end = int(row["to"].get().strip())
            except (TypeError, ValueError):
                continue
            if validate_page_range(start, end, total) is None:
                selected_pages += end - start + 1
        summary_text = f"{prefix}  |  {selected_pages} selected pages"
        if "count" in group_ui:
            group_ui["count"].set(summary_text)
        else:
            group_ui["summary"].set(summary_text)

    def _read_page_count(self, row):
        if PdfReader is None:
            row["pages"].set("N/A")
            row["status"].set("Install pypdf")
            return
        path_key = self._path_key(row["path"])
        if path_key in self.page_counts:
            count = self.page_counts[path_key]
            row["pages"].set(str(count) if count else "N/A")
            row["to"].set(str(count) if count else "")
            row["status"].set("Ready" if count else "Needs Review")
            return
        try:
            reader = PdfReader(str(row["path"]), strict=False)
            if reader.is_encrypted:
                self.page_counts[path_key] = None
                row["pages"].set("N/A")
                row["status"].set("Needs Review")
                row["to"].set("")
            else:
                count = len(reader.pages)
                self.page_counts[path_key] = count
                row["pages"].set(str(count))
                row["to"].set(str(count))
                row["status"].set("Ready")
        except Exception:
            self.page_counts[path_key] = None
            row["pages"].set("N/A")
            row["status"].set("Needs Review")
            row["to"].set("")
        self._update_total_page_count()

    def move_row(self, rows, row, direction):
        try:
            index = rows.index(row)
            target = index + direction
            if target < 0 or target >= len(rows):
                return
            rows[index], rows[target] = rows[target], rows[index]
            if rows:
                rows[0]["group"]["files"] = [item["path"] for item in rows]
            for item in rows:
                item["frame"].grid_forget()
            for i, item in enumerate(rows, start=1):
                item["frame"].grid(row=i, column=0, columnspan=9, sticky="ew")
        except ValueError:
            pass

    def add_selected_left_file(self, group_index):
        if not 0 <= group_index < len(self.group_widgets):
            messagebox.showerror("Group no longer available", "Refresh the automatic groups and try again.")
            return
        source = self._selected_source()
        if source is not None:
            target = self.group_widgets[group_index]["group"]
            self._assign_file(source, "automatic", target)

    def set_group_checks(self, group_index, checked):
        if 0 <= group_index < len(self.group_widgets):
            for row in self.group_widgets[group_index]["rows"]:
                row["use"].set(checked)

    def _collect_group(self, group_ui):
        selected = []
        errors = []
        if PdfReader is None:
            return selected, ["pypdf is not installed"]
        for row in group_ui["rows"]:
            if not row["use"].get():
                continue
            path = row["path"]
            try:
                if not path.is_file():
                    raise FileNotFoundError("source file is missing")
                reader = PdfReader(str(path), strict=False)
                if reader.is_encrypted:
                    raise ValueError("password-protected PDF")
                total = len(reader.pages)
                start = int(row["from"].get().strip())
                end = int(row["to"].get().strip())
                range_error = validate_page_range(start, end, total)
                if range_error:
                    raise ValueError(range_error)
                selected.append((path, start, end))
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")
        return selected, errors

    def merge_all(self):
        if self._merge_running:
            return
        jobs = []
        failures = []
        all_groups = [
            ("automatic", group_ui) for group_ui in self.group_widgets
        ] + [
            ("manual", group_ui) for group_ui in self.manual_group_widgets
        ]
        for kind, group_ui in all_groups:
            selected, errors = self._collect_group(group_ui)
            label = self._group_label(kind, group_ui["group"])
            if kind == "manual" and not group_ui["group"]["name_var"].get().strip():
                errors.append("a patient/group name is required")
            group_ui["status"].set("Needs Review" if errors else "Ready to Merge")
            if errors:
                failures.append(f"{label}: " + "; ".join(errors))
            elif selected:
                jobs.append((kind, group_ui["group"], label, selected))
        self._confirm_and_start_merge(jobs, failures, all_groups=True)

    def merge_one_group(self, kind, group):
        if self._merge_running:
            return
        group_widgets = self.group_widgets if kind == "automatic" else self.manual_group_widgets
        group_ui = next((item for item in group_widgets if item["group"] is group), None)
        if group_ui is None:
            messagebox.showerror("Group no longer available", "Refresh the workspace and try again.")
            return
        selected, errors = self._collect_group(group_ui)
        label = self._group_label(kind, group)
        if kind == "manual" and not group["name_var"].get().strip():
            errors.append("a patient/group name is required")
        group_ui["status"].set("Needs Review" if errors else "Ready to Merge")
        failures = [f"{label}: " + "; ".join(errors)] if errors else []
        jobs = [(kind, group, label, selected)] if selected and not errors else []
        self._confirm_and_start_merge(jobs, failures, all_groups=False)

    def _confirm_and_start_merge(self, jobs, validation_failures, all_groups):
        if PdfReader is None or PdfWriter is None:
            messagebox.showerror(
                "Missing package", "Install packages using:\npy -m pip install -r requirements.txt"
            )
            return
        if self.output_dir is None:
            messagebox.showwarning("Output folder", "Choose an output folder first.")
            return
        if not jobs and not validation_failures:
            messagebox.showwarning("Nothing selected", "No checked PDFs are available to merge.")
            return
        if not jobs and validation_failures:
            messagebox.showerror(
                "Selected PDFs need review",
                "No group can be merged because selected PDFs failed validation:\n\n" +
                "\n".join(validation_failures[:12])
            )
            return

        descriptions = [
            f"{label}: {len(selected)} checked PDF(s)"
            for _kind, _group, label, selected in jobs
        ]
        if validation_failures:
            descriptions.extend(f"NOT MERGED — {error}" for error in validation_failures[:8])
        prompt = (
            "Create separate merged PDFs for these groups?\n\n"
            + "\n".join(descriptions)
            + "\n\nOnly checked PDFs will be included, in the displayed order and page ranges."
        )
        title = "Confirm all groups" if all_groups else "Confirm group merge"
        if not messagebox.askyesno(title, prompt):
            return

        self._merge_running = True
        self.merge_all_button.config(state="disabled")
        self.cleanup_button.config(state="disabled")
        self.cleanup_clear_button.config(state="disabled")
        self.merge_progress["value"] = 0
        self.status_var.set("Merging PDFs. The application remains available while files are processed.")
        output_dir = self.output_dir
        worker = threading.Thread(
            target=self._merge_worker,
            args=(jobs, validation_failures, output_dir),
            daemon=True
        )
        worker.start()
        self.root.after(100, self._poll_merge_queue)

    def _merge_worker(self, jobs, validation_failures, output_dir):
        successes = []
        failures = list(validation_failures)
        failed_groups = []
        total_jobs = len(jobs)
        for index, (kind, group, label, selected) in enumerate(jobs, start=1):
            self._merge_queue.put(("start", index, total_jobs, label))
            try:
                def report_page_progress(completed, total):
                    self._merge_queue.put(
                        ("page_progress", index, total_jobs, label, completed, total)
                    )

                output, sources = merge_pdf_selection(
                    selected, output_dir, label, progress_callback=report_page_progress
                )
                successes.append((output, sources, label, kind, group))
            except Exception as exc:
                failures.append(f"{label}: {exc}")
                failed_groups.append((kind, group))
            self._merge_queue.put(("group_done", index, total_jobs, label))
        self._merge_queue.put(("done", successes, failures, failed_groups))

    def _poll_merge_queue(self):
        try:
            while True:
                item = self._merge_queue.get_nowait()
                if item[0] == "start":
                    _kind, index, total, label = item
                    self.merge_progress["value"] = ((index - 1) / total * 100) if total else 0
                    self.status_var.set(f"Merging {label} ({index} of {total})...")
                elif item[0] == "page_progress":
                    _kind, index, total, label, completed, page_total = item
                    fraction = completed / page_total if page_total else 1
                    self.merge_progress["value"] = ((index - 1 + fraction) / total * 100) if total else 100
                    self.status_var.set(
                        f"Merging {label} ({index} of {total}): {completed}/{page_total} pages"
                    )
                elif item[0] == "group_done":
                    _kind, index, total, _label = item
                    self.merge_progress["value"] = (index / total * 100) if total else 100
                elif item[0] == "done":
                    _kind, successes, failures, failed_groups = item
                    self.cleanup_candidates = accumulate_cleanup_candidates(
                        self.cleanup_candidates, successes
                    )
                    for _output, _sources, _label, kind, group in successes:
                        widgets = self.group_widgets if kind == "automatic" else self.manual_group_widgets
                        group_ui = next((entry for entry in widgets if entry["group"] is group), None)
                        if group_ui is not None:
                            group_ui["status"].set("Merged Successfully")
                    for kind, group in failed_groups:
                        widgets = self.group_widgets if kind == "automatic" else self.manual_group_widgets
                        group_ui = next((entry for entry in widgets if entry["group"] is group), None)
                        if group_ui is not None:
                            group_ui["status"].set("Needs Review")
                    self._merge_running = False
                    self.refresh_cleanup_panel()
                    self.merge_all_button.config(state="normal")
                    self.cleanup_clear_button.config(
                        state=("normal" if self.cleanup_candidates else "disabled")
                    )
                    self.merge_progress["value"] = 100 if successes else 0
                    message = (
                        f"Successfully created {len(successes)} merged PDF(s).\n"
                        f"Failed groups: {len(failures)}."
                    )
                    if successes:
                        message += f"\n\nOutput folder:\n{self.output_dir}"
                    if failures:
                        message += "\n\nNot merged:\n" + "\n".join(failures[:12])
                    if successes:
                        message += (
                            "\n\nOnly originals used in these successful outputs are shown "
                            "in the cleanup panel."
                        )
                    self.status_var.set(
                        f"Created {len(successes)} merged PDF(s); {len(failures)} failed."
                    )
                    messagebox.showinfo("Merge finished", message)
                    return
        except queue.Empty:
            pass
        if self._merge_running:
            self.root.after(100, self._poll_merge_queue)

def main():
    if DND_AVAILABLE:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
    app = MRDPDFMerger(root)
    root.mainloop()


if __name__ == "__main__":
    main()
