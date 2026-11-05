# MRD Smart PDF Merger

A Windows 10/11 desktop application for reviewing patient PDF matches, building
manual groups, selecting page ranges and creating one merged output per group.
Original PDFs are left unchanged unless you explicitly confirm Recycle Bin
cleanup.

## Features

- Add individual PDF files, recursively scan folders, or use drag and drop.
- Browse original PDFs in an expandable folder tree with per-folder PDF counts.
- Match exact cleaned full names; suggest similar full names as **Possible Match**
  groups that remain unchecked until reviewed. Other files are marked
  **Review Required** rather than matched by first name alone.
- Add files to automatic or named manual groups. Duplicate assignment requires
  confirmation before moving a file.
- Set checkboxes, inclusive page ranges and custom order for each group.
- Merge one group or all eligible automatic and manual groups in the background,
  with progress and success/failure reporting.
- Keep cleanup eligibility cumulative for the current source collection. Only
  checked PDFs included in successful outputs are candidates.
- Send selected originals, or all listed cleanup originals with **Clear Cleanup**,
  to the Windows Recycle Bin after confirmation. Files not in the cleanup list
  are not affected, and failed moves remain listed with their error.
- Use collision-safe output names and avoid scanning `Merged_PDFs` output folders.

## Requirements

- Windows 10 or Windows 11
- Python 3.10 or newer with Tcl/Tk enabled
- Packages listed in `requirements.txt`

## Install and launch

Open Command Prompt in the project directory:

```bat
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt
py mrd_pdf_merger.py
```

Drag-and-drop support uses `tkinterdnd2`. If it is unavailable in the runtime,
the application still supports adding files/folders and selecting files from
the folder tree.

## Workflow

1. Choose **Add Files** or **Add Folder**. Folder scans include nested folders,
   regardless of whether those folders are expanded in the tree.
2. Review exact matches, possible fuzzy matches, and individual files requiring
   review. A possible fuzzy match is not selected by default.
3. For files that do not match by name, choose **Create Manual Merge Group**,
   edit its name, select a PDF leaf in the left tree, and choose **Add Selected
   from Left**. **Add PDF** opens a file browser.
4. Confirm any reassignment prompt. Review each checked PDF, its total pages,
   inclusive From/To range, and position.
5. Use **Merge This Group** or **Merge All Groups**. Empty groups are ignored;
   selected invalid files fail their group rather than being silently skipped.
6. Review the cumulative **Original PDFs Ready for Cleanup** list. Select files
   and confirm before sending them to the Recycle Bin. **Clear** resets the
   source collection and its cleanup history.

Matching compares complete normalized names, removes document labels and
numeric identifiers, and uses conservative fuzzy suggestions. Confirm patient
identity before merging; use manual groups when in doubt.

## Tests

Run the automated matching, assignment, range, ordering, output-collision,
failure-handling and cleanup-history tests:

```bat
py -m unittest -v
```

## Output safety

Merged files are written to the selected output folder as separate PDFs.
Existing output files are never overwritten. Sources are validated before
writing; a failed merge does not produce a successful output or cleanup
eligibility. Merged outputs are never cleanup candidates.
