<img src="https://github.com/neutron-au/blogmd/blob/main/logo.png?raw=true" alt="logo" width="128"/>

# BlogMD
#### Neutron Australia

BlogMD is a markdown blog server built on **FastAPI** with **MongoDB** storage. Posts, users, settings,
media metadata and the uploaded files themselves (GridFS) live in the database behind a storage
abstraction, so another database can be swapped in. The public site has a sidebar navigation tree, and
the **Writers area** at `/write/` lets people sign in, write posts with a live preview, and
drag & drop images, video, audio and files straight into the editor.

## Run it with Docker (recommended)

```bash
printf '%s' 'a-long-admin-password' | docker secret create blogmd_admin_password -   # once, 10+ characters
docker stack deploy -c docker-compose.yml blogmd
```

This starts two services:

* **mongo** — MongoDB 8.0 as a single-node replica set (needed for transactions). Its health check
  initiates the replica set on first start. It is only on a private `backend` network: no published port.
  All data lives in the `mongo-data` volume — back that up (e.g. `mongodump`).
* **blogmd** — the app. It waits for MongoDB, imports `content/` into an empty database, makes sure an
  **admin account** exists, and serves on port 8001 (behind Traefik in `docker-compose.yml`). It keeps no
  state of its own, runs as an unprivileged user, and has a `/healthz` health check.

Admin account (applied on every start):

| Variable               | Default | Purpose |
| ---------------------- | ------- | ------- |
| `ADMIN_USERNAME`       | `admin` | Account to create/repair. `mitch` uses the imported author. |
| `ADMIN_PASSWORD_FILE`  | –       | File with the password (Docker/Swarm secret, recommended). |
| `ADMIN_PASSWORD`       | –       | The password inline (visible in `docker inspect`). |
| `ADMIN_DISPLAY_NAME`   | username| Display name for a newly created account. |
| `ADMIN_RESET_PASSWORD` | `false` | Force the configured password onto an existing account. |

* No password configured → one is generated and printed **once** in `docker logs`.
* The configured password is only applied when the account has none, so changing it in the writers area
  survives restarts. Use `ADMIN_RESET_PASSWORD=true` for one start to force it back (e.g. lockout recovery).
* A bad value (e.g. a password under 10 characters) stops the container with a clear error.

Behind a reverse proxy set `TRUST_PROXY` (number of proxy hops; Traefik = `1`) and, behind Cloudflare,
`REAL_IP_HEADER=CF-Connecting-IP`, so login throttling sees real visitor IPs. Only set the latter if the
app is reachable solely through Cloudflare.

## Run it locally (no Docker)

```bash
python -m venv venv && . venv/bin/activate          # Windows: py -m venv venv; venv\Scripts\activate
python -m pip install -r requirements.txt            # requirements-dev.txt adds the test tools

python src/main.py set-password mitch                # give the imported author a password
python src/main.py                                   # http://0.0.0.0:8001   (--reload while developing)
```

Without `DATABASE_URL` it uses a local **SQLite** file (`content/blogmd.db`) and stores uploads in
`content/media`, so nothing else needs installing. To use MongoDB locally:

```bash
DATABASE_URL="mongodb://localhost:27017/blogmd?replicaSet=rs0" MEDIA_URL="gridfs://" python src/main.py
```

> **Windows:** if you have more than one Python installed, `py` and `pip` may point at different ones
> ("No module named ..." even though pip says it's installed). Install and run with the same interpreter:
> `py -m pip install -r requirements.txt` then `py src\main.py`.

On the first start against an empty database, everything in `content/blog/**/*.md`,
`content/author/*.json` and `content/error/*.md` is imported. Other commands:

```bash
python src/main.py import [--overwrite]   # add new markdown files (or re-sync existing posts)
python src/main.py create-user wren --display-name "Wren" [--admin]
python src/main.py set-password mitch      # also resets a forgotten password
python src/main.py ensure-admin            # apply the ADMIN_* variables without starting the server
python src/main.py users
```

Inside the container: `docker compose exec blogmd python src/main.py users`.

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
  audio → `<audio>`, other files → a link). Video/audio support seeking (HTTP Range), also from GridFS.
