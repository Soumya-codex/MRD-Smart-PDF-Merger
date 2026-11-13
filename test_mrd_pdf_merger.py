import tempfile
import time
import tkinter as tk
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter

import mrd_pdf_merger as merger_module
from mrd_pdf_merger import (
    MRDPDFMerger,
    MatchingConfig,
    accumulate_cleanup_candidates,
    assign_source_to_group,
    cleanup_paths_from_successes,
    extract_filename_identity,
    get_name_tokens,
    is_generated_output,
    merge_pdf_selection_to_folders,
    merged_output_directories,
    make_groups,
    merge_pdf_selection,
    move_cleanup_sources_to_trash,
    selected_items,
    selection_page_count,
    source_folder_for_paths,
    source_folder_for_selection,
    unmatched_source_paths,
    validate_page_range,
)


class FilenameMatchingTests(unittest.TestCase):
    def assert_patient_files_grouped(self, filenames, patient_name):
        groups = make_groups([Path(name) for name in filenames])
        matching = [
            group for group in groups
            if " ".join(group["key"]) == patient_name
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(
            {path.name for path in matching[0]["files"]},
            set(filenames),
        )

    def test_ananta_kumar_das_examples_share_one_group(self):
        self.assert_patient_files_grouped(
            [
                "ANANTA KUMAR DAS COVER.pdf",
                "ANANTA KUMAR DAS S5 2026090410037073.pdf",
                "ANANTA KUMAR DAS S5 COVER.pdf",
            ],
            "ANANTA KUMAR DAS",
        )

    def test_scanner_suffixes_and_document_labels_are_removed(self):
        names = [
            "ABHIMANYU SUTARA COVER.pdf",
            "ABHIMANYU SUTARA S-5.pdf",
            "ABHIMANYU SUTARA S-6.pdf",
            "ABHIMANYU SUTARA S5.pdf",
            "ABHIMANYU SUTARA S6.pdf",
            "ABHIMANYU SUTARA S 5.pdf",
            "ABHIMANYU SUTARA SCAN 5.pdf",
            "ABHIMANYU SUTARA SCAN 6.pdf",
            "ABHIMANYU SUTARA TUMOUR BOARD REPORT.pdf",
            "ABHIMANYU SUTARA TUMOR BOARD 2026101410059243.pdf",
        ]
        self.assertEqual(get_name_tokens(names[0]), ["ABHIMANYU", "SUTARA"])
        self.assert_patient_files_grouped(names, "ABHIMANYU SUTARA")

    def test_requested_abhi_scan_file_is_an_exact_match(self):
        groups = make_groups([
            Path("ABHIMANYU SUTARA COVER.pdf"),
            Path("ABHIMANYU SUTARA S-6 2026101410059243.pdf"),
        ])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["category"], "confirmed")
        self.assertEqual(groups[0]["level"], "Exact Match")

    def test_lopinti_scanner_and_attached_cover_names_normalize_to_same_patient(self):
        names = [
            "LOPINTI  SAHADEVA  S-6 202610170037326.pdf",
            "LOPINTI SAHADEVACOVER.pdf",
        ]
        self.assertEqual(get_name_tokens(names[0]), ["LOPINTI", "SAHADEVA"])
        self.assertEqual(get_name_tokens(names[1]), ["LOPINTI", "SAHADEVA"])
        groups = make_groups([Path(name) for name in names])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["category"], "confirmed")
        self.assertEqual(groups[0]["key"], ("LOPINTI", "SAHADEVA"))
        self.assertEqual({path.name for path in groups[0]["files"]}, set(names))

    def test_ratnakar_biswal_examples_share_one_group(self):
        self.assert_patient_files_grouped(
            [
                "RATNAKAR BISWAL COVER.pdf",
                "RATNAKAR BISWAL S5 2026101210027804.pdf",
                "RATNAKAR BISWAL TUMOUR BOARD.pdf",
            ],
            "RATNAKAR BISWAL",
        )

    def test_bidyadhara_bhoi_ct_report_and_ncct_files_share_one_group(self):
        names = [
            "BIDYADHARA BHOI CT REPORT.pdf",
            "BIDYADHARA BHOI NCCT.pdf",
            "BIDYADHARA BHOI S5 2026101910048850.pdf",
        ]
        for name in names:
            self.assertEqual(get_name_tokens(name), ["BIDYADHARA", "BHOI"])
        self.assert_patient_files_grouped(names, "BIDYADHARA BHOI")

    def test_scanner_suffix_and_all_requested_document_labels_are_removed(self):
        names = [
            "ABHIMANYU SUTARA.pdf",
            "ABHIMANYU SUTARA S.pdf",
            "ABHIMANYU SUTARA COVER.pdf",
            "ABHIMANYU SUTARA MRI.pdf",
            "ABHIMANYU SUTARA USG.pdf",
            "ABHIMANYU SUTARA X-RAY REPORT.pdf",
            "ABHIMANYU SUTARA DISCHARGE SUMMARY.pdf",
        ]
        self.assert_patient_files_grouped(names, "ABHIMANYU SUTARA")

    def test_patient_identifiers_are_removed_and_retained_as_supporting_metadata(self):
        identity = extract_filename_identity(
            "BIDYADHARA BHOI UHID001 EP-234 VISIT-56 MOBILE 9876543210 20261019.pdf"
        )
        self.assertEqual(identity.tokens, ("BIDYADHARA", "BHOI"))
        self.assertEqual(identity.identifiers["UHID"], frozenset({"001"}))
        self.assertEqual(identity.identifiers["EP"], frozenset({"234"}))
        self.assertEqual(identity.identifiers["VISIT"], frozenset({"56"}))
        self.assertEqual(identity.identifiers["MOBILE"], frozenset({"9876543210"}))
        self.assertEqual(identity.identifiers["DATE"], frozenset({"20261019"}))

    def test_first_middle_agreement_is_review_only_when_surnames_differ(self):
        groups = make_groups([
            Path("RAMESH KUMAR SAHU.pdf"),
            Path("RAMESH KUMAR DAS.pdf"),
        ])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["category"], "review")
        self.assertIn("First/middle", groups[0]["level"])

    def test_conflicting_uhids_are_not_grouped_even_for_identical_names(self):
        files = [
            Path("BIDYADHARA BHOI UHID001.pdf"),
            Path("BIDYADHARA BHOI UHID002.pdf"),
        ]
        self.assertEqual(make_groups(files), [])
        self.assertEqual(set(unmatched_source_paths(files, [])), set(files))

    def test_alphanumeric_uhid_values_are_compared_in_full(self):
        files = [
            Path("BIDYADHARA BHOI UHID-A001.pdf"),
            Path("BIDYADHARA BHOI UHID-B001.pdf"),
        ]
        self.assertEqual(make_groups(files), [])

    def test_unidentified_file_does_not_bridge_conflicting_uhid_groups(self):
        files = [
            Path("BIDYADHARA BHOI.pdf"),
            Path("BIDYADHARA BHOI UHID001.pdf"),
            Path("BIDYADHARA BHOI UHID002.pdf"),
        ]
        groups = make_groups(files)
        self.assertEqual(groups, [])
        self.assertEqual(set(unmatched_source_paths(files, groups)), set(files))

    def test_shared_uhid_supports_a_review_match_but_does_not_auto_confirm_typo(self):
        groups = make_groups([
            Path("ABHIMANYU SUTARA UHID001.pdf"),
            Path("ABHIMANYU SUTARR UHID001.pdf"),
        ])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["category"], "review")

    def test_supporting_identifiers_can_resolve_a_borderline_name_candidate(self):
        files = [
            Path("ABHIMANYU SUTARA EP1234.pdf"),
            Path("ABHIMANYU SUTARR EP1234.pdf"),
        ]
        config = MatchingConfig(
            token_similarity_threshold=1.0,
            fuzzy_token_threshold=1.0,
            minimum_fuzzy_name_similarity=1.0,
        )
        groups = make_groups(files, config)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["category"], "review")
        self.assertIn("supporting identifier", groups[0]["level"])

    def test_matching_thresholds_are_configurable(self):
        files = [Path("NAZIA KHARAM.pdf"), Path("NAZIA KHANAM.pdf")]
        self.assertEqual(len(make_groups(files)), 1)
        strict_config = MatchingConfig(fuzzy_token_threshold=1.0)
        self.assertEqual(make_groups(files, strict_config), [])

    def test_minor_spelling_difference_is_a_reviewable_possible_match(self):
        groups = make_groups([
            Path("NAZIA KHARAM COVER.pdf"),
            Path("NAZIA KHANAM S5 2026101410053501.pdf"),
        ])
        self.assertEqual(len(groups), 1)
        self.assertIn("Possible Match", groups[0]["level"])
        self.assertIn("Review Required", groups[0]["level"])
        self.assertEqual(len(groups[0]["files"]), 2)

    def test_same_first_and_middle_names_with_different_surnames_require_review(self):
        files = [
            Path("ANANTA KUMAR DAS COVER.pdf"),
            Path("ANANTA KUMAR ROUT COVER.pdf"),
        ]
        groups = make_groups(files)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["category"], "review")

    def test_exact_and_review_candidates_are_emitted_without_duplicate_files(self):
        files = [
            Path("RAMESH KUMAR SAHU.pdf"),
            Path("RAMESH KUMAR SAHU REPORT.pdf"),
            Path("RAMESH KUMAR DAS.pdf"),
        ]
        groups = make_groups(files)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["category"], "review")
        grouped_paths = [path for group in groups for path in group["files"]]
        self.assertEqual(len(grouped_paths), len(set(grouped_paths)))
        self.assertEqual(set(grouped_paths), set(files))

    def test_single_pdf_is_unmatched_not_review_required(self):
        source = Path("SINGLE PATIENT COVER.pdf")
        groups = make_groups([source])
        self.assertEqual(groups, [])
        self.assertEqual(unmatched_source_paths([source], groups), [source])

    def test_right_panel_groups_only_contain_confirmed_or_review_categories(self):
        groups = make_groups([
            Path("PATIENT ONE COVER.pdf"),
            Path("PATIENT ONE REPORT.pdf"),
            Path("PATIENT TWO COVER.pdf"),
            Path("PATIENT THREE SOLO.pdf"),
            Path("PATIENT THREE SOLO REPORT.pdf"),
        ])
        self.assertTrue(groups)
        self.assertTrue(all(group["category"] in {"confirmed", "review"} for group in groups))
        self.assertTrue(any(path.name == "PATIENT TWO COVER.pdf"
                            for path in unmatched_source_paths(
                                [Path("PATIENT TWO COVER.pdf")], groups)))

    def test_checkbox_selection_and_inclusive_page_totals(self):
        first = (Path("first.pdf"), 2, 4)
        unchecked = (Path("unchecked.pdf"), 1, 10)
        second = (Path("second.pdf"), 1, 1)
        selected = selected_items([(True, first), (False, unchecked), (True, second)])
        self.assertEqual(selected, [first, second])
        self.assertEqual(selection_page_count(selected), 4)

    def test_generated_merged_outputs_are_recognized(self):
        self.assertTrue(is_generated_output(Path("Patient_MERGED.pdf")))
        self.assertTrue(is_generated_output(Path("Patient_MERGED_2.PDF")))
        self.assertFalse(is_generated_output(Path("Patient_Report.pdf")))

    def test_shared_prefix_with_different_tokens_is_not_a_fuzzy_match(self):
        groups = make_groups([
            Path("UNRELATED ONE COVER.pdf"),
            Path("UNRELATED TWO REPORT.pdf"),
        ])
        self.assertEqual(groups, [])


class TrialCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.program_data = Path(self.temp_dir.name)
        self.today = date(2026, 1, 1)
        self.license_dir = self.program_data / "MRD_Smart_PDF_Merger"
        self.license_file = self.license_dir / "license.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_first_launch_records_date_and_restart_keeps_trial_date(self):
        self.assertEqual(
            merger_module.check_trial(program_data=str(self.program_data), today=self.today),
            60,
        )
        self.assertEqual(
            merger_module.check_trial(
                program_data=str(self.program_data), today=self.today + timedelta(days=10)
            ),
            50,
        )

    def test_expired_trial_displays_popup_and_exits(self):
        merger_module.check_trial(program_data=str(self.program_data), today=self.today)

        with patch.object(merger_module.messagebox, "showerror") as popup:
            with self.assertRaises(SystemExit) as exit_error:
                merger_module.check_trial(
                    program_data=str(self.program_data), today=self.today + timedelta(days=60)
                )

        self.assertEqual(exit_error.exception.code, 0)
        self.assertEqual(popup.call_args.args[0], "Trial Expired")
        self.assertIn("Your 2-month trial period has expired.", popup.call_args.args[1])
        self.assertIn("9348448730", popup.call_args.args[1])

    def test_missing_or_corrupted_license_does_not_start_a_new_trial(self):
        merger_module.check_trial(program_data=str(self.program_data), today=self.today)
        self.license_file.unlink()
        with patch.object(merger_module.messagebox, "showerror"):
            with self.assertRaises(SystemExit):
                merger_module.check_trial(
                    program_data=str(self.program_data), today=self.today + timedelta(days=5)
                )
        self.assertFalse(self.license_file.exists())

        self.license_file.write_text("{", encoding="utf-8")
        with patch.object(merger_module.messagebox, "showerror"):
            with self.assertRaises(SystemExit):
                merger_module.check_trial(
                    program_data=str(self.program_data), today=self.today + timedelta(days=5)
                )
        self.assertEqual(self.license_file.read_text(encoding="utf-8"), "{")

    def test_system_date_rollback_is_rejected(self):
        merger_module.check_trial(program_data=str(self.program_data), today=self.today)
        merger_module.check_trial(
            program_data=str(self.program_data), today=self.today + timedelta(days=10)
        )

        with patch.object(merger_module.messagebox, "showerror"):
            with self.assertRaises(SystemExit):
                merger_module.check_trial(
                    program_data=str(self.program_data), today=self.today + timedelta(days=9)
                )


