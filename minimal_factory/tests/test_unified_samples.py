"""Checks isolated synthetic imports, provenance, joins and temporal boundaries."""
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from unified_samples import store


class UnifiedSampleTests(unittest.TestCase):
    def setUp(self):
        parent = store.ROOT / ".task_work" / "unified-tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.db = self.folder / "samples.sqlite3"

    def populate(self):
        return store.build(self.db)

    def test_import_is_idempotent_preserves_sources_and_assets(self):
        source = store.load_sources()
        result = self.populate()
        self.assertEqual(result["counts"]["modalities"], {"signal": 1623, "image": 12, "event": 12})
        self.assertEqual(result["counts"]["assets"], 6)
        before = store.sha(self.db)
        self.assertEqual(store.build(self.db)["status"], "unchanged")
        self.assertEqual(store.sha(self.db), before)
        for path, digest in source["hashes"].items():
            self.assertEqual(store.sha(Path(path)), digest)
        self.assertEqual(store.check(self.db)["objects"], 30)

    def test_cross_modal_join_and_pagination(self):
        self.populate()
        result = store.query(self.db, work_order="DEMO-WO-2", start=180, end=180)
        self.assertEqual(result["counts"], {"signal": 1, "image": 2, "event": 1})
        self.assertEqual([r["modality"] for r in result["records"]], ["event", "signal", "image", "image"])
        image = result["records"][2]
        self.assertEqual(image["payload"]["run_id"], None)  # original manifest retained
        self.assertEqual(image["provenance"]["assigned_time_s"], 180)
        self.assertIn("manually_assigned", image["provenance"]["binding_basis"])
        self.assertTrue((self.db.parent / image["relative_path"]).is_file())
        paged = store.query(self.db, work_order="DEMO-WO-2", start=180, end=180, limit=2, offset=2)
        self.assertEqual(paged["total"], 4)
        self.assertEqual(paged["records"], result["records"][2:])

    def test_states_and_dynamic_relations_at_boundaries(self):
        self.populate()
        for t, expected in [(179, "DEMO-WO-1"), (180, "DEMO-WO-2"), (359, "DEMO-WO-2"), (360, None)]:
            with self.subTest(at=t):
                snap = store.snapshot(self.db, at=t)
                eq = next(o for o in snap["objects"] if o["object_type"] == "Equipment")
                self.assertEqual(eq["state_context"].get("work_order_id"), expected)
                dynamic = [r for r in snap["relations"] if r["relation_type"] == "serves_work_order"]
                self.assertEqual(len(dynamic), 1 if expected else 0)
                if expected:
                    self.assertTrue(dynamic[0]["target_id"].endswith(expected))
                for o in snap["objects"]:
                    self.assertTrue({"object_id", "object_type", "domain", "level", "state", "context", "timestamp"}.issubset(o))
                    if o["object_type"] in ("Order", "WorkOrder"):
                        self.assertEqual(o["state"], "unknown")
        idle = store.query(self.db, start=360, end=360, modality="signal")["records"][0]
        self.assertIsNone(idle["work_order_object_id"])

    def test_scenarios_are_isolated_and_do_not_imply_causality(self):
        self.populate()
        summary = {r["scenario_id"]: r for r in store.overview(self.db)["scenarios"]}
        self.assertEqual(summary["normal"]["above_4A_count"], 0)
        self.assertEqual(summary["missing_bolt"]["above_4A_count"], 0)
        self.assertEqual(summary["current_surge"]["above_4A_sample_times_s"], list(range(240, 252)))
        missing = store.query(self.db, scenario="missing_bolt", modality="image", start=359)["records"]
        self.assertEqual({r["payload"]["state"] for r in missing}, {"missing_bolt"})
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE observations SET object_id=? WHERE observation_id=(SELECT observation_id FROM observations WHERE scenario_id='normal' LIMIT 1)",
                            (store.object_id("missing_bolt", "Equipment", "E-ASM"),))

    def test_changed_source_or_config_refused_without_replacing_database(self):
        self.populate()
        before = store.sha(self.db)
        config = store.read_json(store.MODULE / "scenarios.json")
        config["image_observations"]["after_s"] = 358
        path = self.folder / "new-config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Inputs/schema changed"):
            store.build(self.db, config_path=path)
        self.assertEqual(store.sha(self.db), before)
        config["image_observations"]["after_s"] = 360
        path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside configured"):
            store.build(self.folder / "new.sqlite3", config_path=path)
        self.assertFalse((self.folder / "new.sqlite3").exists())

    def test_invalid_unit_and_business_context_are_rejected(self):
        original = store.read_lines
        for field, value in [("unit", "mA"), ("work_order_id", "WRONG")]:
            def changed(path):
                rows = original(path)
                if Path(path).name == "normal.jsonl":
                    rows[180][field] = value
                return rows
            with self.subTest(field=field), patch.object(store, "read_lines", side_effect=changed):
                with self.assertRaises(ValueError):
                    self.populate()
                self.assertFalse(self.db.exists())

    def test_missing_or_corrupt_images_are_rejected(self):
        self.populate()
        with closing(store.connect_read(self.db)) as con:
            path = self.db.parent / con.execute("SELECT relative_path FROM assets LIMIT 1").fetchone()[0]
        path.write_bytes(b"corrupt-image")
        with self.assertRaisesRegex(ValueError, "image hash mismatch"):
            store.check(self.db)
        path.unlink()
        with self.assertRaisesRegex(ValueError, "Missing source file"):
            store.check(self.db)

    def test_failed_import_does_not_publish_partial_database(self):
        original = store.insert_observation
        counter = 0
        def fail_late(*args, **kwargs):
            nonlocal counter
            counter += 1
            if counter > 100:
                raise ValueError("injected import failure")
            return original(*args, **kwargs)
        with patch.object(store, "insert_observation", side_effect=fail_late):
            with self.assertRaisesRegex(ValueError, "injected import failure"):
                self.populate()
        self.assertFalse(self.db.exists())
        self.assertEqual(list(self.folder.glob("*.building-*")), [])
        self.assertEqual(self.populate()["status"], "created")

    def test_reject_foreign_db_and_invalid_queries(self):
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("CREATE TABLE unrelated(value TEXT)")
        before = store.sha(self.db)
        with self.assertRaisesRegex(ValueError, "Not a unified"):
            self.populate()
        self.assertEqual(store.sha(self.db), before)
        for kwargs in [{"start": -1}, {"start": 5, "end": 4}, {"limit": 0}, {"end": float('nan')}]:
            with self.assertRaises(ValueError):
                store.query(self.db, **kwargs)


if __name__ == "__main__":
    unittest.main()
