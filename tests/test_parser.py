import unittest
from pathlib import Path

from internship.parser import parse_readme, role_id, SECTIONS

FIXTURE = Path(__file__).parent / "fixtures" / "README.md"


def table(rows, header="Software Engineering"):
    return f"## {header}\n<table><tbody>{rows}</tbody></table>"


def row(company="AMD", title="Firmware Rust Intern", location="Santa Clara", app='<a href="https://jobs.example/1">Apply</a><a href="https://simplify.jobs/track">Simplify</a>', age="1mo"):
    return "<tr>" + "".join(f"<td>{cell}</td>" for cell in (company, title, location, app, age)) + "</tr>"


class ParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.roles = parse_readme(FIXTURE.read_text())

    def test_real_fixture(self):
        self.assertEqual(len(self.roles), 1199)
        self.assertEqual({r["section"] for r in self.roles}, set(SECTIONS))

    def test_real_continuation_and_first_link(self):
        first, second = self.roles[:2]
        self.assertEqual(first["company"], "Schonfeld")
        self.assertEqual(second["company"], first["company"])
        self.assertEqual(second["flags"], first["flags"])
        self.assertIn("greenhouse.io/schonfeld/jobs/8171772", second["apply_url"])

    def test_real_multilocation(self):
        matching = [r for r in self.roles if "Westborough, MA" in r["locations"] and len(r["locations"]) == 7]
        self.assertTrue(matching)
        self.assertIn("Arlington County, Arlington, VA", matching[0]["locations"])
        self.assertFalse(any("locations" in loc for r in self.roles for loc in r["locations"]))

    def test_company_flags_carry_but_title_flags_do_not(self):
        roles = parse_readme(table(row("🔥 🛂 🇺🇸 🎓 AMD") + row("↳", "Embedded Intern")))
        self.assertTrue(all(roles[1]["flags"].values()))
        self.assertEqual(roles[1]["company"], "AMD")
        roles = parse_readme(table(row("AMD", "Firmware 🎓") + row("↳", "Embedded")))
        self.assertTrue(roles[0]["flags"]["advanced_degree"])
        self.assertFalse(roles[1]["flags"]["advanced_degree"])

    def test_closed_age_and_identity(self):
        result = parse_readme(table(row(app="🔒")))[0]
        self.assertTrue(result["closed"])
        self.assertEqual(result["source_age"], "1mo")
        self.assertNotIn("first_seen", result)
        self.assertEqual(role_id("AMD", "Firmware", "https://x"), role_id(" amd ", "firmware", "https://x"))
        self.assertNotEqual(role_id("AMD", "Firmware", "https://x"), role_id("AMD", "Firmware", "https://y"))

    def test_br_and_count(self):
        result = parse_readme(table(row(location="3 locations<br>NYC<br/>Santa Clara<br />Toronto, Canada")))[0]
        self.assertEqual(result["locations"], ["NYC", "Santa Clara", "Toronto, Canada"])

    def test_invalid_crawl_rejected(self):
        for source in ("Bad gateway", table(row("↳")), table("<tr><td>AMD</td></tr>"), table(row(app='<a href="javascript:alert(1)">Apply</a>'))):
            with self.subTest(source=source), self.assertRaises(ValueError):
                parse_readme(source)

    def test_continuation_does_not_cross_sections(self):
        with self.assertRaises(ValueError):
            parse_readme(table(row()) + "\n" + table(row("↳"), "Hardware Engineering"))


if __name__ == "__main__":
    unittest.main()
