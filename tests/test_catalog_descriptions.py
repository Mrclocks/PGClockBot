import unittest
from types import SimpleNamespace as NS

from app.services.catalog_descriptions import describe_items, with_descriptions


class CatalogDescriptionTests(unittest.TestCase):
    def test_items_without_description_are_skipped(self) -> None:
        items = [NS(name="A", description=" "), NS(name="B", description=None)]
        self.assertEqual(with_descriptions("body", items), "body")

    def test_description_is_escaped_and_listed_under_name(self) -> None:
        text = with_descriptions("body", [NS(name="Gold <1>", description="<b>x</b>")])
        self.assertIn("<b>Gold &lt;1&gt;</b>\n&lt;b&gt;x&lt;/b&gt;", text)

    def test_long_description_is_clipped(self) -> None:
        self.assertLess(len(describe_items([NS(name="A", description="x" * 5000)])), 400)


if __name__ == "__main__":
    unittest.main()