class ApplicationWorkflowTests(unittest.TestCase):
    def test_discovered_pdfs_are_rendered_or_remain_unmatched_in_explorer(self):
        root = tk.Tk()
        root.withdraw()
        app = MRDPDFMerger(root)
        try:
            with tempfile.TemporaryDirectory() as temp:
                source_dir = Path(temp)
                nested_dir = source_dir / "nested"
                nested_dir.mkdir()
                sources = [
                    source_dir / "LOPINTI  SAHADEVA  S-6 202610170037326.pdf",
                    source_dir / "LOPINTI SAHADEVACOVER.pdf",
                    source_dir / "NAZIA KHARAM COVER.pdf",
                    source_dir / "NAZIA KHANAM S5 2026101410053501.pdf",
                    nested_dir / "UNRELATED SYNTHETIC PERSON.pdf",
                ]
                for source in sources:
                    writer = PdfWriter()
                    writer.add_blank_page(width=100, height=100)
                    with source.open("wb") as stream:
                        writer.write(stream)

                app._add_paths([source_dir])

                self.assertEqual(len(app.files), len(sources))
                self.assertEqual(
                    {group["category"] for group in app.groups},
                    {"confirmed", "review"},
                )
                self.assertTrue(any(
                    group["category"] == "confirmed"
                    and group["key"] == ("LOPINTI", "SAHADEVA")
                    and len(group["files"]) == 2
                    for group in app.groups
                ))
                self.assertTrue(any(
                    group["category"] == "review"
                    and {path.name for path in group["files"]}
                    == {"NAZIA KHARAM COVER.pdf", "NAZIA KHANAM S5 2026101410053501.pdf"}
                    for group in app.groups
                ))
                self.assertEqual(
                    [path.name for path in unmatched_source_paths(app.files, app.groups)],
                    ["UNRELATED SYNTHETIC PERSON.pdf"],
                )
                self.assertEqual(
                    {widget["group"]["category"] for widget in app.group_widgets},
                    {"confirmed", "review"},
                )
                self.assertEqual(
                    {app._path_key(path) for path in app._tree_file_paths.values()},
                    {app._path_key(path) for path in sources},
                )

                state_counts = {}
                for group in app.groups:
                    for path in group["files"]:
                        key = app._path_key(path)
                        state_counts[key] = state_counts.get(key, 0) + 1
                for path in unmatched_source_paths(app.files, app.groups):
                    key = app._path_key(path)
                    state_counts[key] = state_counts.get(key, 0) + 1
                self.assertEqual(
                    state_counts,
                    {app._path_key(path): 1 for path in sources},
                )

                deadline = time.time() + 5
                while app._page_tasks and time.time() < deadline:
                    root.update()
                    time.sleep(0.01)
                self.assertFalse(app._page_tasks)
                # Let the initial layout callbacks run before destroying the Tk root.
                deadline = time.time() + 0.4
                while time.time() < deadline:
                    root.update()
                    time.sleep(0.01)
        finally:
            root.destroy()

    def test_unmatched_files_stay_in_explorer_and_merge_all_includes_review_groups(self):
        root = tk.Tk()
        root.withdraw()
        app = MRDPDFMerger(root)
        original_showinfo = merger_module.messagebox.showinfo
        summary_prompts = []
        merger_module.messagebox.showinfo = lambda *_args, **_kwargs: None
        try:
            with tempfile.TemporaryDirectory() as temp:
                source_dir = Path(temp)
                sources = [
                    source_dir / "NAZIA KHARAM COVER.pdf",
                    source_dir / "NAZIA KHANAM REPORT.pdf",
                    source_dir / "UNMATCHED SYNTHETIC PERSON.pdf",
                ]
                for source in sources:
                    writer = PdfWriter()
                    writer.add_blank_page(width=100, height=100)
                    with source.open("wb") as stream:
                        writer.write(stream)

                app._add_paths([source_dir])
                self.assertEqual(
                    {group["category"] for group in app.groups},
                    {"review"},
                )
                self.assertTrue(all(
                    group["category"] in {"confirmed", "review"}
                    for group in app.groups
                ))
                self.assertEqual(len(app.group_widgets), 1)
                self.assertEqual(len(app._tree_file_paths), 3)
                self.assertIn("1 unmatched PDFs", app.match_summary_var.get())

                app.create_manual_group()
                manual = app.manual_groups[-1]
                unmatched = sources[-1]
                source_iid = next(
                    iid for iid, path in app._tree_file_paths.items()
                    if app._same_path(path, unmatched)
                )
                app.all_files_list.selection_set(source_iid)
                app.add_selected_to_manual(manual)
                self.assertEqual(manual["files"], [unmatched])
                self.assertEqual(len(app.group_widgets), 1)

                deadline = time.time() + 5
                while app._page_tasks and time.time() < deadline:
                    root.update()
                    time.sleep(0.01)
                self.assertFalse(app._page_tasks)
                review_ui = app.group_widgets[0]
                for row in review_ui["rows"]:
                    row["use"].set(True)
                app._show_merge_all_summary = lambda text: (
                    summary_prompts.append(text) or "proceed"
                )
                app.merge_all()
                deadline = time.time() + 10
                while app._merge_running and time.time() < deadline:
                    root.update()
                    time.sleep(0.01)
                self.assertFalse(app._merge_running)
                self.assertEqual(len(summary_prompts), 1)
                self.assertIn("Eligible groups: 2", summary_prompts[0])
                self.assertIn("Selected PDFs: 3", summary_prompts[0])
                self.assertIn("Selected review-required groups: 1", summary_prompts[0])
                outputs = list(source_dir.rglob("*_MERGED*.pdf"))
                self.assertEqual(len(outputs), 2)
                self.assertEqual(len(app.cleanup_candidates), 3)
                self.assertTrue(all(source.exists() for source in sources))
                app._add_paths([source_dir])
                self.assertEqual(len(app.files), 3)
                self.assertEqual(len(app.cleanup_candidates), 3)
                extra_folder = source_dir / "additional"
                extra_folder.mkdir()
                extra_source = extra_folder / "ANOTHER SYNTHETIC PERSON.pdf"
                writer = PdfWriter()
                writer.add_blank_page(width=100, height=100)
                with extra_source.open("wb") as stream:
                    writer.write(stream)
                app._add_paths([extra_folder])
                self.assertIn(manual, app.manual_groups)
                self.assertEqual(len(app.cleanup_candidates), 3)
                self.assertEqual(len(app.files), 4)
        finally:
            merger_module.messagebox.showinfo = original_showinfo
            root.destroy()


