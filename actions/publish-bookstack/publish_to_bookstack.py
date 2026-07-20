#!/usr/bin/env python3
"""
Converts README.md to a BookStack Portable ZIP and optionally publishes it.
Optionally bundles a Bruno API test collection as downloadable attachment.

Unified script for all MesoXPO repositories. Repository-specific values
(book name, product tag, instance ID) are passed via CLI arguments.

The --upload mode uses an **upsert strategy**: the book is identified by name.
If it already exists, pages are updated in-place (preserving IDs, URLs,
permissions and comments).  New pages are created, obsolete pages deleted.
If no book exists yet, it is created from scratch.

Usage:
  python publish_to_bookstack.py --upload                          # Publish with defaults from H1
  python publish_to_bookstack.py --upload --book-name "MESO API"   # Override book name
  python publish_to_bookstack.py --upload --product-tag "MesoXPO,developer-docs"  # Multi-tag (comma-separated)
  python publish_to_bookstack.py --no-collection                   # Skip API test collection
  python publish_to_bookstack.py --readme OTHER.md                 # Use different source file

Environment variables for --upload:
  BOOKSTACK_URL          - BookStack instance URL (e.g. https://docs.example.com)
  BOOKSTACK_TOKEN_ID     - API token ID
  BOOKSTACK_TOKEN_SECRET - API token secret
"""

import argparse
import io
import json
import os
import re
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


# ---------------------------------------------------------------------------
# Markdown inline -> HTML helpers (for book description which doesn't render MD)
# ---------------------------------------------------------------------------

def _md_inline_to_html(text: str) -> str:
    """Convert basic Markdown inline formatting to HTML."""
    # Code-Spans zuerst extrahieren und durch Platzhalter schuetzen: Sternchen
    # in Code (z.B. Wildcard-Muster wie `*.pdf;*.tif`) duerfen nicht von der
    # Bold-/Italic-Konvertierung als Hervorhebung interpretiert werden.
    code_spans: list[str] = []

    def _stash_code(m: re.Match) -> str:
        code_spans.append(m.group(1))
        return f"\x00{len(code_spans) - 1}\x00"

    text = re.sub(r'`(.+?)`', _stash_code, text)
    # Bold: **text** -> <strong>text</strong>
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    # Italic: *text* -> <em>text</em>
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    # Links: [text](url) -> <a href="url">text</a>
    text = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2">\1</a>', text)

    # Inline code: `text` -> <code>text</code> (HTML-Sonderzeichen escapen,
    # damit z.B. Generics wie `List<int>` nicht als Tags interpretiert werden)
    def _restore_code(m: re.Match) -> str:
        code = code_spans[int(m.group(1))]
        code = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<code>{code}</code>"

    return re.sub(r'\x00(\d+)\x00', _restore_code, text)


def _description_to_html(description: str) -> str:
    """Convert book description (Markdown) to HTML."""
    if not description:
        return ""
    paragraphs = [p.strip() for p in description.split("\n\n") if p.strip()]
    html_parts: list[str] = []
    for para in paragraphs:
        if para == "---":
            continue
        if para.startswith("> "):
            inner = _md_inline_to_html(para[2:])
            html_parts.append(f"<blockquote><p>{inner}</p></blockquote>")
        else:
            html_parts.append(f"<p>{_md_inline_to_html(para)}</p>")
    return "".join(html_parts)


# ---------------------------------------------------------------------------
# README parsing
# ---------------------------------------------------------------------------

