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

import base64
import io
import tempfile
import zipfile
import unittest
from pathlib import Path
from unittest import mock

import publish_to_bookstack as pub
from publish_to_bookstack import (
    _bookstack_anchor_id,
    _build_heading_page_map,
    _collect_local_images,
    _heading_slug,
    _is_local_image_path,
    _rewrite_image_links,
    _rewrite_internal_links,
    build_data_json,
)

# Kleinstes gueltiges PNG (1x1 transparent) -- reicht, es wird nur durchgereicht.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
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


class LocalImagePathTests(unittest.TestCase):
    def test_relative_paths_are_local(self):
        self.assertTrue(_is_local_image_path("bilder/suche.png"))
        self.assertTrue(_is_local_image_path("./bilder/suche.png"))

    def test_absolute_and_remote_paths_are_not(self):
        for pfad in ["https://example.com/a.png", "http://example.com/a.png",
                     "//example.com/a.png", "/img/a.png", "data:image/png;base64,AAA"]:
            with self.subTest(pfad=pfad):
                self.assertFalse(_is_local_image_path(pfad))


class CollectLocalImagesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "bilder").mkdir()
        (self.dir / "bilder" / "suche.png").write_bytes(PNG_1PX)
        (self.dir / "bilder" / "notiz.svg").write_bytes(b"<svg/>")
        self.addCleanup(self._tmp.cleanup)

    def _sammle(self, *inhalte):
        sections = [{"title": f"Seite {i+1}", "content": c} for i, c in enumerate(inhalte)]
        return _collect_local_images(sections, self.dir)

    def test_finds_local_image_and_reads_bytes(self):
        bilder = self._sammle("Text\n\n![Suche](bilder/suche.png)\n")
        self.assertEqual(list(bilder), ["bilder/suche.png"])
        eintrag = bilder["bilder/suche.png"]
        self.assertEqual(eintrag["bytes"], PNG_1PX)
        self.assertEqual(eintrag["mime"], "image/png")
        self.assertEqual(eintrag["pages"], ["Seite 1"])

    def test_image_used_on_two_pages_is_collected_once(self):
        bilder = self._sammle("![A](bilder/suche.png)", "![B](bilder/suche.png)")
        self.assertEqual(len(bilder), 1)
        self.assertEqual(bilder["bilder/suche.png"]["pages"], ["Seite 1", "Seite 2"])

    def test_remote_images_are_ignored(self):
        self.assertEqual(self._sammle("![X](https://example.com/a.png)"), {})

    def test_missing_file_is_skipped(self):
        self.assertEqual(self._sammle("![X](bilder/fehlt.png)"), {})

    def test_unsupported_format_is_skipped(self):
        # BookStack nimmt nur png/jpeg/gif/webp an.
        self.assertEqual(self._sammle("![X](bilder/notiz.svg)"), {})

    def test_ignored_sections_contribute_nothing(self):
        sections = [{"title": "Intern",
                     "content": "<!-- bookstack:ignore -->\n![X](bilder/suche.png)"}]
        self.assertEqual(_collect_local_images(sections, self.dir), {})


class RewriteImageLinksTests(unittest.TestCase):
    def test_replaces_only_known_paths(self):
        md = "![Suche](bilder/suche.png) und ![Extern](https://example.com/a.png)"
        ergebnis = _rewrite_image_links(md, {"bilder/suche.png": "/uploads/images/suche.png"})
        self.assertEqual(
            ergebnis,
            "![Suche](/uploads/images/suche.png) und ![Extern](https://example.com/a.png)",
        )

    def test_keeps_optional_title(self):
        md = '![Suche](bilder/suche.png "Die Suchmaske")'
        ergebnis = _rewrite_image_links(md, {"bilder/suche.png": "/u/s.png"})
        self.assertEqual(ergebnis, '![Suche](/u/s.png "Die Suchmaske")')

    def test_leaves_plain_links_untouched(self):
        md = "[kein Bild](bilder/suche.png)"
        self.assertEqual(_rewrite_image_links(md, {"bilder/suche.png": "/u/s.png"}), md)


class ZipExportImageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "a.png").write_bytes(PNG_1PX)
        self.addCleanup(self._tmp.cleanup)

    def test_zip_uses_bsexport_token_and_declares_image(self):
        sections = [{"title": "Seite", "content": "![Alt](a.png)"}]
        bilder = _collect_local_images(sections, self.dir)
        data = build_data_json("Buch", "", sections, "produkt", "inst", None, bilder)
        seite = data["book"]["pages"][0]
        self.assertEqual(seite["markdown"], "![Alt]([[bsexport:image:1]])")
        self.assertEqual(
            seite["images"],
            [{"id": 1, "name": "a.png", "file": "a.png", "type": "gallery"}],
        )

    def test_image_on_two_pages_is_declared_once(self):
        sections = [{"title": "Eins", "content": "![A](a.png)"},
                    {"title": "Zwei", "content": "![B](a.png)"}]
        bilder = _collect_local_images(sections, self.dir)
        data = build_data_json("Buch", "", sections, "produkt", "inst", None, bilder)
        eins, zwei = data["book"]["pages"]
        # Deklaration nur bei der ersten Seite -- doppelte IDs verletzen die
        # Eindeutigkeitspruefung des BookStack-Imports.
        self.assertEqual(len(eins["images"]), 1)
        self.assertEqual(zwei["images"], [])
        # Der Verweis funktioniert trotzdem auf beiden Seiten.
        self.assertIn("[[bsexport:image:1]]", eins["markdown"])
        self.assertIn("[[bsexport:image:1]]", zwei["markdown"])

    def test_zip_without_images_keeps_empty_list(self):
        sections = [{"title": "Seite", "content": "nur Text"}]
        data = build_data_json("Buch", "", sections, "produkt", "inst")
        self.assertEqual(data["book"]["pages"][0]["images"], [])


class CollectLocalAttachmentsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "rechner.html").write_text("<html>Kalkulator</html>", encoding="utf-8")
        (self.dir / "muster.pdf").write_bytes(b"%PDF-1.4 ...")
        (self.dir / "nachbar.md").write_text("# Nachbar", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def _sammle(self, *inhalte):
        sections = [{"title": f"Seite {i+1}", "content": c} for i, c in enumerate(inhalte)]
        return pub._collect_local_attachments(sections, self.dir)

    def test_html_is_zipped_before_attaching(self):
        anhaenge = self._sammle("[Kalkulator](rechner.html)")
        eintrag = anhaenge["rechner.html"]
        self.assertEqual(eintrag["name"], "rechner.zip")
        with zipfile.ZipFile(io.BytesIO(eintrag["bytes"])) as zf:
            self.assertEqual(zf.namelist(), ["rechner.html"])
            self.assertEqual(zf.read("rechner.html").decode(), "<html>Kalkulator</html>")

    def test_other_types_are_attached_unchanged(self):
        anhaenge = self._sammle("[Muster](muster.pdf)")
        self.assertEqual(anhaenge["muster.pdf"]["name"], "muster.pdf")
        self.assertEqual(anhaenge["muster.pdf"]["bytes"], b"%PDF-1.4 ...")

    def test_markdown_targets_are_ignored(self):
        # Die behandelt das Cross-Book-Rewriting.
        self.assertEqual(self._sammle("[Nachbar](nachbar.md)"), {})

    def test_markdown_target_with_anchor_is_ignored(self):
        # Ohne Abtrennen des Ankers liefe das als "Datei nicht gefunden" auf.
        self.assertEqual(self._sammle("[Nachbar](nachbar.md#abschnitt)"), {})

    def test_pure_anchor_link_is_ignored(self):
        self.assertEqual(self._sammle("[Oben](#ueberblick)"), {})

    def test_images_are_not_treated_as_attachments(self):
        self.assertEqual(self._sammle("![Bild](rechner.html)"), {})

    def test_remote_and_missing_targets_are_skipped(self):
        self.assertEqual(self._sammle("[X](https://example.com/a.html)"), {})
        self.assertEqual(self._sammle("[X](fehlt.html)"), {})

    def test_same_file_on_two_pages_collected_once(self):
        anhaenge = self._sammle("[A](rechner.html)", "[B](rechner.html)")
        self.assertEqual(len(anhaenge), 1)
        self.assertEqual(anhaenge["rechner.html"]["pages"], ["Seite 1", "Seite 2"])

    def test_zip_export_declares_attachment_and_rewrites_link(self):
        sections = [{"title": "Seite", "content": "[Kalkulator](rechner.html)"}]
        anhaenge = pub._collect_local_attachments(sections, self.dir)
        data = build_data_json("Buch", "", sections, "t", "i", None, None, anhaenge)
        seite = data["book"]["pages"][0]
        self.assertEqual(seite["markdown"], "[Kalkulator]([[bsexport:attachment:1]])")
        self.assertEqual(
            seite["attachments"],
            [{"id": 1, "name": "rechner.zip", "file": "rechner.zip"}],
        )

    def test_attachment_ids_do_not_collide_with_collection(self):
        # Die Bruno-Collection belegt ID 1, die verlinkte Datei muss danach kommen.
        sections = [{"title": "Seite", "content": "[Kalkulator](rechner.html)"}]
        anhaenge = pub._collect_local_attachments(sections, self.dir)
        collection = [{"id": 1, "display_name": "API-Tests",
                       "filename": "api.zip", "target_page": "Seite"}]
        data = build_data_json("Buch", "", sections, "t", "i", collection, None, anhaenge)
        ids = [a["id"] for a in data["book"]["pages"][0]["attachments"]]
        self.assertEqual(ids, [1, 2])
        self.assertIn("[[bsexport:attachment:2]]", data["book"]["pages"][0]["markdown"])


class UpsertGalleryImageTests(unittest.TestCase):
    """Die Wiedererkennung ist der kritische Teil: ohne sie legt jeder Lauf
    eine weitere Kopie an, weil BookStack Dateinamen beim Upload eindeutig macht."""

    def setUp(self):
        self.aufrufe: list[dict] = []

        def _fake_api_request(method, url, headers, data=None, content_type=None):
            self.aufrufe.append({"method": method, "url": url})
            return {"id": 99, "name": "suche.png", "url": "/uploads/images/suche.png"}

        patcher = mock.patch.object(pub, "_api_request", _fake_api_request)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_uploads_when_gallery_is_empty(self):
        vorhandene: list[dict] = []
        url = pub.upsert_gallery_image("https://bs", {}, 7, "suche.png", PNG_1PX,
                                       "image/png", vorhandene)
        self.assertEqual(url, "/uploads/images/suche.png")
        self.assertEqual(len(self.aufrufe), 1)
        self.assertEqual(self.aufrufe[0]["method"], "POST")

    def test_reuses_existing_image_on_same_page(self):
        vorhandene = [{"name": "suche.png", "uploaded_to": 7, "url": "/uploads/alt.png"}]
        url = pub.upsert_gallery_image("https://bs", {}, 7, "suche.png", PNG_1PX,
                                       "image/png", vorhandene)
        self.assertEqual(url, "/uploads/alt.png")
        self.assertEqual(self.aufrufe, [], "es darf kein Upload stattfinden")

    def test_same_name_on_other_page_is_uploaded_separately(self):
        # uploaded_to gehoert zur Identitaet: dasselbe Bild auf einer anderen
        # Seite ist in BookStack ein eigener Galerie-Eintrag.
        vorhandene = [{"name": "suche.png", "uploaded_to": 3, "url": "/uploads/alt.png"}]
        url = pub.upsert_gallery_image("https://bs", {}, 7, "suche.png", PNG_1PX,
                                       "image/png", vorhandene)
        self.assertEqual(url, "/uploads/images/suche.png")
        self.assertEqual(len(self.aufrufe), 1)

    def test_second_call_in_same_run_reuses_first_upload(self):
        vorhandene: list[dict] = []
        for _ in range(2):
            pub.upsert_gallery_image("https://bs", {}, 7, "suche.png", PNG_1PX,
                                     "image/png", vorhandene)
        self.assertEqual(len(self.aufrufe), 1, "der zweite Aufruf darf nicht erneut hochladen")


if __name__ == "__main__":
    unittest.main()
