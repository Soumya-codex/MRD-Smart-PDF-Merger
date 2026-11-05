import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from mrd_pdf_merger import (
    accumulate_cleanup_candidates,
    assign_source_to_group,
    cleanup_paths_from_successes,
    make_groups,
    merge_pdf_selection,
    move_cleanup_sources_to_trash,
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

    def test_ratnakar_biswal_examples_share_one_group(self):
        self.assert_patient_files_grouped(
            [
                "RATNAKAR BISWAL COVER.pdf",
                "RATNAKAR BISWAL S5 2026101210027804.pdf",
                "RATNAKAR BISWAL TUMOUR BOARD.pdf",
            ],
            "RATNAKAR BISWAL",
        )

    def test_minor_spelling_difference_is_a_reviewable_possible_match(self):
        groups = make_groups([
            Path("NAZIA KHARAM COVER.pdf"),
            Path("NAZIA KHANAM S5 2026101410053501.pdf"),
        ])
        self.assertEqual(len(groups), 1)
        self.assertIn("Possible Match", groups[0]["level"])
        self.assertIn("Review Required", groups[0]["level"])
        self.assertEqual(len(groups[0]["files"]), 2)

    def test_different_complete_names_do_not_match_on_first_name_alone(self):
        groups = make_groups([
            Path("ANANTA KUMAR DAS COVER.pdf"),
            Path("ANANTA KUMAR ROUT COVER.pdf"),
        ])
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(group["level"] == "Review Required" for group in groups))

    def test_shared_prefix_with_different_tokens_is_not_a_fuzzy_match(self):
        groups = make_groups([
            Path("UNRELATED ONE COVER.pdf"),
            Path("UNRELATED TWO REPORT.pdf"),
        ])
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(group["level"] == "Review Required" for group in groups))


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