def split_readme_into_sections(readme_path: str) -> tuple[str, str, list[dict]]:
    """Split README.md by H2 headings. Returns (book_name, description, sections)."""
    content = Path(readme_path).read_text(encoding="utf-8")
    lines = content.split("\n")

    book_name = ""
    description_lines: list[str] = []
    sections: list[dict] = []
    current_title: str | None = None
    current_lines: list[str] = []
    in_intro = True
    in_html_img_block = False
    intro_image_html: list[str] = []

    for line in lines:
        if in_intro and line.startswith("# "):
            book_name = line.lstrip("# ").strip()
            continue

        if line.startswith("## "):
            if in_intro:
                description_lines = current_lines[:]
                in_intro = False
            elif current_title is not None:
                sections.append(
                    {"title": current_title, "content": "\n".join(current_lines).strip()}
                )
            current_title = line[3:].strip()
            current_lines = []
            continue

        if in_intro:
            stripped = line.strip()
            if stripped.startswith("[!["):
                continue
            # HTML-Bild-Bloecke (z.B. zentriertes Logo) erkennen und separat
            # erfassen, damit sie nicht in der Buchbeschreibung landen
            if stripped.startswith("<p") and "align" in stripped:
                in_html_img_block = True
                intro_image_html.append(line)
                continue
            if in_html_img_block:
                intro_image_html.append(line)
                if "</p>" in stripped:
                    in_html_img_block = False
                continue
            current_lines.append(line)
        else:
            current_lines.append(line)

    if current_title:
        sections.append(
            {"title": current_title, "content": "\n".join(current_lines).strip()}
        )

    # Erfasstes Logo-HTML an den Anfang der ersten Seite setzen
    if intro_image_html and sections:
        logo_block = "\n".join(intro_image_html).strip()
        sections[0]["content"] = logo_block + "\n\n" + sections[0]["content"]

    description = "\n".join(description_lines).strip()
    return book_name, description, sections


# ---------------------------------------------------------------------------
# API test collection bundling
# ---------------------------------------------------------------------------

def bundle_collection(collection_dir: str) -> tuple[bytes | None, str | None]:
    """ZIP a Bruno API collection directory. Returns (zip_bytes, filename) or (None, None)."""
    collection_path = Path(collection_dir)
    if not collection_path.exists():
        return None, None

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(collection_path.rglob("*")):
            if file.is_file():
                zf.write(file, file.relative_to(collection_path.parent))

    filename = f"{collection_path.name}.zip"
    return buf.getvalue(), filename


# ---------------------------------------------------------------------------
# BookStack Portable ZIP (offline export)
# ---------------------------------------------------------------------------

def build_data_json(
    book_name: str, description: str, sections: list[dict],
    product_tag: str, instance_id: str,
    attachment_files: list[dict] | None = None,
) -> dict:
    """Build the BookStack Portable ZIP data.json structure."""
    attachment_files = attachment_files or []
    pages = []
    priority = 1

    for section in sections:
        if section["title"] == "Inhaltsverzeichnis":
            continue

        page_attachments = [
            {"id": a["id"], "name": a["display_name"], "file": a["filename"]}
            for a in attachment_files
            if a["target_page"] == section["title"]
        ]

        # Native Markdown-Pipe-Tabellen an BookStack durchreichen: BookStacks
        # eigener Renderer stylt sie (Rahmen, responsives Umbrechen). Roh-HTML-
        # Tabellen blieben ungestylt (rahmenlos) — daher bewusst KEINE HTML-
        # Umwandlung mehr.
        markdown = section["content"]
        if page_attachments:
            download_lines = ["\n\n---\n", "### Downloads\n"]
            for a in page_attachments:
                download_lines.append(
                    f"- [{a['name']}]([[bsexport:attachment:{a['id']}]])"
                )
            markdown += "\n".join(download_lines) + "\n"

        pages.append({
            "name": section["title"],
            "markdown": markdown,
            "priority": priority,
            "attachments": page_attachments,
            "images": [],
            "tags": [],
        })
        priority += 1

    desc_html = _description_to_html(description)

    return {
        "instance": {"id": instance_id, "version": ""},
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
        "book": {
            "name": book_name,
            "description_html": desc_html,
            "pages": pages,
            "tags": [
                *[{"name": "product", "value": t.strip()} for t in product_tag.split(",") if t.strip()],
                {"name": "source", "value": "README.md"},
                {"name": "generated", "value": datetime.now().strftime("%Y-%m-%d")},
            ],
        },
    }