class AssignmentTests(unittest.TestCase):
    def test_assignment_moves_source_and_prevents_duplicates(self):
        source = Path("left/source.pdf")
        automatic = {"files": [source]}
        manual = {"files": []}

        self.assertTrue(assign_source_to_group(source, [automatic, manual], manual))
        self.assertEqual(automatic["files"], [])
        self.assertEqual(manual["files"], [source])
        self.assertFalse(assign_source_to_group(source, [automatic, manual], manual))
        self.assertEqual(manual["files"], [source])

    def test_multiple_manual_groups_keep_independent_sources(self):
        first, second = Path("left/first.pdf"), Path("right/second.pdf")
        automatic = {"files": []}
        manual_one, manual_two = {"files": []}, {"files": []}
        groups = [automatic, manual_one, manual_two]
        self.assertTrue(assign_source_to_group(first, groups, manual_one))
        self.assertTrue(assign_source_to_group(second, groups, manual_two))
        self.assertEqual(manual_one["files"], [first])
        self.assertEqual(manual_two["files"], [second])
        self.assertEqual(automatic["files"], [])


class MergeEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.output_dir = self.root / "Merged_PDFs"

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_pdf(self, filename, widths):
        path = self.root / filename
        writer = PdfWriter()
        for width in widths:
            writer.add_blank_page(width=width, height=100)
        with path.open("wb") as stream:
            writer.write(stream)
        return path

    def test_unrelated_manual_files_merge_in_custom_order_and_page_ranges(self):
        first = self.make_pdf("Unrelated_Alpha.pdf", [101, 102, 103])
        second = self.make_pdf("Completely_Different.pdf", [201, 202])

        output, included = merge_pdf_selection(
            [(second, 2, 2), (first, 1, 2)],
            self.output_dir,
            "Patient Manual",
        )

        reader = PdfReader(str(output))
        self.assertEqual(len(reader.pages), 3)
        self.assertEqual(
            [float(page.mediabox.width) for page in reader.pages],
            [202.0, 101.0, 102.0],
        )
        self.assertEqual(included, [second, first])

    def test_existing_output_is_never_overwritten(self):
        source = self.make_pdf("Source.pdf", [100])
        self.output_dir.mkdir()
        existing = self.output_dir / "Patient_MERGED.pdf"
        existing.write_bytes(b"keep this existing file")

        output, _included = merge_pdf_selection(
            [(source, 1, 1)], self.output_dir, "Patient"
        )

        self.assertEqual(existing.read_bytes(), b"keep this existing file")
        self.assertEqual(output.name, "Patient_MERGED_2.pdf")

    def test_same_folder_group_outputs_to_its_source_folder(self):
        source_folder = self.root / "nested" / "patient"
        source_folder.mkdir(parents=True)
        source = source_folder / "Source.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        with source.open("wb") as stream:
            writer.write(stream)

        destination = source_folder_for_selection([(source, 1, 1)])
        output, included = merge_pdf_selection([(source, 1, 1)], destination, "Patient")

        self.assertEqual(destination, source_folder.resolve())
        self.assertEqual(output.parent.resolve(), source_folder.resolve())
        self.assertEqual(included, [source])

    def test_group_outputs_are_created_in_each_source_folder(self):
        source_folders = [
            self.root / "first",
            self.root / "nested" / "second",
        ]
        selection = []
        for index, folder in enumerate(source_folders, start=1):
            folder.mkdir(parents=True)
            source = folder / f"Source_{index}.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            with source.open("wb") as stream:
                writer.write(stream)
            selection.append((source, 1, 1))

        output_dirs = merged_output_directories([source for source, *_ in selection])
        outputs, included = merge_pdf_selection_to_folders(
            selection, output_dirs, "Patient"
        )

        self.assertEqual(
            {output.parent.resolve() for output in outputs},
            {(folder / "Merged_PDFs").resolve() for folder in source_folders},
        )
        self.assertEqual(len(outputs), 2)
        self.assertEqual(included, [source for source, *_ in selection])
        self.assertTrue(all(len(PdfReader(str(output)).pages) == 2 for output in outputs))

    def test_multi_folder_output_failure_removes_partial_outputs(self):
        source = self.make_pdf("Source.pdf", [100])
        blocked_parent = self.root / "not-a-folder"
        blocked_parent.write_text("block directory creation", encoding="utf-8")
        output_dirs = [
            self.root / "first" / "Merged_PDFs",
            blocked_parent / "Merged_PDFs",
        ]

        with self.assertRaises(OSError):
            merge_pdf_selection_to_folders(
                [(source, 1, 1)], output_dirs, "Patient"
            )

        self.assertFalse((output_dirs[0] / "Patient_MERGED.pdf").exists())

    def test_cross_folder_group_requires_an_explicit_destination(self):
        first = self.make_pdf("First.pdf", [100])
        second_folder = self.root / "nested"
        second_folder.mkdir()
        second = second_folder / "Second.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        with second.open("wb") as stream:
            writer.write(stream)
        selection = [(first, 1, 1), (second, 1, 1)]

        self.assertIsNone(source_folder_for_selection(selection))
        self.assertIsNone(source_folder_for_paths([first, second]))
        chosen_destination = self.root / "user-chosen"
        output, _included = merge_pdf_selection(selection, chosen_destination, "Patient")
        self.assertEqual(output.parent, chosen_destination)

    def test_unchecked_group_source_still_requires_cross_folder_destination(self):
        checked = self.make_pdf("Checked.pdf", [100])
        unchecked_folder = self.root / "unchecked"
        unchecked_folder.mkdir()
        unchecked = unchecked_folder / "Unchecked.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        with unchecked.open("wb") as stream:
            writer.write(stream)

        self.assertIsNone(source_folder_for_paths([checked, unchecked]))

    def test_invalid_or_missing_selected_pdf_leaves_no_output(self):
        source = self.make_pdf("Valid.pdf", [100])
        missing = self.root / "Missing.pdf"
        with self.assertRaises(FileNotFoundError):
            merge_pdf_selection(
                [(source, 1, 1), (missing, 1, 1)],
                self.output_dir,
                "Failed Group",
            )
        self.assertFalse(self.output_dir.exists())

    def test_corrupted_and_password_protected_pdfs_are_rejected(self):
        corrupted = self.root / "Corrupted.pdf"
        corrupted.write_bytes(b"not a PDF")
        protected = self.make_pdf("Protected.pdf", [100])
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt("test-password")
        with protected.open("wb") as stream:
            writer.write(stream)

        with self.assertRaises(Exception):
            merge_pdf_selection([(corrupted, 1, 1)], self.output_dir, "Corrupted")
        with self.assertRaisesRegex(ValueError, "password-protected"):
            merge_pdf_selection([(protected, 1, 1)], self.output_dir, "Protected")
        self.assertFalse(self.output_dir.exists())

    def test_invalid_page_ranges_are_rejected(self):
        self.assertIsNone(validate_page_range(1, 3, 3))
        self.assertIsNotNone(validate_page_range(0, 1, 3))
        self.assertIsNotNone(validate_page_range(2, 1, 3))
        self.assertIsNotNone(validate_page_range(1, 4, 3))

    def test_cleanup_candidates_come_only_from_successful_outputs(self):
        included_source = self.make_pdf("Included.pdf", [100])
        unchecked_source = self.make_pdf("Unchecked.pdf", [100])
        failed_source = self.make_pdf("Failed.pdf", [100])
        successful = (self.output_dir / "ok.pdf", [included_source], "OK")

        candidates = cleanup_paths_from_successes([successful])

        self.assertEqual(list(candidates.values()), [included_source])
        self.assertNotIn(str(unchecked_source), candidates)
        self.assertNotIn(str(failed_source), candidates)
        self.assertEqual(cleanup_paths_from_successes([]), {})

    def test_cleanup_history_accumulates_and_deduplicates_between_merge_runs(self):
        first = self.make_pdf("First.pdf", [100])
        second = self.make_pdf("Second.pdf", [100])
        first_run = [(self.output_dir / "one.pdf", [first], "One")]
        second_run = [
            (self.output_dir / "two.pdf", [second, first], "Two"),
        ]

        candidates = accumulate_cleanup_candidates({}, first_run)
        candidates = accumulate_cleanup_candidates(candidates, second_run)

        self.assertEqual(set(candidates.values()), {first, second})

    def test_cleanup_clear_moves_only_candidates_and_keeps_failures(self):
        listed_success = self.make_pdf("ListedSuccess.pdf", [100])
        listed_failure = self.make_pdf("ListedFailure.pdf", [100])
        unrelated = self.make_pdf("NotListed.pdf", [100])
        moved_to_recycle_bin = []

        def trash(path):
            source = Path(path)
            if source == listed_failure:
                raise OSError("Recycle Bin unavailable")
            moved_to_recycle_bin.append(source)

        moved, failures = move_cleanup_sources_to_trash(
            [listed_success, listed_failure], trash
        )

        self.assertEqual(moved, [listed_success])
        self.assertEqual([path for path, _error in failures], [listed_failure])
        self.assertEqual(moved_to_recycle_bin, [listed_success])
        self.assertTrue(listed_failure.exists())
        self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
