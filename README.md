<img src="https://github.com/neutron-au/blogmd/blob/main/logo.png?raw=true" alt="logo" width="128"/>

# BlogMD
#### Neutron Australia

BlogMD is a markdown blog server. Posts, users and media metadata live in a database behind a
storage abstraction (SQLite out of the box), the public site has a sidebar navigation tree, and
the **Writers area** at `/write/` lets people sign in, write posts with a live preview, and
drag & drop images, video, audio and files straight into the editor.

## Quick start

```bash
python -m venv venv && . venv/bin/activate
python -m pip install -r requirements.txt

# give an imported author a password (or create a new account)
python src/main.py set-password mitch
python src/main.py create-user wren --display-name "Wren" [--admin]

python src/main.py            # serves on http://0.0.0.0:8001
```

`CONTENT_PATH` defaults to the `content` folder next to `src/`, so you can launch from any directory.

> **Windows:** if you have more than one Python installed, `py` and `pip` may point at different ones
> ("No module named 'flask'" even though pip says it's installed). Install and run with the same interpreter:
> `py -m pip install -r requirements.txt` then `py src\main.py`.
On the first start against an empty database, everything in `content/blog/**/*.md`,
`content/author/*.json` and `content/error/*.md` is imported automatically. Re-run it any time:

```bash
python src/main.py import              # adds new files, skips existing paths
python src/main.py import --overwrite  # re-syncs posts from the markdown files
python src/main.py users               # list accounts
```

## Writers area (`/write/`)

| Page            | What it does                                                                                                                       |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `/write/login`  | Sign in. Failed attempts are throttled per IP + username; sessions rotate on login and die when the password changes.               |
| `/write/`       | Your posts (admins see everyone's) with status filters and search.                                                                 |
| `/write/new`    | Editor: Markdown on the left, **live preview** on the right (rendered by the same server pipeline as the public page).            |
| `/write/media`  | Media library: upload, browse, see stored metadata, edit alt text, see which posts use a file, delete.                            |
| `/write/account`| Display name, avatar, bio, password.                                                                                               |

Editor features:

* **Live preview** as you type. Only changed blocks are re-rendered, so images and maths don't flicker. Scroll is synced.
* **Drag & drop** files anywhere on the editor, **paste** screenshots, or use *Upload*. A placeholder is inserted at
  the cursor and swapped for the final embed when the upload finishes (images → `![]()`, video → `<video>`,
  audio → `<audio>`, other files → a link).
* **Media panel**: click a file to insert it, or drag it into the text at the exact spot you want.
* Draft **autosave**, `Ctrl+S` to save, a local crash backup offered back on reload, and conflict detection if the
  post was saved somewhere else while you were editing.
* Formatting toolbar + shortcuts (`Ctrl+B/I/K`), list continuation, `Tab` indent (press `Esc` first to tab out),
  `Ctrl+1/2/3` for write / split / preview.
* Post settings: URL path (auto-filled from the title), description, status (draft / published / unlisted) and nav order.

Roles: **admin** can edit everything and their raw HTML is trusted (the imported authors become admins).
**writer** can edit their own posts and media; their HTML is sanitised (scripts, event handlers and
`javascript:` URLs are stripped). Set `ALLOW_REGISTRATION=true` to let people create writer accounts themselves.

## Themes and emoji

* **Dark mode** follows the reader's OS setting by default. The toggle in the sidebar footer (and in the
  writers-area rail) cycles **System → Light → Dark**; the choice is saved per browser and applied before the
  page paints, so there's no flash. Colours are CSS custom properties at the top of `web/static/css/site.css`
  (light values in `:root`, dark values in the `prefers-color-scheme` / `[data-theme="dark"]` blocks).
* **Site icon**: the brand mark (sidebar, writers area, sign-in) and the favicon use
  `content/static/icon.png` if it exists, otherwise `content/static/app.ico`. Replace either file to rebrand;
  browsers pick up the change automatically. Pixel art stays crisp; a 16×16-grid icon (like the potion) looks best.
* **Emoji** (`:rocket:` shortcodes or pasted 🚀) are output as plain Unicode, and post content uses the
  font stack `Apple Color Emoji, Segoe UI Emoji, Noto Color Emoji`. Readers on macOS/iOS see Apple's emoji,
  Windows readers see Microsoft's, Android readers see Google's.

## Configuration

Environment variables (a `.env` file is read too, see `.env.example`):

| Variable                | Default                              | Purpose                                         |
| ----------------------- | ------------------------------------ | ----------------------------------------------- |
| `DEBUG`                 | `false`                              | Flask dev server instead of uvicorn             |
| `CONTENT_PATH`          | `content`                            | Markdown import source + `/static/` files       |
| `DATABASE_URL`          | `sqlite:///content/blogmd.db`        | Storage backend (the URL scheme picks it)       |
| `MEDIA_URL`             | `file://content/media`               | Where uploaded bytes go                         |
| `SECRET_KEY`            | generated once, stored in the DB     | Session signing key                             |
| `MAX_UPLOAD_MB`         | `50`                                 | Per-file upload limit                           |
| `ALLOW_REGISTRATION`    | `false`                              | Public sign-up for writer accounts              |
| `SESSION_COOKIE_SECURE` | `false`                              | Set `true` behind HTTPS                         |
| `SITE_NAME`             | `Mitch Naake`                        | Sidebar title and `og:site_name`                |

## Architecture

```
src/mdserver/
    domain.py              User, Post, Media dataclasses - no SQL, no HTTP
    storage/
        base.py            Database + UserRepository / PostRepository / MediaRepository /
                           SettingsRepository ABCs, FileStore ABC
        __init__.py        backend registry: open_database(url), open_file_store(url)
        sqlite/            SQLite implementation (stdlib sqlite3, WAL, versioned migrations)
        files.py           local-disk FileStore
    services/              importer, navigation tree, media uploads, auth (hashing, CSRF, throttle)
    render/                markdown pipeline (+ nh3 sanitiser for untrusted authors)
    web/                   Flask app factory, public + writer blueprints, templates, CSS/JS
    cli.py                 serve / import / create-user / set-password / users
```

The web layer only ever calls the repository interfaces, so nothing outside `storage/sqlite/`
knows it's SQLite.

### Database schema (SQLite)

* `users` — username (unique, case-insensitive), display name, role, avatar, bio, password hash
* `posts` — path (unique), title, description, markdown content, author, status, nav order, created/updated/published timestamps
* `media` — owner, original name, storage key, MIME type, size, SHA-256, width/height, duration, alt text, upload time
* `post_media` — which posts embed which media (kept in sync on every save)
* `settings` — key/value (e.g. the generated secret key)

Schema changes are appended to `storage/sqlite/schema.py`; `PRAGMA user_version` tracks what has run.

### Adding another database

```python
# src/mdserver/storage/postgres.py
from mdserver.storage import register_backend
from mdserver.storage.base import Database, UserRepository, PostRepository, MediaRepository, SettingsRepository

class PostgresDatabase(Database):
    schemes = ('postgresql', 'postgres')

    @classmethod
    def from_url(cls, url): ...
    def migrate(self): ...
    def transaction(self): ...
    def close(self): ...
    # plus self.users / self.posts / self.media / self.settings implementing the repository ABCs

register_backend(PostgresDatabase)
```

Import it once at startup (e.g. at the bottom of `storage/__init__.py`), then set
`DATABASE_URL=postgresql://user:pass@host/blogmd`. To prove it behaves the same, add it to
`BACKENDS` in `tests/conftest.py`: the whole contract suite in `tests/test_storage_contract.py`
runs against every backend listed there. Media bytes work the same way via `FileStore` /
`register_file_store` (e.g. an S3 store for `s3://bucket/prefix`).

## Content paths

Post paths map to URLs exactly as the original file layout did, and imported files keep their URLs:

| File                                       | Post path            | URL                                 |
| ------------------------------------------ | -------------------- | ----------------------------------- |
| `content/blog/index.md`                    | *(root)*             | `/blog/`                            |
| `content/blog/myblog/index.md`             | `myblog`             | `/blog/myblog/`                     |
| `content/blog/myblog.md`                   | `myblog`             | `/blog/myblog/` (index.md wins)     |
| `content/blog/myblog/hello-world.md`       | `myblog/hello-world` | `/blog/myblog/hello-world/`         |

A folder with no page of its own (e.g. `/blog/theta/`) gets an automatic listing of what's inside it.
Files in `content/static/` are still served at `/static/...`; uploads are served at `/media/<id>-<hash>/<name>`.

## Tests

```bash
python -m pytest
```
