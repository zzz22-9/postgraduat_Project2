"""批次存储与后台任务。源数据库只读；实验从不可变快照创建自己的 MES 数据库。"""
import csv
import io
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from mes import service as mes
from .engine import Interrupted, simulate
from .inputs import configure, demo_snapshot, source_snapshot

_json_lock = threading.RLock()


def write_json(path, value):
    with _json_lock:
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        # Windows 不允许替换仍被读取的文件；本进程读写互斥，跨进程短暂读占用重试。
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(.01)


def read_json(path):
    with _json_lock:
        return json.loads(path.read_text(encoding="utf-8"))


class RunLease:
    """OS 文件锁防止同一实验目录被两个服务进程同时执行；进程退出自动释放。"""
    def __init__(self, root):
        self.file = (root / ".execution.lock").open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise mes.BusinessError("已有仿真运行中，请等待完成后再启动")

    def close(self):
        if not self.file.closed:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()


class SimulationService:
    def __init__(self, source_path, root=None):
        self.source_path = source_path
        self.root = root or source_path.parent / (source_path.stem + ".simulations")
        self.executor = None
        self.guard = threading.Lock()
        self.stop = threading.Event()

    def startup(self):
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            lease = RunLease(self.root)
        except mes.BusinessError:
            lease = None  # 另一实例仍在运行，不能将其误标为中断。
        if lease:
            try:
                for meta in self.root.glob("SIM-*/run.json"):
                    record = read_json(meta)
                    if record["status"] == "running":
                        record.update(status="interrupted", error="上次服务中断，已保留数据；请复制参数重跑",
                                      finished_at=mes.now())
                        write_json(meta, record)
            finally:
                lease.close()
        self.stop.clear()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="factory-simulation")

    def shutdown(self):
        self.stop.set()
        if self.executor:
            self.executor.shutdown(wait=True)
            self.executor = None

    def folder(self, run_id):
        mes.require(bool(re.fullmatch(r"SIM-[0-9a-f]{32}", run_id)), "实验编号格式不正确", 404)
        folder = self.root / run_id
        mes.require((folder / "run.json").is_file(), "实验不存在", 404)
        return folder

    def preview(self, request):
        if request.source == "demo":
            snapshot = demo_snapshot()
        elif request.source == "work_orders":
            snapshot = source_snapshot(self.source_path, request.work_order_ids)
        else:
            snapshot = read_json(self.folder(request.replay_run_id) / "input.json")
        return configure(snapshot, request.overrides)

    def start(self, request):
        mes.require(self.executor is not None and not self.stop.is_set(), "仿真服务尚未就绪")
        mes.require(self.guard.acquire(blocking=False), "已有仿真运行中，请等待完成后再启动")
        lease = None
        try:
            lease = RunLease(self.root)
            snapshot = self.preview(request)
            run_id = "SIM-" + uuid4().hex
            folder = self.root / run_id
            folder.mkdir()
            mapping = [
                dict(work_order_id=w["work_order_id"], order_id=w["order_id"],
                     source_work_order_id=w["work_order_id"] if snapshot["source"] == "work_orders" else None,
                     source_order_id=w["order_id"] if snapshot["source"] == "work_orders" else None,
                     operations=[dict(operation_id=o["operation_id"], equipment_id=o["equipment_id"],
                                      source_operation_id=o["operation_id"] if snapshot["source"] == "work_orders" else None,
                                      source_equipment_id=o["equipment_id"] if snapshot["source"] == "work_orders" else None)
                                 for o in w["operations"]])
                for w in snapshot["orders"]]
            record = dict(run_id=run_id, status="running", created_at=mes.now(), finished_at=None,
                          source=request.source, replay_run_id=request.replay_run_id,
                          completed_orders=0, total_orders=len(snapshot["orders"]), sim_time_min=0, error=None)
            write_json(folder / "input.json", snapshot)
            write_json(folder / "mapping.json", mapping)
            write_json(folder / "run.json", record)
            response = dict(record)
            self.executor.submit(self._execute, folder, snapshot, record, lease)
            return response
        except BaseException:
            if lease:
                lease.close()
            self.guard.release()
            raise

    def _execute(self, folder, snapshot, record, lease):
        def progress(sim_time, completed):
            record.update(sim_time_min=sim_time, completed_orders=completed)
            write_json(folder / "run.json", record)
        try:
            result = simulate(folder, record["run_id"], snapshot, self.stop, progress)
            write_json(folder / "summary.json", result)
            record.update(status="completed", sim_time_min=result["makespan_min"],
                          completed_orders=len(result["orders"]))
        except Interrupted as error:
            record.update(status="interrupted", error=str(error))
        except Exception as error:
            logging.exception("Simulation %s failed", record["run_id"])
            record.update(status="failed", error=f"仿真未完成：{error}")
        finally:
            record["finished_at"] = mes.now()
            try:
                write_json(folder / "run.json", record)
            finally:
                lease.close()
                self.guard.release()

    def list_runs(self):
        return sorted((read_json(p) for p in self.root.glob("SIM-*/run.json")),
                      key=lambda r: (r["created_at"], r["run_id"]), reverse=True)

    def telemetry(self, run_id, offset=0, limit=500):
        """Read complete synthetic telemetry JSONL lines; bounded for Connector polling."""
        folder = self.folder(run_id)
        path = folder / "telemetry.jsonl"
        if not path.exists():
            return dict(run_id=run_id, available=False, total=0, offset=offset,
                        limit=limit, items=[], next_offset=None)
        items, total = [], 0
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.endswith("\n"):
                    break  # Incomplete line may exist during an ongoing run.
                if total >= offset and len(items) < limit:
                    items.append(json.loads(line))
                total += 1
        next_offset = offset + len(items) if offset + len(items) < total else None
        return dict(run_id=run_id, available=True, total=total, offset=offset,
                    limit=limit, items=items, next_offset=next_offset)

    def get(self, run_id):
        folder = self.folder(run_id)
        record = read_json(folder / "run.json")
        record["input"] = read_json(folder / "input.json")
        record["mapping"] = read_json(folder / "mapping.json")
        record["summary"] = read_json(folder / "summary.json") if record["status"] == "completed" else None
        record["events"] = []
        summary = record["summary"] or {}
        record["equipment_alerts"] = summary.get("equipment_alerts", [])
        record["delay_summary"] = summary.get("delay_summary", [])
        record["protocol_messages"] = summary.get("protocol_messages", [])
        protocol_path = folder / "protocol_messages.jsonl"
        if not record["summary"] and protocol_path.exists():
            record["protocol_messages"] = [json.loads(line) for line in
                protocol_path.read_text(encoding="utf-8").splitlines(keepends=True) if line.endswith("\n")]
        path = folder / "events.jsonl"
        if path.exists():
            # 运行中最后一行可能还没写完；已完成的行仍可查询。
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            for line in lines:
                if line.endswith("\n"):
                    record["events"].append(json.loads(line))
        return record

    def export(self, run_id, format):
        record = self.get(run_id)
        if format == "json":
            record["mes_work_orders"] = []
            if record["status"] == "completed":
                record["mes_work_orders"] = [
                    mes.get_work_order(self.folder(run_id) / "mes.db", w["work_order_id"])
                    for w in record["input"]["orders"]]
            return json.dumps(record, ensure_ascii=False, indent=2), "application/json"
        if format == "events_csv":
            fields = ["run_id", "sequence", "sim_time_min", "event_type", "order_id", "work_order_id",
                      "operation_id", "equipment_id", "quantity", "phase", "rework_id", "wait_min",
                      "accepted_quantity", "rejected_quantity", "source_operation_id", "recorded_at"]
            rows = record["events"]
        else:
            mes.require(record["status"] == "completed", "实验未完成，尚无完整汇总")
            fields = ["work_order_id", "order_id", "product_name", "quantity", "completed_min",
                      "waiting_min", "rework_quantity", "mes_status", "erp_status"]
            rows = record["summary"]["orders"]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            # 防止导出的用户产品名称/编号被电子表格当作公式执行。
            writer.writerow({k: ("'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v)
                             for k, v in row.items()})
        return "\ufeff" + output.getvalue(), "text/csv"
