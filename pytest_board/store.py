"""The SQLite database (board.db): schema, upgrades from older versions, and the one connection everything uses."""
import secrets
import sqlite3
import sys
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS tests(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', args TEXT NOT NULL DEFAULT '',
  env TEXT NOT NULL DEFAULT '', timeout INTEGER NOT NULL DEFAULT 0, position REAL NOT NULL DEFAULT 0,
  created REAL, updated REAL, grp TEXT, machine TEXT, optsets TEXT, branch TEXT, extra_args TEXT);
CREATE TABLE IF NOT EXISTS seeded(grp TEXT NOT NULL, spec TEXT NOT NULL, PRIMARY KEY(grp, spec));
CREATE TABLE IF NOT EXISTS machines(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, host TEXT NOT NULL, user TEXT NOT NULL DEFAULT '',
  port INTEGER, identity TEXT NOT NULL DEFAULT '', workdir TEXT NOT NULL DEFAULT '',
  python TEXT NOT NULL DEFAULT 'python3 -m pytest', sync INTEGER NOT NULL DEFAULT 0,
  env TEXT NOT NULL DEFAULT '', ssh_options TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
  position REAL NOT NULL DEFAULT 0, created REAL, updated REAL, env_file TEXT NOT NULL DEFAULT '',
  remote_env_file TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS lanes(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0,
  collapsed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS cards(
  id INTEGER PRIMARY KEY,
  lane_id INTEGER NOT NULL REFERENCES lanes(id) ON DELETE CASCADE,
  title TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', args TEXT NOT NULL DEFAULT '',
  env TEXT NOT NULL DEFAULT '', timeout INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'backlog', position REAL NOT NULL DEFAULT 0,
  run_at REAL, cron TEXT, queued_at REAL, pending_trigger TEXT, last_run_id INTEGER,
  created REAL, updated REAL,
  test_id INTEGER REFERENCES tests(id) ON DELETE SET NULL, grp TEXT, machine TEXT, optset TEXT, branch TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY,
  card_id INTEGER REFERENCES cards(id) ON DELETE SET NULL,
  test_id INTEGER REFERENCES tests(id) ON DELETE SET NULL,
  card_title TEXT, lane_name TEXT,
  trigger TEXT, command TEXT, started REAL, finished REAL, exit_code INTEGER,
  outcome TEXT, total INTEGER, passed INTEGER, failed INTEGER, errors INTEGER, skipped INTEGER, machine TEXT,
  optset TEXT, remote TEXT, branch TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""
RUN_COLS_V1 = ("id, card_id, trigger, command, started, finished, exit_code, outcome, "
               "total, passed, failed, errors, skipped")
INDEXES = """
CREATE INDEX IF NOT EXISTS runs_card ON runs(card_id, id);
CREATE INDEX IF NOT EXISTS runs_test ON runs(test_id, id);
CREATE INDEX IF NOT EXISTS cards_test ON cards(test_id);
CREATE INDEX IF NOT EXISTS tests_grp ON tests(grp, position);
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.executescript(INDEXES)
        self.lock = threading.RLock()
        self.version = int(time.time())  # bumps on every write; the UI polls it
        # names this board's run folders and screen sessions on the machines, so boards sharing a machine
        # (or a fresh board.db) never mix up each other's runs
        row = self.db.execute("SELECT v FROM meta WHERE k='board_id'").fetchone()
        if row:
            self.board_id = row[0]
        else:
            self.board_id = secrets.token_hex(3)
            self.db.execute("INSERT INTO meta(k, v) VALUES('board_id', ?)", (self.board_id,))

    def _cols(self, table):
        return {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}

    def _migrate(self):
        """Upgrade a board.db created by an earlier version."""
        for table in ("tests", "cards"):  # v3: headlines
            if "grp" not in self._cols(table):
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN grp TEXT")
        if "test_id" not in self._cols("cards"):
            self.db.execute("ALTER TABLE cards ADD COLUMN test_id INTEGER REFERENCES tests(id) ON DELETE SET NULL")
        if "test_id" not in self._cols("runs"):
            # runs used to be deleted with their card; rebuild so history outlives cards
            self.db.execute("PRAGMA foreign_keys=OFF")
            self.db.executescript(f"""
                BEGIN;
                ALTER TABLE runs RENAME TO runs_v1;
                CREATE TABLE runs(
                  id INTEGER PRIMARY KEY,
                  card_id INTEGER REFERENCES cards(id) ON DELETE SET NULL,
                  test_id INTEGER REFERENCES tests(id) ON DELETE SET NULL,
                  card_title TEXT, lane_name TEXT,
                  trigger TEXT, command TEXT, started REAL, finished REAL, exit_code INTEGER,
                  outcome TEXT, total INTEGER, passed INTEGER, failed INTEGER, errors INTEGER, skipped INTEGER);
                INSERT INTO runs({RUN_COLS_V1}) SELECT {RUN_COLS_V1} FROM runs_v1;
                UPDATE runs SET card_title=(SELECT title FROM cards WHERE cards.id=runs.card_id),
                                lane_name=(SELECT l.name FROM cards c JOIN lanes l ON l.id=c.lane_id
                                           WHERE c.id=runs.card_id);
                DROP TABLE runs_v1;
                COMMIT;""")
            self.db.execute("PRAGMA foreign_keys=ON")
            print("migrated board.db: added test library", file=sys.stderr)
        for table in ("tests", "cards", "runs"):  # v4: machines (after the runs rebuild above)
            if "machine" not in self._cols(table):
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN machine TEXT")
        for table, col in (("cards", "optset"), ("runs", "optset"), ("tests", "optsets"),  # v6: option sets
                           ("runs", "remote")):  # v7: runs that live on a machine (screen / background)
            if col not in self._cols(table):
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        for table in ("tests", "cards", "runs"):  # v9: git branches
            if "branch" not in self._cols(table):
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN branch TEXT")
        if "extra_args" not in self._cols("tests"):  # v10: extra arguments last used for a test's copies
            self.db.execute("ALTER TABLE tests ADD COLUMN extra_args TEXT")
        # v11: a git branch is set on cards (or a headline), not on library tests any more
        self.db.execute("UPDATE tests SET branch=NULL WHERE branch IS NOT NULL")
        # v12: the board server doesn't run tests itself any more: 'local' (this server) becomes "no machine"
        for table in ("tests", "cards"):
            self.db.execute(f"UPDATE {table} SET machine=NULL WHERE machine='local'")
        # v13: library tests have no machine of their own: a copy runs on the row it is dropped into, else on its
        # headline's machine (cards keep theirs)
        self.db.execute("UPDATE tests SET machine=NULL WHERE machine IS NOT NULL")
        # v14: a machine runs one test at a time, so machines lose their "how many at once" column
        if "slots" in self._cols("machines"):
            try:
                self.db.execute("ALTER TABLE machines DROP COLUMN slots")
            except sqlite3.OperationalError:
                pass  # SQLite before 3.35 can't drop a column; it just stays unused
        for col in ("env_file", "remote_env_file"):  # v8: .env files
            if col not in self._cols("machines"):
                self.db.execute(f"ALTER TABLE machines ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        # v5 had temperatures: a card at -20 becomes a card with the option set --temperature=-20
        for table in ("cards", "runs"):
            if "temp" in self._cols(table):
                n = self.db.execute(f"UPDATE {table} SET optset='--temperature=' || temp "
                                    "WHERE temp IS NOT NULL AND optset IS NULL").rowcount
                if table == "cards":
                    self.db.execute("UPDATE cards SET temp=NULL WHERE temp IS NOT NULL")
                    if n:
                        print(f"migrated board.db: {n} temperature card(s) now use --temperature=... option sets",
                              file=sys.stderr)

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.version += 1
            return cur.lastrowid
