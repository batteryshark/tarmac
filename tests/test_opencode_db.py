"""OpenCode 2.x keeps provider credentials in opencode.db, not auth.json."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tarmac.providers import creds


def _make_db(root: Path, rows: dict) -> Path:
    db = root / "opencode.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE credential (id text PRIMARY KEY, integration_id text, "
                "label text NOT NULL, value text NOT NULL, connector_id text, "
                "method_id text, active integer, time_created integer NOT NULL, "
                "time_updated integer NOT NULL)")
    for i, (name, entry) in enumerate(rows.items()):
        con.execute("INSERT INTO credential VALUES (?,?,?,?,NULL,NULL,1,0,?)",
                    (f"c{i}", name, name, json.dumps(entry), i))
    con.commit()
    con.close()
    return db


class OpencodeDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.auth = self.root / "auth.json"  # absent: forces the db path

    def tearDown(self):
        self.tmp.cleanup()

    def test_key_read_from_db_when_auth_json_is_absent(self):
        _make_db(self.root, {"deepseek": {"type": "key", "key": "db-key"}})
        key, source = creds.api_key("deepseek", auth_path=self.auth)
        self.assertEqual(key, "db-key")
        self.assertEqual(source, "opencode db (deepseek)")

    def test_auth_json_still_wins(self):
        _make_db(self.root, {"deepseek": {"type": "key", "key": "db-key"}})
        self.auth.write_text(json.dumps({"deepseek": {"type": "api", "key": "file-key"}}))
        self.assertEqual(creds.api_key("deepseek", auth_path=self.auth)[0], "file-key")

    def test_replace_entry_is_compare_and_swap(self):
        db = _make_db(self.root, {"xai": {"type": "oauth", "access": "a1", "refresh": "r1"}})
        new = {"type": "oauth", "access": "a2", "refresh": "r2"}
        current, err = creds.replace_entry("xai", "r1", new, self.auth)
        self.assertIsNone(err)
        self.assertIs(current, new)
        # A second renewal holding the consumed token writes nothing.
        stale, err = creds.replace_entry("xai", "r1", {"access": "x", "refresh": "y"}, self.auth)
        self.assertIsNone(err)
        self.assertEqual(stale["refresh"], "r2")
        value = sqlite3.connect(db).execute("SELECT value FROM credential").fetchone()[0]
        self.assertEqual(json.loads(value)["access"], "a2")


if __name__ == "__main__":
    unittest.main()