def create_zip(data: dict, output_path: str,
               bundled_files: dict[str, bytes] | None = None) -> str:
    """Create the BookStack Portable ZIP file."""
    bundled_files = bundled_files or {}
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data.json", json.dumps(data, ensure_ascii=False, indent=2))
        for name, content in bundled_files.items():
            zf.writestr(f"files/{name}", content)
    return output_path


# ---------------------------------------------------------------------------
# BookStack REST API helpers (stdlib only, no external dependencies)
# ---------------------------------------------------------------------------

def _api_headers(token_id: str, token_secret: str, user_agent: str) -> dict:
    return {
        "Authorization": f"Token {token_id}:{token_secret}",
        "User-Agent": user_agent,
    }


def _api_request(method: str, url: str, headers: dict, data: bytes | None = None,
                 content_type: str | None = None) -> dict | bytes:
    """Make an HTTP request and return parsed JSON (or raw bytes)."""
    req = Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    if content_type:
        req.add_header("Content-Type", content_type)

    with urlopen(req, timeout=120) as resp:
        body = resp.read()
        ct = resp.headers.get("Content-Type", "")
        if "application/json" in ct:
            return json.loads(body)
        return body


def _api_json(method: str, url: str, headers: dict, payload: dict) -> dict:
    """Convenience: send JSON body and return parsed JSON."""
    body = json.dumps(payload, ensure_ascii=False).encode()
    return _api_request(method, url, headers, data=body,
                        content_type="application/json")


def _build_multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    """Build a multipart/form-data body from fields and files."""
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []

    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )

    for name, (filename, filedata, mime) in files.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode()
            + filedata
            + b"\r\n"
        )

    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


def _api_get_all(url: str, headers: dict) -> list[dict]:
    """Paginate through a BookStack listing endpoint and return all items."""
    items: list[dict] = []
    offset = 0
    count = 100
    while True:
        sep = "&" if "?" in url else "?"
        page_url = f"{url}{sep}count={count}&offset={offset}"
        result = _api_request("GET", page_url, headers)
        data = result.get("data", [])
        items.extend(data)
        total = result.get("total", len(items))
        if len(items) >= total or not data:
            break
        offset += count
    return items


# ---------------------------------------------------------------------------
# Internal link rewriting for BookStack cross-page references
# ---------------------------------------------------------------------------

def _heading_slug(text: str) -> str:
    """Generate a GitHub-compatible heading anchor slug.

    Matches GitHub's algorithm: lowercase, strip punctuation (keep Unicode
    letters, digits, spaces, hyphens), replace spaces with hyphens.
    """
    slug = text.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug, flags=re.UNICODE)
    slug = re.sub(r'[\s]+', '-', slug)
    slug = re.sub(r'-+', '-', slug)
    return slug.strip('-')


def _build_heading_page_map(sections: list[dict]) -> dict[str, str]:
    """Map every heading anchor slug to the H2 page title it belongs to.

    Covers H2 titles themselves as well as H3–H6 sub-headings found
    in each section's markdown content.
    """
    heading_map: dict[str, str] = {}
    for section in sections:
        page_title = section["title"]
        heading_map[_heading_slug(page_title)] = page_title
        for m in re.finditer(r'^#{3,6}\s+(.+)$', section["content"], re.MULTILINE):
            heading_map[_heading_slug(m.group(1).strip())] = page_title
    return heading_map


def _rewrite_internal_links(
    markdown: str,
    current_page_title: str,
    heading_map: dict[str, str],
    page_slugs: dict[str, str],
    book_slug: str,
) -> str:
    """Rewrite ``](#anchor)`` links that point to headings on other BookStack pages.

    Same-page links are left unchanged.  Cross-page links are rewritten to
    ``/books/{book_slug}/page/{page_slug}``.
    """
    def _replace(m: re.Match) -> str:
        link_text, anchor = m.group(1), m.group(2)
        target_page = heading_map.get(anchor)
        if target_page is None or target_page == current_page_title:
            return m.group(0)  # unknown or same-page -> leave as-is
        page_slug = page_slugs.get(target_page)
        if not page_slug:
            return m.group(0)
        return f"[{link_text}](/books/{book_slug}/page/{page_slug})"

    return re.sub(r'\[([^\]]+)\]\(#([^)]+)\)', _replace, markdown)