* **Media panel**: click a file to insert it, or drag it into the text at the exact spot you want.
* Draft **autosave**, `Ctrl+S` to save, a local crash backup offered back on reload, and conflict detection if the
  post was saved somewhere else while you were editing.
* Formatting toolbar + shortcuts (`Ctrl+B/I/K`), list continuation, `Tab` indent (press `Esc` first to tab out),
  `Ctrl+1/2/3` for write / split / preview. Code blocks get a **Copy** button.

Roles: **admin** can edit everything and their raw HTML is trusted (the imported authors become admins).
**writer** can edit their own posts and media; their HTML is sanitised (scripts, event handlers and
`javascript:` URLs are stripped). Set `ALLOW_REGISTRATION=true` to let people create writer accounts themselves.

## Themes and emoji

* **Dark mode** follows the reader's OS setting by default. The toggle in the sidebar footer (and in the
  writers-area rail) cycles **System → Light → Dark**; the choice is saved per browser and applied before the
  page paints. Colours are CSS custom properties at the top of `web/static/css/site.css`.
* **Site icon**: the brand mark and favicon use `content/static/icon.png` if it exists, otherwise
  `content/static/app.ico`. Pixel art stays crisp; a 16×16-grid icon (like the potion) looks best.
* **Emoji** are output as plain Unicode with an emoji font stack, so macOS/iOS readers see Apple's emoji,
  Windows readers Microsoft's, Android readers Google's.

## Configuration

Environment variables (a `.env` file is read too, see `.env.example`):

| Variable                | Default                              | Purpose |
| ----------------------- | ------------------------------------ | ------- |
| `DATABASE_URL`          | `sqlite:///content/blogmd.db` (image: `mongodb://mongo:27017/blogmd?replicaSet=rs0`) | Storage backend; the URL scheme picks it (`mongodb://`, `mongodb+srv://`, `sqlite://`) |
| `MEDIA_URL`             | `file://content/media` (image: `gridfs://`) | Uploaded bytes: `gridfs://` (in MongoDB) or `file://<dir>` |
| `DB_WAIT_SECONDS`       | `60`                                 | How long startup retries an unreachable database |
| `CONTENT_PATH`          | `content` (next to `src/`)           | Markdown import source + `/static/` files |
| `SECRET_KEY`            | generated once, stored in the DB     | Session cookie signing key |
| `MAX_UPLOAD_MB`         | `50`                                 | Per-file upload limit (larger requests get 413) |
| `ALLOW_REGISTRATION`    | `false`                              | Public sign-up for writer accounts |
| `SESSION_COOKIE_SECURE` | `false`                              | Set `true` behind HTTPS |
| `TRUST_PROXY`           | `0`                                  | Reverse-proxy hops to trust for `X-Forwarded-*` |
| `REAL_IP_HEADER`        | –                                    | e.g. `CF-Connecting-IP` |
| `WEB_LIMIT_CONCURRENCY` | `0` (auto)                           | Max open connections; auto = open-files limit minus a reserve |
| `WEB_BACKLOG`           | `4096`                               | Kernel queue for connections not yet accepted |
| `SITE_NAME`             | `Mitch Naake`                        | Sidebar title and `og:site_name` |
| `DEBUG`                 | `false`                              | Auto-reload on code changes |

### Capacity

The server is a single async process (uvicorn + uvloop). Rendered posts and the navigation tree are
cached in memory, and CPU-heavy work (markdown rendering, password hashing, image inspection) runs in
worker threads so the event loop keeps accepting connections. At startup the server raises its soft
open-files limit to the hard limit and never accepts more connections than it has file descriptors for,
so a flood queues rather than failing with *Too many open files*. The compose file sets `nofile` to 65536.
With a 1024 soft limit on the host, 8,000 page requests at 4,000 concurrent completed without errors.

