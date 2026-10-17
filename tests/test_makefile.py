import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
HOW = (ROOT / "docs" / "how.txt").read_text(encoding="utf-8")

# "name: prerequisites ## help", skipping variable assignments (":=").
_RULE = re.compile(r"^([a-zA-Z_-]+):(?!=)(.*)$", re.M)


def rules() -> dict[str, str]:
    """Target name to the rest of its rule line, in Makefile order."""
    return {name: rest for name, rest in _RULE.findall(MAKEFILE)}


def phony() -> set[str]:
    return set(re.search(r"^\.PHONY:(.*)$", MAKEFILE, re.M).group(1).split())


class MakeHowTest(unittest.TestCase):
    def test_make_how_names_only_targets_the_makefile_has(self):
        named = set(re.findall(r"\bmake ([a-z][a-z-]*)", HOW))
        self.assertTrue(named)
        self.assertEqual(named - set(rules()), set())

    def test_make_how_fits_half_a_page_of_a_standard_terminal(self):
        lines = HOW.rstrip("\n").split("\n")
        self.assertLessEqual(len(lines), 30)
        self.assertLessEqual(max(len(line) for line in lines), 80)

    def test_make_how_lays_the_flow_out_in_three_to_five_steps_each_naming_its_target(self):
        steps = re.findall(r"^\s+(\d)\. (make [a-z-]+)", HOW, re.M)
        self.assertGreaterEqual(len(steps), 3)
        self.assertLessEqual(len(steps), 5)
        self.assertEqual([int(n) for n, _ in steps], list(range(1, len(steps) + 1)))
        self.assertIn("make ui", [target for _, target in steps])

    def test_make_how_comes_right_after_make_help(self):
        self.assertEqual(list(rules())[:2], ["help", "how"])
        self.assertIn("docs/how.txt", MAKEFILE.split("\nhow:", 1)[1].split("\n\n", 1)[0])


class MakeUiTest(unittest.TestCase):
    def test_make_ui_opens_the_review_interface(self):
        recipe = MAKEFILE.split("\nui:", 1)[1].split("\n\n", 1)[0]
        self.assertIn("python3 -m music2spotify review", recipe)

    def test_make_tui_and_the_earlier_make_review_are_the_same_target_as_make_ui(self):
        for alias in ("tui", "review"):
            prerequisites = rules()[alias].split("##", 1)[0].split()
            self.assertEqual(prerequisites, ["ui"], alias)
            # No recipe of its own, so the two can never drift apart.
            block = MAKEFILE.split(f"\n{alias}:", 1)[1].split("\n\n", 1)[0]
            self.assertNotIn("\n\t", block, alias)


class MakeHelpTest(unittest.TestCase):
    def test_every_target_carries_a_help_comment(self):
        missing = [name for name, rest in rules().items() if "## " not in rest]
        self.assertEqual(missing, [])

    def test_every_target_is_declared_phony(self):
        self.assertEqual(set(rules()) - phony(), set())
        self.assertEqual(phony() - set(rules()), set())


if __name__ == "__main__":
    unittest.main()