def _strip_html_anchors(markdown: str) -> str:
    """Remove ``<a id="..."></a>`` anchor tags (used for GitHub compatibility)."""
    return re.sub(r'<a\s+id="[^"]*"\s*>\s*</a>\s*\n?', '', markdown)


# ---------------------------------------------------------------------------
# Cross-book link rewriting (links to other MD files that are also published)
# ---------------------------------------------------------------------------

def _parse_link_map(link_map_str: str) -> dict[str, str]:
    """Parse newline-separated ``file.md = book-name`` entries.

    Lines starting with ``#`` or empty lines are ignored.  Returns
    ``{filename: book-name}``.
    """
    result: dict[str, str] = {}
    if not link_map_str:
        return result
    for raw_line in link_map_str.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        src, target = line.split("=", 1)
        src = src.strip()
        target = target.strip()
        if src and target:
            result[src] = target
    return result


def _book_name_to_slug(name: str) -> str:
    """Derive a BookStack-compatible book slug from a book name.

    Matches BookStack's default slug algorithm (Str::slug):
    transliterate to ASCII, lowercase, non-alnum to hyphen, collapse hyphens.
    """
    # German umlaut transliteration
    translit = {
        "ä": "a", "ö": "o", "ü": "u", "ß": "ss",
        "Ä": "a", "Ö": "o", "Ü": "u",
    }
    slug = "".join(translit.get(c, c) for c in name).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def _resolve_link_map_slugs(
    link_map: dict[str, str],
    base_url: str | None,
    headers: dict | None,
) -> dict[str, str]:
    """For each ``file.md -> book-name`` entry, resolve the book slug.

    If API access is available (base_url + headers), look up the real slug
    via BookStack API.  Otherwise (or if the book doesn't exist yet),
    fall back to a deterministic slug derived from the book name.
    """
    result: dict[str, str] = {}
    for filename, book_name in link_map.items():
        slug = None
        if base_url and headers:
            try:
                _, slug = find_book(base_url, headers, book_name)
            except Exception:
                slug = None
        if not slug:
            slug = _book_name_to_slug(book_name)
        result[filename] = slug
    return result


def _rewrite_cross_book_links(markdown: str, slug_map: dict[str, str]) -> str:
    """Rewrite ``](file.md)`` and ``](file.md#anchor)`` links to other
    BookStack books.

    Targets are matched by exact path, by basename, or by suffix match,
    so both ``docs/foo.md`` and ``foo.md`` resolve to the same slug.
    Anchors are dropped — BookStack page anchors would require knowledge
    of the target book's page structure, which we don't have here.
    """
    if not slug_map:
        return markdown

    # Build lookup variants: exact path AND basename for each entry
    lookup: dict[str, str] = {}
    for path, slug in slug_map.items():
        lookup[path] = slug
        basename = path.rsplit("/", 1)[-1]
        lookup.setdefault(basename, slug)

    def _replace(m: re.Match) -> str:
        link_text, target = m.group(1), m.group(2)
        file_part = target.split("#", 1)[0]

        slug = lookup.get(file_part)
        if slug is None:
            # Try suffix match (handles "docs/foo.md" vs "foo.md" mismatches)
            for path, candidate in slug_map.items():
                if file_part.endswith("/" + path) or path.endswith("/" + file_part):
                    slug = candidate
                    break

        if slug is None:
            return m.group(0)
        return f"[{link_text}](/books/{slug})"

    return re.sub(r"\[([^\]]+)\]\(([^)]+\.md(?:#[^)]*)?)\)", _replace, markdown)


# ---------------------------------------------------------------------------
# BookStack upsert logic
# ---------------------------------------------------------------------------

