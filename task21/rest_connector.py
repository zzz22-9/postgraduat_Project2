"""Task 2.1 REST source connector V0.1: fetch, validate and envelope *raw* telemetry.

No semantic object construction or database mutation is performed here.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler


class ConnectorError(RuntimeError):
    """A remote or input contract error, never silently replaced by empty data."""


class RESTConnector:
    def __init__(self, base_url, *, timeout=10):
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.username:
            raise ValueError("base_url必须是有效的HTTP(S)地址")
        if timeout <= 0:
            raise ValueError("timeout必须大于0")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}))

    def fetch_json(self, path, *, params=None):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("只允许站内绝对API路径")
        url = self.base_url + path
        if params:
            url += "?" + urlencode(params)
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise ConnectorError("响应体超过16MiB上限")
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise ConnectorError(f"REST数据源请求失败: {error}") from error
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise ConnectorError("REST数据源返回的不是有效JSON") from error

    def read_erp_feed(self):
        """Raw enterprise data access; separate schema normalization comes later."""
        return self.fetch_json("/erp/feed")

    def iter_telemetry(self, run_id, *, page_size=500):
        """Yield V0.1 envelopes without modifying any producer-side records."""
        if not re.fullmatch(r"SIM-[0-9a-f]{32}", run_id):
            raise ValueError("run_id格式不正确")
        if type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ValueError("page_size必须为1..1000的整数")
        offset = 0
        while True:
            path = f"/simulation/runs/{run_id}/telemetry"
            response = self.fetch_json(path, params={"offset": offset, "limit": page_size})
            if not isinstance(response, dict) or response.get("run_id") != run_id:
                raise ConnectorError("遥测数据批次编号与请求不一致")
            if response.get("available") is not True:
                raise ConnectorError("该批次没有连续时序数据；不能当作空采样流")
            items = response.get("items")
            next_offset = response.get("next_offset")
            if not isinstance(items, list):
                raise ConnectorError("遥测items必须为数组")
            for sample in items:
                if not isinstance(sample, dict) or sample.get("run_id") != run_id:
                    raise ConnectorError("采样记录缺少当前批次身份")
                for field in ("equipment_id", "device_code", "work_order_id",
                              "operation_id", "virtual_minute", "telemetry_id", "sample_sequence"):
                    if field not in sample:
                        raise ConnectorError(f"缺少关键字段: {field}")
                yield {
                    "schema_version": "connector.v0.1",
                    "source_system": "minimal_factory",
                    "source_protocol": "HTTP-REST",
                    "source_endpoint": path,
                    "received_at_utc": datetime.now(timezone.utc).isoformat(),
                    "object_hint": {"object_type": "Equipment", "object_id": sample["device_code"]},
                    "context": {
                        "run_id": sample["run_id"],
                        "work_order_id": sample["work_order_id"],
                        "operation_id": sample["operation_id"],
                        "equipment_id": sample["equipment_id"],
                        "virtual_minute": sample["virtual_minute"],
                    },
                    "raw_data": sample,
                }
            if next_offset is None:
                return
            if type(next_offset) is not int or next_offset <= offset or next_offset != offset + len(items):
                raise ConnectorError("分页游标无效，拒绝无限循环或跳过记录")
            offset = next_offset
