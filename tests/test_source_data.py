import csv
from pathlib import Path
import unittest

DATA = Path(__file__).resolve().parents[1] / "data"

TABLES = {
    'slots': ('slot day starts capacity priority_class', 'slot'),
    'orders': ('order_id family slot seats may_split confirmed_at', 'order_id'),
    'waitlist': ('request family old_slot alternatives seats joined_at may_split wait_class', 'request'),
    'events': ('event offer request slot kind at expires seen seats requires_offer', 'event'),
    'randomization': ('draw request slot at probability eligible selected', 'draw request'),
    'gate': ('entry family slot seats at status', 'entry'),
    'policies': ('policy state effective_at ordering expiry_minutes max_regular_wait_minutes regular_floor', 'policy'),
}


class SourceDataTests(unittest.TestCase):
    def load(self, name):
        fields, _ = TABLES[name]
        with (DATA / (name + ".csv")).open(encoding="utf-8", newline="") as handle:
            contents = csv.DictReader(handle)
            self.assertEqual(contents.fieldnames, fields.split())
            records = list(contents)
        self.assertTrue(records)
        self.assertTrue(all(None not in row and None not in row.values() for row in records))
        return records

    def test_format_and_record_keys(self):
        for name, (_, key) in TABLES.items():
            with self.subTest(table=name):
                records = self.load(name)
                values = [tuple(row[field] for field in key.split()) for row in records]
                self.assertTrue(all(all(value) for value in values))
                self.assertEqual(len(values), len(set(values)))

    def test_related_source_records(self):
        rows = {name: self.load(name) for name in TABLES}
        self.assertTrue(any(r["old_slot"] and "|" in r["alternatives"] for r in rows["waitlist"]))
        self.assertTrue(any(r["kind"] == "origin_released" for r in rows["events"]))
        self.assertTrue(any(r["kind"] == "pending" for r in rows["events"]))
        self.assertTrue(any(r["eligible"] == "yes" and r["selected"] == "no" for r in rows["randomization"]))
        self.assertTrue(any(r["expiry_minutes"] != rows["policies"][0]["expiry_minutes"]
                            for r in rows["policies"]))
