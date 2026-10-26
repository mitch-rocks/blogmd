"""
Ordered schema migrations. The DB's `PRAGMA user_version` records how many have run.
Append new migrations to the end; never edit one that has shipped.
"""

MIGRATIONS: list[str] = [
    # 1 - initial schema
    """
    CREATE TABLE users (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        username       TEXT    NOT NULL UNIQUE COLLATE NOCASE,
        display_name   TEXT    NOT NULL,
        role           TEXT    NOT NULL DEFAULT 'writer' CHECK (role IN ('admin','writer','system')),
        avatar_url     TEXT    NOT NULL DEFAULT '',
        bio            TEXT    NOT NULL DEFAULT '',
        password_hash  TEXT,
        created_at     TEXT    NOT NULL
    );

    CREATE TABLE posts (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        path           TEXT    NOT NULL UNIQUE,
        title          TEXT    NOT NULL,
        description    TEXT    NOT NULL DEFAULT '',
        content        TEXT    NOT NULL DEFAULT '',
        author_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
        status         TEXT    NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','unlisted')),
        nav_order      INTEGER NOT NULL DEFAULT 0,
        created_at     TEXT    NOT NULL,
        updated_at     TEXT    NOT NULL,
        published_at   TEXT
    );
    CREATE INDEX idx_posts_author ON posts(author_id);
    CREATE INDEX idx_posts_status ON posts(status);

    CREATE TABLE media (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        original_name  TEXT    NOT NULL,
        storage_key    TEXT    NOT NULL UNIQUE,
        mime_type      TEXT    NOT NULL,
        size_bytes     INTEGER NOT NULL,
        sha256         TEXT    NOT NULL,
        width          INTEGER,
        height         INTEGER,
        duration_s     REAL,
        alt_text       TEXT    NOT NULL DEFAULT '',
        created_at     TEXT    NOT NULL
    );
    CREATE INDEX idx_media_owner ON media(owner_id, created_at DESC);
    CREATE INDEX idx_media_hash  ON media(owner_id, sha256);

    CREATE TABLE post_media (
        post_id   INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
        media_id  INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
        PRIMARY KEY (post_id, media_id)
    );
    CREATE INDEX idx_post_media_media ON post_media(media_id);

    CREATE TABLE settings (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    );
    """,
]
