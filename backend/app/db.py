import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers(
  id TEXT PRIMARY KEY, name TEXT, phone TEXT, plan TEXT, amount_due INTEGER, due_date TEXT,
  failure_reason TEXT, past_attempts INTEGER, scenario TEXT, scenario_tests TEXT, persona TEXT,
  balance INTEGER, instrument_status TEXT DEFAULT 'active', expected_outcome TEXT, expected_flag TEXT,
  account_edited INTEGER DEFAULT 0, status TEXT DEFAULT 'due', debits_blocked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT REFERENCES customers(id), to_number TEXT,
  channel TEXT, plivo_uuid TEXT, status TEXT, started_at TEXT, ended_at TEXT, outcome TEXT,
  identity_result TEXT, promise_amount INTEGER, promise_date TEXT, summary TEXT, transcript_json TEXT, latency_json TEXT, disposition TEXT);
CREATE TABLE IF NOT EXISTS payment_attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT REFERENCES customers(id), call_id INTEGER,
  kind TEXT, amount INTEGER, date TEXT, result TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS attention_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT REFERENCES customers(id), call_id INTEGER,
  reason TEXT, notes TEXT, created_at TEXT, resolved INTEGER DEFAULT 0, resolved_at TEXT);
"""

CUSTOMER_MIGRATIONS = [("scenario", "TEXT"), ("scenario_tests", "TEXT"), ("balance", "INTEGER"),
                       ("instrument_status", "TEXT DEFAULT 'active'"), ("expected_flag", "TEXT"),
                       ("account_edited", "INTEGER DEFAULT 0")]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
    if "latency_json" not in cols:  # migrate an existing recovery.db
        conn.execute("ALTER TABLE calls ADD COLUMN latency_json TEXT")
    if "disposition" not in cols:
        conn.execute("ALTER TABLE calls ADD COLUMN disposition TEXT")
    cust = {r[1] for r in conn.execute("PRAGMA table_info(customers)")}
    for col, typ in CUSTOMER_MIGRATIONS:
        if col not in cust:  # an older recovery.db; seed.backfill fills the values
            conn.execute(f"ALTER TABLE customers ADD COLUMN {col} {typ}")
    conn.commit()
