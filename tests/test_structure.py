"""The app is split into modules; these checks make sure they all load, in any order, without importing each other in a circle,
and that the entry point stays small."""
import importlib
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = ["ui_common", "data_sources", "maps", "charts", "pdfs", "views.compare_view", "views.spots_view",
           "views.conditions_view", "views.help_view", "web"]


class Structure(unittest.TestCase):
    def test_every_module_loads_on_its_own(self):
        """Each one is imported in a fresh Python, so a circular import cannot hide behind what another import loaded first."""
        for name in MODULES:
            result = subprocess.run([sys.executable, "-c", f"import {name}"], cwd=HERE, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{name} failed to import:\n{result.stderr[-600:]}")

    def test_web_still_offers_the_pages(self):
        sys.path.insert(0, HERE)
        web = importlib.import_module("web")
        for page in ("spots_page", "conditions_page", "help_page", "main"):
            self.assertTrue(callable(getattr(web, page)), page)

    def test_the_entry_point_stays_small(self):
        with open(os.path.join(HERE, "web.py"), encoding="utf-8") as f:
            self.assertLess(len(f.read().splitlines()), 60)


if __name__ == "__main__":
    unittest.main()
