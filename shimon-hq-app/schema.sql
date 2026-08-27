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

CREATE TABLE IF NOT EXISTS wa_inbox (
  uid TEXT PRIMARY KEY,           -- TimelinesAI message uid, so a retry never doubles
  chat_id TEXT,
  chat_name TEXT,
  sender TEXT,
  from_me INTEGER NOT NULL DEFAULT 0,
  text TEXT,
  ts TEXT,                        -- when WhatsApp says it was sent
  received_at TEXT,               -- when the webhook reached us
  handled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS wa_inbox_open ON wa_inbox(handled, ts);

-- ---------- productivity history ----------
-- The board only ever knew the present: a task was open, or it was not. To say
-- anything about a week you need to know what changed and when, so every change
-- to an item is written here by a trigger. A trigger rather than application
-- code on purpose - there are a dozen routes that can close a task (swipe, the
-- hold menu, the edit form, the API, the briefing) and one of them would
-- eventually be missed.
--
-- No foreign key to items: when a task is deleted its history has to survive,
-- otherwise deleting finished work quietly rewrites the record of the week.
CREATE TABLE IF NOT EXISTS item_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL,
  at TEXT NOT NULL,                 -- UTC, always ...Z
  kind TEXT NOT NULL,               -- created | status | waiting | due | deleted | snapshot
  old TEXT DEFAULT '',
  new TEXT DEFAULT '',
  title TEXT DEFAULT ''             -- copied in, so a deleted task still reads sensibly
);
CREATE INDEX IF NOT EXISTS item_events_at ON item_events(at);
CREATE INDEX IF NOT EXISTS item_events_item ON item_events(item_id, at);

CREATE TRIGGER IF NOT EXISTS ie_created AFTER INSERT ON items
BEGIN
  INSERT INTO item_events(item_id, at, kind, old, new, title)
  VALUES(new.id, strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'created', '', new.status, new.title);
END;

CREATE TRIGGER IF NOT EXISTS ie_status AFTER UPDATE OF status ON items
WHEN ifnull(old.status,'') <> ifnull(new.status,'')
BEGIN
  INSERT INTO item_events(item_id, at, kind, old, new, title)
  VALUES(new.id, strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'status',
         ifnull(old.status,''), ifnull(new.status,''), new.title);
END;

CREATE TRIGGER IF NOT EXISTS ie_waiting AFTER UPDATE OF waiting_on ON items
WHEN ifnull(old.waiting_on,'') <> ifnull(new.waiting_on,'')
BEGIN
  INSERT INTO item_events(item_id, at, kind, old, new, title)
  VALUES(new.id, strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'waiting',
         ifnull(old.waiting_on,''), ifnull(new.waiting_on,''), new.title);
END;

CREATE TRIGGER IF NOT EXISTS ie_due AFTER UPDATE OF due_date ON items
WHEN ifnull(old.due_date,'') <> ifnull(new.due_date,'')
BEGIN
  INSERT INTO item_events(item_id, at, kind, old, new, title)
  VALUES(new.id, strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'due',
         ifnull(old.due_date,''), ifnull(new.due_date,''), new.title);
END;

CREATE TRIGGER IF NOT EXISTS ie_deleted AFTER DELETE ON items
BEGIN
  INSERT INTO item_events(item_id, at, kind, old, new, title)
  VALUES(old.id, strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'deleted',
         ifnull(old.status,''), '', old.title);
END;

-- The written half of the week - the part numbers cannot say.
CREATE TABLE IF NOT EXISTS pulse_notes (
  week TEXT PRIMARY KEY,            -- the Monday, YYYY-MM-DD
  body TEXT NOT NULL,
  created_at TEXT
);

-- ---------- people ----------
-- The board was built for one person and every row belonged to him by default.
-- Now that two people share it, "who may see this" has to be written down, and
-- the default has to be no. A section is the unit of sharing: private means the
-- owner alone, shared means everyone on the board. Items inherit from their
-- section, which is why there is no per-item permission to get wrong.
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL,
  pw_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT
);

