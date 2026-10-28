"""启动工具不得重复启动、误终止端口占用者或把失败报告为成功。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import demo_launcher as launcher


class LauncherTests(unittest.TestCase):
    def test_healthy_server_is_reused(self):
        with patch.object(launcher, "healthy", return_value=True), patch.object(launcher.subprocess, "Popen") as start:
            launcher.ensure_server()
            start.assert_not_called()

    def test_unhealthy_occupied_port_is_not_replaced(self):
        with patch.object(launcher, "healthy", return_value=False), patch.object(launcher, "occupied", return_value=True), patch.object(launcher.subprocess, "Popen") as start:
            with self.assertRaisesRegex(RuntimeError, "端口被占用"):
                launcher.ensure_server()
            start.assert_not_called()

    def test_startup_failure_is_reported_with_log(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(launcher, "LOGS", Path(temp)), patch.object(launcher, "healthy", return_value=False), patch.object(launcher, "occupied", return_value=False), patch.object(launcher.subprocess, "run") as dependencies, patch.object(launcher.subprocess, "Popen") as start:
            dependencies.return_value.returncode = 0
            start.return_value.pid = 123
            start.return_value.poll.return_value = 1
            with self.assertRaisesRegex(RuntimeError, "后台启动失败"):
                launcher.ensure_server()
            self.assertEqual(start.call_count, 1)
            self.assertTrue(list(Path(temp).glob("server-*.log")))

    def test_preflight_detects_broken_frontend_and_keeps_diagnostics(self):
        def fetch(path):
            return {"/health": '{"status":"ok","module":"erp+mes"}', "/": 'data-page="simulation"',
                    "/web/simulation.js": "broken script", "/orders": "[]", "/mes/work-orders": "[]",
                    "/simulation/runs": "[]"}[path]
        with tempfile.TemporaryDirectory() as temp, patch.object(launcher, "LOGS", Path(temp)), patch.object(launcher, "fetch", side_effect=fetch):
            self.assertFalse(launcher.preflight())
            report = json.loads((Path(temp) / "last-check.json").read_text(encoding="utf-8"))
            self.assertEqual([c["name"] for c in report["checks"] if not c["ok"]], ["仿真页面脚本"])


if __name__ == "__main__":
    unittest.main()
