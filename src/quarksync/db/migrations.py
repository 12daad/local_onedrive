from __future__ import annotations

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            relative_path TEXT NOT NULL,
            path_key TEXT NOT NULL UNIQUE,
            remote_path TEXT NOT NULL,
            remote_etag TEXT,
            remote_size INTEGER,
            remote_mtime TEXT,
            local_size INTEGER,
            local_mtime_ns INTEGER,
            sync_state TEXT NOT NULL,
            hydration_state TEXT NOT NULL DEFAULT 'REMOTE_ONLY',
            dirty INTEGER NOT NULL DEFAULT 0 CHECK (dirty IN (0, 1)),
            tombstone INTEGER NOT NULL DEFAULT 0 CHECK (tombstone IN (0, 1)),
            base_etag TEXT,
            last_sync_at TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            path TEXT NOT NULL,
            destination_path TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_error TEXT,
            UNIQUE(type, path, status)
        );
        CREATE INDEX IF NOT EXISTS idx_operations_due
            ON operations(status, next_retry_at);
        CREATE TABLE IF NOT EXISTS remote_snapshot (
            path_key TEXT PRIMARY KEY,
            relative_path TEXT NOT NULL,
            parent_path_key TEXT NOT NULL,
            etag TEXT,
            size INTEGER,
            mtime TEXT,
            is_dir INTEGER NOT NULL,
            scan_generation INTEGER NOT NULL,
            seen_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_snapshot_parent
            ON remote_snapshot(parent_path_key);
        CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY,
            device_name TEXT NOT NULL,
            hostname TEXT NOT NULL,
            client_version TEXT NOT NULL,
            last_heartbeat TEXT NOT NULL,
            last_successful_sync TEXT,
            pending_uploads INTEGER NOT NULL DEFAULT 0,
            pending_downloads INTEGER NOT NULL DEFAULT 0,
            pending_deletes INTEGER NOT NULL DEFAULT 0,
            conflicts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            raw_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            local_path TEXT,
            remote_etag TEXT,
            base_etag TEXT,
            conflict_copy_path TEXT,
            status TEXT NOT NULL DEFAULT 'OPEN',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            details TEXT
        );
        CREATE TABLE IF NOT EXISTS scan_cursors (
            path_key TEXT PRIMARY KEY,
            relative_path TEXT NOT NULL,
            last_scanned_at TEXT,
            generation INTEGER NOT NULL DEFAULT 0,
            pending INTEGER NOT NULL DEFAULT 1
        );
        """,
    ),
    (
        2,
        """
        CREATE TABLE IF NOT EXISTS cache_entries (
            cache_key TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            purpose TEXT NOT NULL,
            size INTEGER NOT NULL,
            protected INTEGER NOT NULL DEFAULT 0 CHECK (protected IN (0, 1)),
            created_at TEXT NOT NULL,
            last_access_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cache_eviction
            ON cache_entries(protected, last_access_at);
        """,
    ),
    (
        3,
        """
        ALTER TABLE cache_entries ADD COLUMN owner_key TEXT;
        CREATE INDEX IF NOT EXISTS idx_cache_owner ON cache_entries(owner_key);
        """,
    ),
    (
        4,
        """
        CREATE TABLE operations_v4 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            path TEXT NOT NULL,
            destination_path TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_error TEXT
        );
        INSERT INTO operations_v4 SELECT * FROM operations;
        DROP TABLE operations;
        ALTER TABLE operations_v4 RENAME TO operations;
        CREATE INDEX idx_operations_due ON operations(status, next_retry_at);
        CREATE UNIQUE INDEX idx_operations_active_unique
            ON operations(type, path) WHERE status IN ('PENDING', 'RUNNING');
        """,
    ),
    (
        5,
        """
        ALTER TABLE files ADD COLUMN is_directory INTEGER NOT NULL DEFAULT 0
            CHECK (is_directory IN (0, 1));
        """,
    ),
    (
        6,
        """
        DROP INDEX idx_operations_active_unique;
        CREATE UNIQUE INDEX idx_operations_pending_unique
            ON operations(type, path) WHERE status = 'PENDING';
        """,
    ),
)