def find_book(base_url: str, headers: dict, book_name: str) -> tuple[int | None, str | None]:
    """Find an existing book by exact name match. Returns (book_id, slug) or (None, None)."""
    books = _api_get_all(urljoin(base_url, "/api/books"), headers)
    for book in books:
        if book.get("name") == book_name:
            return book["id"], book.get("slug", "")
    return None, None


def create_book(base_url: str, headers: dict, book_name: str,
                description_html: str, tags: list[dict]) -> tuple[int, str]:
    """Create a new book and return (book_id, slug)."""
    url = urljoin(base_url, "/api/books")
    payload = {"name": book_name, "description_html": description_html, "tags": tags}
    result = _api_json("POST", url, headers, payload)
    return result["id"], result.get("slug", "")


def update_book(base_url: str, headers: dict, book_id: int,
                description_html: str, tags: list[dict]) -> None:
    """Update an existing book's description and tags."""
    url = urljoin(base_url, f"/api/books/{book_id}")
    payload = {"description_html": description_html, "tags": tags}
    _api_json("PUT", url, headers, payload)


def get_book_pages(base_url: str, headers: dict, book_id: int) -> list[dict]:
    """Get all pages belonging to a specific book."""
    all_pages = _api_get_all(urljoin(base_url, "/api/pages"), headers)
    return [p for p in all_pages if p.get("book_id") == book_id]


def create_page(base_url: str, headers: dict, book_id: int,
                name: str, markdown: str, priority: int) -> dict:
    """Create a new page in a book."""
    url = urljoin(base_url, "/api/pages")
    payload = {
        "book_id": book_id,
        "name": name,
        "markdown": markdown,
        "priority": priority,
    }
    return _api_json("POST", url, headers, payload)


def update_page(base_url: str, headers: dict, page_id: int,
                name: str, markdown: str, priority: int) -> dict:
    """Update an existing page's content and priority."""
    url = urljoin(base_url, f"/api/pages/{page_id}")
    payload = {
        "name": name,
        "markdown": markdown,
        "priority": priority,
    }
    return _api_json("PUT", url, headers, payload)


def delete_page(base_url: str, headers: dict, page_id: int) -> None:
    """Delete a page."""
    url = urljoin(base_url, f"/api/pages/{page_id}")
    _api_request("DELETE", url, headers)


def get_page_attachments(base_url: str, headers: dict, page_id: int) -> list[dict]:
    """Get all attachments for a specific page."""
    all_attachments = _api_get_all(urljoin(base_url, "/api/attachments"), headers)
    return [a for a in all_attachments if a.get("uploaded_to") == page_id]


def upsert_attachment(base_url: str, headers: dict, page_id: int,
                      display_name: str, file_bytes: bytes, filename: str,
                      existing_attachments: list[dict]) -> dict:
    """Create or update an attachment on a page."""
    existing = None
    for a in existing_attachments:
        if a.get("name") == display_name:
            existing = a
            break

    if existing:
        url = urljoin(base_url, f"/api/attachments/{existing['id']}")
        body, ct = _build_multipart(
            {"name": display_name, "uploaded_to": str(page_id)},
            {"file": (filename, file_bytes, "application/zip")},
        )
        return _api_request("PUT", url, headers, data=body, content_type=ct)
    else:
        url = urljoin(base_url, "/api/attachments")
        body, ct = _build_multipart(
            {"name": display_name, "uploaded_to": str(page_id)},
            {"file": (filename, file_bytes, "application/zip")},
        )
        return _api_request("POST", url, headers, data=body, content_type=ct)


