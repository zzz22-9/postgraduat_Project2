"""Real loopback HTTP tests; no factory production database writes."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from task21.rest_connector import ConnectorError, RESTConnector

RUN_ID = "SIM-" + "a"*32


def sample(number):
    return {
        "run_id": RUN_ID, "telemetry_id": f"TEL-{number}", "sample_sequence": number,
        "equipment_id": "E-ASM", "device_code": "CNC_01",
        "work_order_id": "WO-test", "operation_id": "OP-test",
        "virtual_minute": number/60, "synthetic": True, "metrics": {"temperature_c": 30+number},
    }


class SourceHandler(BaseHTTPRequestHandler):
    mode = "normal"

    def log_message(self, *_):
        return

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/erp/feed":
            self.send_json({"orders": [{"order_id": "ORD-test"}]})
            return
        if url.path != f"/simulation/runs/{RUN_ID}/telemetry":
            self.send_json({"detail": "not found"}, 404)
            return
        if self.mode == "unavailable":
            self.send_json({"run_id": RUN_ID, "available": False, "items": [], "next_offset": None})
            return
        params = parse_qs(url.query)
        offset = int(params.get("offset", ["0"])[0])
        limit = int(params.get("limit", ["500"])[0])
        data = [sample(i) for i in range(5)]
        subset = data[offset:offset+limit]
        next_offset = offset + len(subset) if offset + len(subset) < len(data) else None
        self.send_json({"run_id": RUN_ID, "available": True, "total": len(data),
                        "offset": offset, "limit": limit, "items": subset,
                        "next_offset": next_offset})

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RestConnectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_fetch_business_api_and_paged_telemetry(self):
        client = RESTConnector(self.url)
        self.assertEqual(client.read_erp_feed()["orders"][0]["order_id"], "ORD-test")
        rows = list(client.iter_telemetry(RUN_ID, page_size=2))
        self.assertEqual(len(rows), 5)
        self.assertEqual([x["raw_data"]["sample_sequence"] for x in rows], list(range(5)))
        self.assertEqual(rows[0]["source_protocol"], "HTTP-REST")
        self.assertEqual(rows[0]["object_hint"]["object_id"], "CNC_01")
        self.assertEqual(rows[0]["context"]["virtual_minute"], 0)
        self.assertTrue(rows[0]["received_at_utc"].endswith("+00:00"))

    def test_invalid_and_missing_source_errors(self):
        client = RESTConnector(self.url)
        with self.assertRaises(ValueError):
            list(client.iter_telemetry("bad-run-id"))
        with self.assertRaises(ValueError):
            list(client.iter_telemetry(RUN_ID, page_size=0))
        with self.assertRaises(ConnectorError):
            client.fetch_json("/missing")
        self.assertRaises(ValueError, RESTConnector, "file:///etc/passwd")


if __name__ == "__main__":
    unittest.main()
