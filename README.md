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
  Scanner suffixes/IDs (for example `S-5`, `S 6`, or `SCAN 5`), document labels
  such as CT, NCCT, MRI, USG, X-RAY, REPORT, COVER, and DISCHARGE SUMMARY,
  UHID/EP/visit/mobile/date identifiers, punctuation, and extra whitespace are
  normalized away while identifier values are retained as matching evidence.
- **REVIEW REQUIRED**: potential matches from first-and-middle-name agreement,
  token similarity, spelling similarity, or supporting identifiers. These are
  never auto-confirmed; different surnames and conflicting UHIDs cannot become
  confirmed matches. Files with conflicting UHIDs are never grouped together,
  and an unnumbered filename is not used to bridge those identities.
- Matching thresholds are defined by `MatchingConfig` in `mrd_pdf_merger.py`
  and can be overridden when calling `make_groups`.

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

- Merged PDFs are saved in a `Merged_PDFs` subfolder inside each source folder
  represented in the group. The subfolder is created automatically when needed.
- If a group contains source PDFs from multiple folders, an identical merged
  output is created in each of those folders. No output destination needs to be
  selected.
- Existing outputs are never overwritten; a unique `_MERGED` filename is
  selected. Generated merged PDFs are excluded from later scans.

The **Add Folder** button opens a folder picker to choose input PDFs; it does
not select an output destination.

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

## Trial licensing and Windows packaging

The application starts a 60-day trial on its first launch and stores the
installation date and last-run date in
`C:\ProgramData\MRD_Smart_PDF_Merger\license.json`. If the existing license
folder is present but its file is missing or invalid, startup stops instead of
creating a replacement trial. This local check is not tamper-proof.

To package the application from the project directory after installing
PyInstaller:

```bat
pyinstaller --noconfirm --clean --onefile --windowed --name "MRD Smart PDF Merger" --icon="app_icon.ico" --add-data "app_icon.ico;." --collect-all tkinterdnd2 mrd_pdf_merger.py
```

The icon option sets the EXE's File Explorer icon, and `--add-data` makes the
same ICO available to the Tk window at runtime. The license path is based on
Windows `ProgramData`, not the current directory or the PyInstaller extraction
directory.

## Tests

Run the synthetic-PDF unit suite from the project directory:

```bat
py -m unittest -v
```

The tests cover normalization and grouping, conservative fuzzy suggestions,
unmatched-source availability, checkbox/page selection helpers, manual
assignment, same- and cross-folder destinations, output-name collisions, PDF
validation, cleanup-history safety, and trial initialization, expiry, invalid
files, and date rollback. Trial dates and storage paths are injected in tests,
so trial boundaries can be verified without waiting 60 days or changing the
machine clock.
