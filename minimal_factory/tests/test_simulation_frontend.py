"""图片只读路由与真实沙盒数据驱动的前端功能测试。"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from erp.app import create_app
from simulation.assets import IMAGE_NAMES, IMAGE_ROOT
from simulation.engine import simulate
from tests.test_field_simulation import fixture


class SimulationFrontendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "test-source.db"
        self.client = self.enterContext(TestClient(create_app(self.source)))

    def test_whitelisted_png_routes_return_exact_assets(self):
        before = self.source.read_bytes()
        for name in IMAGE_NAMES:
            response = self.client.get("/static/rendered_v1/" + name)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "image/png")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertEqual(response.content, (IMAGE_ROOT / name).read_bytes())
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.client.get("/simulation/runs").json(), [])

    def test_unknown_paths_traversal_and_writes_are_rejected(self):
        before = self.source.read_bytes()
        for name in ("manifest.json", "erp.db", "unknown.png", "..%2F..%2Fdata%2Ferp.db", "%5C..%5Cerp.db"):
            self.assertEqual(self.client.get("/static/rendered_v1/" + name).status_code, 404, name)
        self.assertEqual(self.client.post("/static/rendered_v1/normal_front.png").status_code, 405)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.client.get("/simulation/runs").json(), [])

    def test_frontend_requests_rendering_escaping_and_no_automatic_run(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for frontend functional tests")
        fixtures = self.root / "ui-fixtures"
        fixtures.mkdir()
        for mode in ("normal", "anomaly"):
            folder = fixtures / mode
            folder.mkdir()
            snapshot = fixture(mode)
            run_id = "SIM-" + ("a" if mode == "normal" else "b") * 32
            summary = simulate(folder, run_id, snapshot, threading.Event(), lambda *_: None)
            record = dict(run_id=run_id, status="completed", created_at="2026-11-06T00:00:00+00:00",
                          total_orders=1, completed_orders=1, sim_time_min=summary["makespan_min"],
                          error=None, input=snapshot, summary=summary,
                          events=[json.loads(line) for line in (folder/"events.jsonl").read_text(encoding="utf-8").splitlines()])
            record.update({key: summary[key] for key in ("equipment_alerts", "delay_summary", "protocol_messages")})
            (fixtures / f"{mode}.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        runner = Path(__file__).with_name("test_simulation_ui.cjs")
        completed = subprocess.run([node, str(runner), str(fixtures)], capture_output=True, text=True,
                                   encoding="utf-8", timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        print(completed.stdout.strip())
        if output := os.environ.get("SIM_UI_ARTIFACT_DIR"):
            target = Path(output).resolve()
            workspace = Path(__file__).resolve().parents[2]
            self.assertTrue(target.is_relative_to(workspace / ".task_work"))
            target.mkdir(parents=True, exist_ok=True)
            for name in ("normal.json", "anomaly.json", "ui_normal.html", "ui_anomaly.html"):
                shutil.copyfile(fixtures / name, target / name)


if __name__ == "__main__":
    unittest.main()
