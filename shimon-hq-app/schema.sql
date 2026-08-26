CREATE TABLE IF NOT EXISTS sections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  pos INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  pos INTEGER NOT NULL DEFAULT 0,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  note TEXT DEFAULT '',
  waiting_on TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',  -- open | waiting | done
  pos INTEGER NOT NULL DEFAULT 0,
  due_date TEXT,
  thread_key TEXT,          -- the email conversation this task belongs to
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS item_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  body TEXT NOT NULL,
  source TEXT DEFAULT '',   -- '' = you typed it | 'email' = filed automatically
  ext_id TEXT,              -- the message it came from, so a re-run never doubles it
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS item_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,
  stored_name TEXT NOT NULL,
  size INTEGER NOT NULL DEFAULT 0,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ext_key TEXT UNIQUE,
  subject TEXT NOT NULL,
  day TEXT NOT NULL,          -- YYYY-MM-DD
  start_time TEXT,            -- HH:MM (24h, local)
  location TEXT DEFAULT '',
  source TEXT DEFAULT 'outlook',   -- outlook = refreshed daily | manual = yours, never overwritten
  note TEXT DEFAULT '',
  synced_at TEXT
);

CREATE TABLE IF NOT EXISTS hidden_events (
  ext_key TEXT PRIMARY KEY        -- Outlook events you deleted here; the sync stops bringing them back
);

CREATE TABLE IF NOT EXISTS push_subs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  endpoint TEXT UNIQUE NOT NULL,
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS reminders_sent (
  ref TEXT PRIMARY KEY,     -- e.g. item:12:2026-08-26T14:00  |  ev:key  |  digest:2026-08-26
  sent_at TEXT
);

CREATE TABLE IF NOT EXISTS brief_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT NOT NULL,              -- YYYY-MM-DD
  kind TEXT NOT NULL DEFAULT 'note',  -- meeting | email | due | added | note
  text TEXT NOT NULL,
  detail TEXT DEFAULT '',
  link TEXT DEFAULT '',
  is_read INTEGER NOT NULL DEFAULT 0,
  item_id INTEGER,                -- set once pushed to a task
  target_item_id INTEGER,         -- the task this update is offering to file onto
  created_at TEXT
);
