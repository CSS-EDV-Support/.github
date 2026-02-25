<p align="center">
  <img src="logo.png" alt="CSS EDV Support / MesoXPO" width="128">
</p>

# MesoXPO Organisation - Gemeinsame GitHub-Ressourcen

Dieses Repository enthält gemeinsame GitHub Actions, Workflow-Templates und Konfiguration fuer alle Repositories der MesoXPO-Organisation.

## Composite Actions

### `actions/publish-bookstack`

Publiziert eine `README.md` als BookStack-Buch mit Upsert-Strategie (erstellen oder aktualisieren).

**Features:**
- README wird anhand von H2-Headings in einzelne BookStack-Seiten aufgeteilt
- Buch-Metadaten (Beschreibung, Tags) werden automatisch aus dem Intro extrah
- Optionaler Upload von Bruno API-Test-Collections als Dateianhang
- Erzeugt immer ein Portable-ZIP-Artefakt (auch ohne Upload)

**Verwendung:**

```yaml
- uses: CSS-EDV-Support/.github/actions/publish-bookstack@main
  with:
    book-name: 'MESO API'
    product-tag: 'MESO WebAPI'
    bookstack-url: ${{ secrets.BOOKSTACK_URL }}
    bookstack-token-id: ${{ secrets.BOOKSTACK_TOKEN_ID }}
    bookstack-token-secret: ${{ secrets.BOOKSTACK_TOKEN_SECRET }}
```

**Alle Inputs:**

| Input | Pflicht | Default | Beschreibung |
|-------|---------|---------|--------------|
| `readme-path` | Nein | `README.md` | Pfad zur README-Datei |
| `book-name` | Nein | aus H1 | Buch-Name in BookStack |
| `product-tag` | Nein | = book-name | Product-Tag fuer Metadaten |
| `instance-id` | Nein | abgeleitet | Instance-ID im Portable ZIP |
| `upload` | Nein | `true` | Direkt zu BookStack publizieren |
| `collection-dir` | Nein | leer | Pfad zu Bruno-Collection |
| `collection-name` | Nein | automatisch | Anzeigename des Attachments |
| `collection-target-page` | Nein | automatisch | Zielseite fuer das Attachment |
| `bookstack-url` | Ja | - | BookStack-Instanz-URL |
| `bookstack-token-id` | Ja | - | API-Token-ID |
| `bookstack-token-secret` | Ja | - | API-Token-Secret |

## Workflow-Templates

Unter `workflow-templates/` liegen vorkonfigurierte Workflow-Dateien:

- **`documentation.yml`** - BookStack-Dokumentation publizieren
- **`purge-artifacts.yml`** - Alte Build-Artefakte automatisch bereinigen

Diese Templates erscheinen beim Erstellen neuer Workflows in Repositories der Organisation.

## Verwendung in Repositories

### winlineodataservice (MESO API)

```yaml
- uses: CSS-EDV-Support/.github/actions/publish-bookstack@main
  with:
    book-name: 'MESO API'
    product-tag: 'MESO WebAPI'
    instance-id: 'mesoapi-docs'
    collection-dir: 'ApiTestCollections/MesoAPI'
    collection-name: 'Bruno API Test Collection (MESO WebAPI)'
    collection-target-page: 'API-Dokumentation und Developer Portal'
```

### MesoXPO (Framework)

```yaml
- uses: CSS-EDV-Support/.github/actions/publish-bookstack@main
  with:
    book-name: 'MesoXPO'
    product-tag: 'MesoXPO'
    instance-id: 'mesoxpo-docs'
```

### mesoxpo-business (Business Library)

```yaml
- uses: CSS-EDV-Support/.github/actions/publish-bookstack@main
  with:
    book-name: 'MesoXPO.Business'
    product-tag: 'MesoXPO.Business'
    instance-id: 'mesoxpo-business-docs'
```

## Empfohlene Org-Level Secrets

Folgende Secrets sollten auf Organisations-Ebene konfiguriert werden:

| Secret | Beschreibung |
|--------|--------------|
| `BOOKSTACK_URL` | BookStack-Instanz-URL |
| `BOOKSTACK_TOKEN_ID` | BookStack API-Token-ID |
| `BOOKSTACK_TOKEN_SECRET` | BookStack API-Token-Secret |
