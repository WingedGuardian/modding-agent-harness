import tempfile
from pathlib import Path
import unittest

from modding_harness import preferences


class PreferenceTests(unittest.TestCase):
    def test_sections_fences_and_parenthetical_headings(self):
        self.assertEqual(preferences.section("```md\n## My preferences\nfake\n```\n## My preferences (local)\nkeep\n## Other\nstop"), "keep")
        self.assertEqual(preferences.section("```unclosed\n## My preferences\nkept\n## Other\nstop"), "kept")

    def test_bounded_unicode_render_and_missing_section_guidance(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / preferences.FILE
            path.write_text("## My preferences\n" + "é" * 5000, encoding="utf-8")
            text = preferences.render(folder)
            self.assertIn("USER PREFERENCES", text)
            self.assertIn("truncated", text)
            self.assertNotIn("\ufffd", text)
            self.assertLess(len(text.encode()), preferences.CAP + 400)
            path.write_text("### My preferences\nretain these notes", encoding="utf-8")
            self.assertIn("NOT INJECTED", preferences.render(folder))

    def test_absence_and_ordinary_notes_never_become_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(preferences.render(folder), "")
            path = Path(folder) / preferences.FILE
            path.write_text("# Unclassified prior notes\nThis is evidence, not a preference.")
            self.assertEqual(preferences.render(folder), "")


if __name__ == "__main__":
    unittest.main()
