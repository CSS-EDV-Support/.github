#!/usr/bin/env python3
"""Unit tests for the link rewriting in publish_to_bookstack.

Run from this directory::

    python -m unittest test_publish_to_bookstack -v

These cover the two defects that made links dead in BookStack:

* headings containing ``&`` were unresolvable because consecutive hyphens
  were collapsed, so their links were never rewritten;
* same-page anchors were left untouched although BookStack replaces GitHub
  heading slugs with its own ``bkmrk-*`` ids.
"""

import unittest

from publish_to_bookstack import (
    _bookstack_anchor_id,
    _build_heading_page_map,
    _heading_slug,
    _rewrite_internal_links,
)


class HeadingSlugTests(unittest.TestCase):
    def test_matches_github_for_plain_heading(self):
        self.assertEqual(_heading_slug("Lizenz prüfen"), "lizenz-prüfen")

    def test_keeps_double_hyphen_from_removed_ampersand(self):
        # GitHub does not collapse the two hyphens the removed "&" leaves behind.
        self.assertEqual(_heading_slug("Anmeldung & Sitzung"), "anmeldung--sitzung")

    def test_strips_punctuation_but_keeps_digits(self):
        self.assertEqual(_heading_slug("1. Konfigurationsübersicht"), "1-konfigurationsübersicht")


class BookstackAnchorIdTests(unittest.TestCase):
    def test_url_encodes_umlauts(self):
        self.assertEqual(_bookstack_anchor_id("Lizenz prüfen", set()), "bkmrk-lizenz-pr%C3%BCfen")

    def test_truncates_text_to_twenty_characters(self):
        # "rollen-und-berechtig" is exactly 20 characters.
        self.assertEqual(
            _bookstack_anchor_id("Rollen und Berechtigungen einrichten", set()),
            "bkmrk-rollen-und-berechtig",
        )

    def test_lowercases_ascii_only(self):
        # PHP strtolower is byte-wise: a leading "Ä" survives unchanged.
        self.assertEqual(_bookstack_anchor_id("Änderungen", set()), "bkmrk-%C3%84nderungen")

    def test_appends_counter_for_duplicates(self):
        used: set[str] = set()
        self.assertEqual(_bookstack_anchor_id("Überblick", used), "bkmrk-%C3%9Cberblick")
        self.assertEqual(_bookstack_anchor_id("Überblick", used), "bkmrk-%C3%9Cberblick-1")
        self.assertEqual(_bookstack_anchor_id("Überblick", used), "bkmrk-%C3%9Cberblick-2")

    def test_counter_uses_truncated_base(self):
        used: set[str] = set()
        lang = "Konfiguration der Schnittstelle"
        # 20 characters of "konfiguration-der-schnittstelle" -> "konfiguration-der-sc"
        self.assertEqual(_bookstack_anchor_id(lang, used), "bkmrk-konfiguration-der-sc")
        self.assertEqual(_bookstack_anchor_id(lang, used), "bkmrk-konfiguration-der-sc-1")


SECTIONS = [
    {
        "title": "Ersteinrichtung (Administratoren)",
        "content": "### Lizenz prüfen\ntext\n\n### Mandanten freischalten\ntext\n",
    },
    {
        "title": "Anmeldung & Sitzung",
        "content": "### Zwei-Faktor\ntext\n",
    },
]


class RewriteInternalLinksTests(unittest.TestCase):
    def setUp(self):
        self.heading_map = _build_heading_page_map(SECTIONS)
        self.page_slugs = {
            "Ersteinrichtung (Administratoren)": "ersteinrichtung-administratoren",
            "Anmeldung & Sitzung": "anmeldung-sitzung",
        }

    def _rewrite(self, markdown, page="Ersteinrichtung (Administratoren)"):
        return _rewrite_internal_links(markdown, page, self.heading_map, self.page_slugs, "buch")

    def test_same_page_subheading_becomes_bkmrk_anchor(self):
        self.assertEqual(
            self._rewrite("siehe [Lizenz prüfen](#lizenz-prüfen)"),
            "siehe [Lizenz prüfen](#bkmrk-lizenz-pr%C3%BCfen)",
        )

    def test_cross_page_link_gets_page_url_and_anchor(self):
        self.assertEqual(
            self._rewrite("siehe [Zwei-Faktor](#zwei-faktor)"),
            "siehe [Zwei-Faktor](/books/buch/page/anmeldung-sitzung#bkmrk-zwei-faktor)",
        )

    def test_cross_page_link_to_page_title_has_no_fragment(self):
        # Heading with "&": resolvable only because hyphens are no longer collapsed.
        self.assertEqual(
            self._rewrite("siehe [Anmeldung](#anmeldung--sitzung)"),
            "siehe [Anmeldung](/books/buch/page/anmeldung-sitzung)",
        )

    def test_link_to_own_page_title_points_at_the_page(self):
        self.assertEqual(
            self._rewrite("[nach oben](#ersteinrichtung-administratoren)"),
            "[nach oben](/books/buch/page/ersteinrichtung-administratoren)",
        )

    def test_unknown_anchor_is_left_untouched(self):
        markdown = "siehe [Dark Mode](#dunkle-darstellung)"
        self.assertEqual(self._rewrite(markdown), markdown)

    def test_external_links_are_left_untouched(self):
        markdown = "siehe [Handbuch](https://example.com/a#b)"
        self.assertEqual(self._rewrite(markdown), markdown)


if __name__ == "__main__":
    unittest.main()