def publish_to_bookstack(
    base_url: str, headers: dict, book_name: str,
    description_html: str, tags: list[dict],
    pages_data: list[dict],
    attachment_config: list[dict] | None = None,
    bundled_files: dict[str, bytes] | None = None,
) -> int:
    """Publish content to BookStack using upsert strategy.

    Returns the book ID.

    pages_data: list of {"name": str, "markdown": str, "priority": int}
    attachment_config: list of {"display_name": str, "filename": str, "target_page": str}
    bundled_files: dict mapping filename -> bytes
    """
    attachment_config = attachment_config or []
    bundled_files = bundled_files or {}

    # --- Step 1: Find or create book ---
    book_id, book_slug = find_book(base_url, headers, book_name)
    if book_id:
        print(f"  Buch gefunden: '{book_name}' (ID {book_id})")
        update_book(base_url, headers, book_id, description_html, tags)
        print(f"  Buch-Metadaten aktualisiert.")
    else:
        book_id, book_slug = create_book(base_url, headers, book_name, description_html, tags)
        print(f"  Neues Buch erstellt: '{book_name}' (ID {book_id})")

    # --- Step 2: Load existing pages for this book ---
    existing_pages = get_book_pages(base_url, headers, book_id)
    existing_by_name: dict[str, dict] = {p["name"]: p for p in existing_pages}
    desired_names = {p["name"] for p in pages_data}

    # --- Step 3: Upsert pages ---
    updated = 0
    created = 0
    for page in pages_data:
        name = page["name"]
        markdown = page["markdown"]
        priority = page["priority"]

        if name in existing_by_name:
            page_id = existing_by_name[name]["id"]
            update_page(base_url, headers, page_id, name, markdown, priority)
            updated += 1
            print(f"    Aktualisiert: {name} (ID {page_id})")
        else:
            result = create_page(base_url, headers, book_id, name, markdown, priority)
            created += 1
            print(f"    Neu erstellt: {name} (ID {result['id']})")

    # --- Step 4: Delete obsolete pages ---
    deleted = 0
    for name, page_info in existing_by_name.items():
        if name not in desired_names:
            delete_page(base_url, headers, page_info["id"])
            deleted += 1
            print(f"    Geloescht: {name} (ID {page_info['id']})")

    print(f"  Seiten: {updated} aktualisiert, {created} neu, {deleted} geloescht")

    # --- Step 4b: Ensure correct page order ---
    # BookStack may ignore priority on POST (page creation).
    # Re-apply priority via PUT on all newly created pages.
    if created > 0:
        all_pages = get_book_pages(base_url, headers, book_id)
        pages_by_name = {p["name"]: p for p in all_pages}
        reordered = 0
        for page in pages_data:
            existing = pages_by_name.get(page["name"])
            if existing and existing.get("priority") != page["priority"]:
                update_page(base_url, headers, existing["id"],
                            page["name"], page["markdown"], page["priority"])
                reordered += 1
        if reordered:
            print(f"  Reihenfolge korrigiert: {reordered} Seiten")

    # --- Step 5: Rewrite cross-page internal links ---
    if book_slug:
        heading_map = _build_heading_page_map(
            [{"title": p["name"], "content": p["markdown"]} for p in pages_data]
        )
        all_pages_for_links = get_book_pages(base_url, headers, book_id)
        page_slug_map = {p["name"]: p["slug"] for p in all_pages_for_links}
        page_id_map = {p["name"]: p["id"] for p in all_pages_for_links}

        link_updates = 0
        for page in pages_data:
            original_md = page["markdown"]
            cleaned_md = _strip_html_anchors(original_md)
            rewritten_md = _rewrite_internal_links(
                cleaned_md, page["name"], heading_map, page_slug_map, book_slug
            )
            if rewritten_md != original_md:
                pid = page_id_map.get(page["name"])
                if pid:
                    update_page(base_url, headers, pid,
                                page["name"], rewritten_md, page["priority"])
                    link_updates += 1

        if link_updates:
            print(f"  Interne Links umgeschrieben: {link_updates} Seiten")

    # --- Step 6: Upsert attachments ---
    page_link_updates: dict[str, list[tuple[str, int]]] = {}
    if attachment_config and bundled_files:
        if created > 0:
            existing_pages = get_book_pages(base_url, headers, book_id)
            existing_by_name = {p["name"]: p for p in existing_pages}

        for att in attachment_config:
            target_page_name = att["target_page"]
            target_page = existing_by_name.get(target_page_name)
            if not target_page:
                print(f"    Zielseite '{target_page_name}' nicht gefunden, "
                      f"Attachment '{att['display_name']}' uebersprungen.")
                continue

            page_id = target_page["id"]
            file_bytes = bundled_files.get(att["filename"])
            if not file_bytes:
                continue

            page_atts = get_page_attachments(base_url, headers, page_id)
            att_result = upsert_attachment(
                base_url, headers, page_id,
                att["display_name"], file_bytes, att["filename"], page_atts,
            )
            print(f"    Attachment: {att['display_name']} -> {target_page_name}")

            if isinstance(att_result, dict) and att_result.get("id"):
                page_link_updates.setdefault(target_page_name, []).append(
                    (att["display_name"], att_result["id"])
                )

    # --- Step 7: Update page content with clickable attachment links ---
    if page_link_updates:
        all_pages_current = get_book_pages(base_url, headers, book_id)
        pages_by_name_current = {p["name"]: p for p in all_pages_current}
        for target_page_name, link_list in page_link_updates.items():
            target = pages_by_name_current.get(target_page_name)
            if not target:
                continue
            page_detail = _api_request(
                "GET", urljoin(base_url, f"/api/pages/{target['id']}"), headers
            )
            if not isinstance(page_detail, dict):
                continue
            current_md = page_detail.get("markdown", "")
            updated_md = current_md
            for display_name, att_id in link_list:
                updated_md = updated_md.replace(
                    f"- {display_name}",
                    f"- [{display_name}](/attachments/{att_id})",
                )
            if updated_md != current_md:
                update_page(base_url, headers, target["id"], target["name"],
                            updated_md, target.get("priority", 1))
                print(f"    Attachment-Link eingefuegt: {target_page_name}")

    return book_id


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert README.md to BookStack Portable ZIP and optionally publish."
    )
    parser.add_argument(
        "--readme", default="README.md",
        help="Path to the README.md file (default: README.md)",
    )
    parser.add_argument(
        "--output", default="bookstack-import.zip",
        help="Output ZIP file path (default: bookstack-import.zip)",
    )
    parser.add_argument(
        "--book-name", default=None,
        help="Override the book name (default: extracted from H1)",
    )
    parser.add_argument(
        "--product-tag", default=None,
        help="Product tag(s) for BookStack — comma-separated for multi-tag (default: same as book name)",
    )
    parser.add_argument(
        "--instance-id", default=None,
        help="Instance ID for portable ZIP (default: derived from book name)",
    )
    parser.add_argument(
        "--user-agent", default="MesoXPO-DocPublisher/2.0",
        help="User-Agent header for API requests",
    )
    parser.add_argument(
        "--upload", action="store_true",
        help="Publish to BookStack via REST API (upsert: create or update)",
    )
    parser.add_argument(
        "--collection", default=None,
        help="Path to Bruno API collection directory (optional)",
    )
    parser.add_argument(
        "--collection-name", default=None,
        help="Display name for the collection attachment",
    )
    parser.add_argument(
        "--collection-target-page", default=None,
        help="Target page name for the collection attachment",
    )
    parser.add_argument(
        "--link-map", default=None,
        help=(
            "Map cross-MD-file links to other BookStack books. "
            "Either a path to a file or an inline string with one "
            "'filename.md = Book Name' entry per line."
        ),
    )
    args = parser.parse_args()

    readme_path = Path(args.readme)
    if not readme_path.exists():
        print(f"Error: {readme_path} not found.", file=sys.stderr)
        sys.exit(1)

    # 1. Parse and split README
    book_name, description, sections = split_readme_into_sections(str(readme_path))
    if args.book_name:
        book_name = args.book_name

    product_tag = args.product_tag or book_name
    instance_id = args.instance_id or book_name.lower().replace(" ", "-").replace(".", "-") + "-docs"

    page_sections = [s for s in sections if s["title"] != "Inhaltsverzeichnis"]
    print(f"Book: {book_name}")
    print(f"Product tag(s): {product_tag}")
    print(f"Instance ID: {instance_id}")
    print(f"Pages: {len(page_sections)}")

    # 1b. Cross-book link rewriting: ](other.md) -> ](/books/<slug>)
    link_map_raw = args.link_map or ""
    if link_map_raw:
        candidate = Path(link_map_raw)
        if candidate.is_file():
            link_map_raw = candidate.read_text(encoding="utf-8")
    link_map = _parse_link_map(link_map_raw)
    if link_map:
        slug_map = _resolve_link_map_slugs(link_map, None, None)
        for section in sections:
            section["content"] = _rewrite_cross_book_links(section["content"], slug_map)
        description = _rewrite_cross_book_links(description, slug_map)
        print(f"Cross-book links: {len(slug_map)} target(s) mapped (deterministic slugs)")

    # 2. Bundle API test collection (optional)
    attachment_config: list[dict] = []
    bundled_files: dict[str, bytes] = {}

    if args.collection:
        collection_bytes, collection_filename = bundle_collection(args.collection)
        if collection_bytes:
            display_name = args.collection_name or f"Bruno API Test Collection ({product_tag})"
            target_page = args.collection_target_page or page_sections[0]["title"] if page_sections else ""
            bundled_files[collection_filename] = collection_bytes
            attachment_config.append({
                "filename": collection_filename,
                "display_name": display_name,
                "target_page": target_page,
            })
            print(f"Collection: {args.collection} -> {collection_filename} "
                  f"({len(collection_bytes):,} bytes)")
        else:
            print(f"Collection: {args.collection} not found, skipping.")

    # 3. Build Portable ZIP (always, for artifact/offline use)
    zip_attachment_files = [
        {**a, "id": i + 1} for i, a in enumerate(attachment_config)
    ]
    data = build_data_json(book_name, description, sections, product_tag, instance_id,
                           zip_attachment_files)
    output_path = create_zip(data, args.output, bundled_files)
    zip_size = Path(output_path).stat().st_size
    print(f"ZIP created: {output_path} ({zip_size:,} bytes)")

    # 4. Publish via REST API
    if args.upload:
        base_url = os.environ.get("BOOKSTACK_URL", "").rstrip("/")
        token_id = os.environ.get("BOOKSTACK_TOKEN_ID", "")
        token_secret = os.environ.get("BOOKSTACK_TOKEN_SECRET", "")

        if not all([base_url, token_id, token_secret]):
            print(
                "Error: Set BOOKSTACK_URL, BOOKSTACK_TOKEN_ID, and "
                "BOOKSTACK_TOKEN_SECRET environment variables.",
                file=sys.stderr,
            )
            sys.exit(1)

        headers = _api_headers(token_id, token_secret, args.user_agent)

        desc_html = _description_to_html(description)

        tags = [
            *[{"name": "product", "value": t.strip()} for t in product_tag.split(",") if t.strip()],
            {"name": "source", "value": "README.md"},
            {"name": "generated", "value": datetime.now().strftime("%Y-%m-%d")},
        ]

        # Build pages list (without TOC, with download section for attachments)
        pages_data: list[dict] = []
        priority = 1
        for section in page_sections:
            # Native Markdown-Tabellen durchreichen (s. Kommentar in build_data_json).
            markdown = section["content"]

            page_atts = [a for a in attachment_config
                         if a["target_page"] == section["title"]]
            if page_atts:
                download_lines = ["\n\n---\n", "### Downloads\n"]
                for a in page_atts:
                    download_lines.append(f"- {a['display_name']}")
                markdown += "\n".join(download_lines) + "\n"

            pages_data.append({
                "name": section["title"],
                "markdown": markdown,
                "priority": priority,
            })
            priority += 1

        try:
            book_id = publish_to_bookstack(
                base_url, headers, book_name,
                desc_html, tags, pages_data,
                attachment_config, bundled_files,
            )
            book_url = f"{base_url}/books/{book_id}"
            print(f"  Book URL: {book_url}")
        except HTTPError as e:
            body = e.read().decode() if hasattr(e, "read") else ""
            print(f"Error: {e.code} {e.reason}", file=sys.stderr)
            if body:
                print(body, file=sys.stderr)
            sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    main()
