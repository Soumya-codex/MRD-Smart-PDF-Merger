# MRD Smart PDF Merger

A Windows 10/11 desktop application for reviewing filename-based PDF groups,
combining selected pages into one output per group, and optionally moving
successfully merged original PDFs to the Windows Recycle Bin. The application
does not modify original PDFs during scanning or merging.

## Requirements and installation

- Windows 10 or Windows 11
- Python 3.10 or newer with Tcl/Tk enabled
- Dependencies from `requirements.txt`: `pypdf`, `tkinterdnd2`, and `Send2Trash`

In Command Prompt, from the project directory:

```bat
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt
py mrd_pdf_merger.py
```

Drag-and-drop support uses `tkinterdnd2`. Without it, add files and folders
using the buttons and use the source tree to assign PDFs to groups.

## Scanning and browsing

- **Add Files** loads selected PDFs. **Add Folder** recursively scans nested
  subfolders whether or not they are expanded in the folder tree.
- Adding another folder adds it to the current collection. Existing manual
  groups and cleanup history are retained.
- The left tree displays nested folders, PDF counts, and PDF leaves. Use its
  filename search to narrow the visible leaves.
- The main **Clear** action asks for confirmation, then resets only the current
  collection and session cleanup history. It never deletes originals or merged
  outputs.
- `Merged_PDFs` folders and filenames ending in `_MERGED.pdf` (including
  collision suffixes such as `_MERGED_2.pdf`) are excluded from scanning.

## Matching panel

The right panel has exactly two matching sections, plus a compact source/group
summary, patient-name search, category filters, and collapsible cards:

- **CONFIRMED MATCHES**: files with the same complete normalized patient name.
  Scanner suffixes/IDs (for example `S-5`, `S 6`, or `SCAN 5`), document labels,
  dates, timestamps, numeric IDs, punctuation, and extra whitespace are
  normalized away.
- **REVIEW REQUIRED**: conservative spelling-variation suggestions. They are
  never auto-confirmed; review the files before merging. Distinct surnames are
  not joined just because first or middle names match.

PDFs without an exact or reviewable match have no automatic card. They remain
visible and selectable in the left folder explorer, and are included in the
unmatched count. The match cards show patient name, PDF/page counts, and status
when collapsed. Expand them to review file checkboxes, inclusive page ranges,
order, and merge actions.

## Manual groups and merging

Create multiple independent manual groups in the separately scrollable
**Manual Groups** area on the left, below the explorer. Select an unmatched PDF
in the explorer and use **Add from Left**, or add through the file picker.
Rename groups, select files, set page ranges, reorder rows, and remove
assignments without deleting originals. A source PDF cannot belong to two groups
at once; moving it requires confirmation. Manual groups are included in
**Merge All**.

**Merge This Group** merges that group alone. **Merge All** includes eligible
automatic and manual groups, including review-required groups. It shows the
eligible group count, selected PDF/page counts, and selected review-required
group count, then offers **Review First** or **Proceed Anyway**. Unchecked PDFs
are excluded; empty groups are skipped. Invalid selected PDFs/ranges are
reported and prevent that group from merging. Each group produces a separate
output PDF, preserving its selected PDF order and page ranges. Merges run in a
background worker with progress and a completion/failure summary.

### Output destinations

- When all selected source PDFs in a group are in the same folder, that exact
  folder is used for the group's output.
- When a group contains PDFs from multiple folders, the application asks you
  to choose that group's output folder before merging. For **Merge All**, all
  required destination choices are collected before the merge starts; cancelling
  a destination dialog cancels the batch.
- The **Output Folder** setting is retained as the starting location for
  mixed-folder destination dialogs. Same-folder groups always use their source
  folder.
- Existing outputs are never overwritten; a unique `_MERGED` filename is
  selected. Generated merged PDFs are excluded from later scans.

## Cleanup safety

Only original source PDFs actually included in successful merges appear in the
cleanup list. Entries accumulate during the current session and are deduplicated
by full path. Unchecked, unused, failed-merge, and generated output PDFs are not
added.

Select the desired entries and use **Move Selected to Recycle Bin**. The app
shows the selected-file count, requires explicit confirmation, and uses
`Send2Trash` to recycle only those listed source PDFs. Successful moves are
removed from the list; failures remain listed with their error. There is no
permanent-delete action.

## Tests

Run the synthetic-PDF unit suite from the project directory:

```bat
py -m unittest -v
```

The tests cover normalization and grouping, conservative fuzzy suggestions,
unmatched-source availability, checkbox/page selection helpers, manual
assignment, same- and cross-folder destinations, output-name collisions, PDF
validation, and cleanup-history safety.