## Architecture

```
src/mdserver/
    domain.py              User, Post, Media dataclasses - no database, no HTTP
    storage/
        base.py            async Database + UserRepository / PostRepository / MediaRepository /
                           SettingsRepository ABCs, FileStore ABC
        __init__.py        backend registry: open_database(url), open_file_store(url, db)
        mongo.py           MongoDB backend (pymongo async driver, transactions on replica sets)
        sqlite/            SQLite backend (sync engine on a dedicated thread, versioned migrations)
        files.py           GridFSStore (gridfs://) and LocalFileStore (file://)
    services/              importer, navigation tree, media uploads, auth, admin bootstrap
    render/                markdown pipeline (+ nh3 sanitiser for untrusted authors)
    web/                   FastAPI app factory, public + writer routers, middleware, templates, CSS/JS
    cli.py                 serve / import / create-user / set-password / ensure-admin / users
```

The web layer only calls the repository interfaces, so nothing outside `storage/mongo.py` knows it's MongoDB.

### MongoDB collections

* `users` — username (+ lowercase copy, unique index), display name, role, avatar, bio, password hash
* `posts` — path (unique), title, description, markdown content, author, status, nav order,
  created/updated/published timestamps, `media_ids` (which uploads the post embeds, kept in sync on save)
* `media` — owner, original name, storage key, MIME type, size, SHA-256, width/height, duration, alt text, upload time
* `media.files` / `media.chunks` — the uploaded bytes (GridFS)
* `settings` — key/value (e.g. the generated secret key)
* `counters` — integer id sequences, so URLs like `/write/edit/12` and `/media/3-…` stay short and stable

Indexes are created on startup. Multi-document writes (e.g. saving a post and its media links) run in
a transaction when MongoDB is a replica set, as in `docker-compose.yml`.

### Adding another database

```python
# src/mdserver/storage/postgres.py
from mdserver.storage import register_backend
from mdserver.storage.base import Database

class PostgresDatabase(Database):
    schemes = ('postgresql', 'postgres')

    @classmethod
    def from_url(cls, url): ...
    async def migrate(self): ...
    async def ping(self): ...
    def transaction(self): ...          # async context manager
    async def close(self): ...
    # plus self.users / self.posts / self.media / self.settings implementing the repository ABCs

register_backend(PostgresDatabase)
```

Register it in `storage/__init__.py`, then set `DATABASE_URL=postgresql://user:pass@host/blogmd`.
To prove it behaves the same, add it to `BACKENDS` in `tests/conftest.py`: the contract suite in
`tests/test_storage_contract.py` runs against every backend listed there. File storage works the same way
via `FileStore` / `register_file_store` (e.g. an S3 store for `s3://bucket/prefix`).

## Content paths

Post paths map to URLs exactly as the original file layout did, and imported files keep their URLs:

| File                                       | Post path            | URL                                 |
| ------------------------------------------ | -------------------- | ----------------------------------- |
| `content/blog/index.md`                    | *(root)*             | `/blog/`                            |
| `content/blog/myblog/index.md`             | `myblog`             | `/blog/myblog/`                     |
| `content/blog/myblog.md`                   | `myblog`             | `/blog/myblog/` (index.md wins)     |
| `content/blog/myblog/hello-world.md`       | `myblog/hello-world` | `/blog/myblog/hello-world/`         |

A folder with no page of its own (e.g. `/blog/theta/`) gets an automatic listing of what's inside it.
Files in `content/static/` are served at `/static/...`; uploads at `/media/<id>-<hash>/<name>`.

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

The MongoDB tests run against `TEST_MONGO_URL` (default `mongodb://127.0.0.1:27017/?directConnection=true`,
must be a replica set) and are skipped if no server is reachable; SQLite tests always run.
