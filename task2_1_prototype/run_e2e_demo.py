"""Local A-F demonstration. Default exits; --serve explicitly starts Uvicorn."""
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json

from fastapi.testclient import TestClient

from task2_1_prototype.simulators.simpy_factory import run_demo
from task2_1_prototype.services.connector_service.pipeline import run_batch as connector_batch, invalid_examples
from task2_1_prototype.services.connector_service.validation import PROJECT
from task2_1_prototype.services.semantic_object_service.pipeline import run_batch as semantic_batch
from task2_1_prototype.services.semantic_object_service.models import validate_object
from task2_1_prototype.services.spatiotemporal_model_service.pipeline import run_batch as graph_batch, read_lines
from task2_1_prototype.services.relation_service.relation_engine import validated_edge
from task2_1_prototype.services.api_gateway import create_gateway_app
from task2_1_prototype.services.api_gateway.dashboard import render_dashboard


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def check_objects(values):
    for value in values if isinstance(values, list) else [values]:
        assert len(value) == 7
        validate_object(value)


def check_edges(values):
    for value in values:
        assert len(value) == 7
        validated_edge(value)


def exercise_gateway(output):
    wp1, wp2, wp3 = (output / name for name in ("wp1", "wp2", "wp3"))
    raw = read_lines(wp2 / "raw_ingest_messages.jsonl")
    checks = []

    def request(client, method, path, *, expected=200, **kwargs):
        response = client.request(method, path, **kwargs)
        assert response.status_code == expected, (method, path, response.status_code, response.text[:1000])
        checks.append({"method": method, "path": path, "params": kwargs.get("params"), "status_code": response.status_code,
                       "response_sha256": hashlib.sha256(response.content).hexdigest()})
        return response.json()

    options = dict(source_dir=wp1, catalog_file=wp3 / "semantic_objects.jsonl")
    branch = create_gateway_app(output / "event_session", **options,
                                bootstrap_raw_file=wp2 / "raw_ingest_messages.jsonl", bootstrap_until=59)
    with TestClient(branch) as client:
        events = []
        for kind, seconds in (("fault", 70), ("fault_cleared", 80)):
            result = request(client, "POST", "/api/v1/events/trigger", json=dict(event_id="e2e-" + kind, kind=kind,
                             object_id="CNC_01", sim_time_s=seconds))
            check_objects(result["objects"])
            check_edges(result["relations"])
            assert result["pipeline"] == ["WP2", "WP3", "WP4"]
            assert len(result["impact"]["active_impacts"]) == (1 if kind == "fault" else 0)
            events.append(result)
        assert [(r["start_s"], r["end_s"]) for r in events[-1]["impact"]["impact_events"]] == [(70, 80)]
        branch_counts = branch.state.runtime.store.counts()
    write_json(output / "online_event_evidence.json", {"session": "isolated_cut_at_59s", "results": events, "counts": branch_counts})
    app = create_gateway_app(output / "gateway", **options, dashboard_file=output / "e2e_dashboard.html",
                             demo_workspace=output / "event_session")
    with TestClient(app) as client:
        source = read_lines(wp1 / "transport_records.jsonl")[0]
        check_objects(request(client, "POST", "/api/v1/objects/ingest", json=source))
        check_objects(request(client, "POST", "/api/v1/objects/ingest", json=raw))
        counts = app.state.runtime.store.counts()
        check_objects(request(client, "POST", "/api/v1/objects/ingest", json=raw[0]))
        assert counts == app.state.runtime.store.counts()
        assert counts == {"semantic_objects": 12, "semantic_state_history": 2215, "processed_messages": 2206, "signal_cycles": 360}
        objects = request(client, "GET", "/api/v1/objects")
        check_objects(objects)
        assert len(objects) == 12
        check_objects(request(client, "GET", "/api/v1/objects", params={"object_type": "equipment", "domain": "manufacturing", "level": "device"}))
        check_objects(request(client, "GET", "/api/v1/states/CNC_01"))
        check_objects(request(client, "GET", "/api/v1/states/CNC_01/history", params={"scope": app.state.runtime.manager.scope}))
        request(client, "GET", "/api/v1/states/UNKNOWN", expected=404)
        check_edges(request(client, "GET", "/api/v1/relations", params={"source": "CNC_01", "relation": "executes", "active": True, "as_of": 75}))
        graph_counts = app.state.runtime.graph_store.counts()
        check_edges(request(client, "POST", "/api/v1/relations/infer", json={"as_of": 75}))
        assert graph_counts == app.state.runtime.graph_store.counts()
        checkpoints = []
        for seconds, count in ((59, 0), (75, 1), (95, 0), (360, 0)):
            graph = request(client, "GET", "/api/v1/model/graph", params={"as_of": seconds})
            check_objects(graph["nodes"])
            check_edges(graph["edges"])
            impact = request(client, "GET", "/api/v1/model/impact/CNC_01", params={"as_of": seconds})
            assert len(impact["active_impacts"]) == count
            checkpoints.append({"as_of": seconds, "status": next(o["state"]["status"] for o in graph["nodes"] if o["object_id"] == "CNC_01"),
                                "active_impacts": len(impact["active_impacts"]), "paths": [r["path"] for r in impact["active_impacts"]],
                                "impact_events": [{k: r[k] for k in ("path", "start_s", "end_s", "status")} for r in impact["impact_events"]]})
            app.state.runtime.manager.snapshot(seconds, save=True)
        assert len(request(client, "GET", "/api/v1/model/snapshots")) == 4
        depend = request(client, "GET", "/api/v1/model/impact/ORDER_A", params={"as_of": 75, "direction": "downstream"})
        assert set(depend["dependencies"]["object_ids"]) == {o["object_id"] for o in objects}
        check_objects(request(client, "GET", "/semantic/objects"))
        check_objects(request(client, "GET", "/semantic/objects/CNC_01/history"))
        check_objects(request(client, "POST", "/semantic/object", json=raw[0]))
        check_edges(request(client, "GET", "/relations"))
        check_edges(request(client, "POST", "/relations/infer", json={"objects": objects}))
        check_objects(request(client, "GET", "/model/graph", params={"as_of": 75})["nodes"])
        assert len(request(client, "GET", "/model/impact/CNC_01", params={"as_of": 75})["active_impacts"]) == 1
        request(client, "GET", "/model/snapshots")
        for bad in invalid_examples(wp1):
            request(client, "POST", "/api/v1/objects/ingest", expected=422, json=bad)
        assert counts == app.state.runtime.store.counts()
        # The main completed run must refuse to inject a new fault into an already-finished order.
        request(client, "POST", "/api/v1/events/trigger", expected=422,
                json=dict(event_id="completed-refusal", kind="fault", object_id="CNC_01", sim_time_s=365))
        standard = sorted(path for path in app.openapi()["paths"] if path.startswith("/api/v1/"))
        assert len(standard) == 10
        runtime_counts = {"semantic": app.state.runtime.store.counts(), "graph": app.state.runtime.graph_store.counts(),
                          "connector": app.state.runtime.session.summary()}
    assert app.state.runtime.closed and branch.state.runtime.closed
    return dict(api_checks=checks, api_standard_routes=len(standard), routes=standard, gateway_counts=runtime_counts,
                checkpoints=checkpoints, online_event_summary=[{"kind": r["source_records"][-1]["payload"].get("state_action", "fault"),
                            "status": r["objects"][0]["state"]["status"], "active_impacts": len(r["impact"]["active_impacts"])} for r in events])


def weekly_report(summary):
    lines = ["# 任务2.1 · 导师每周汇报五问", "", f'本次运行：{summary["run_id"]}；北京时间：{summary["executed_at"]}。', "",
             "## ① 本周完成哪个模块？", "", "完成阶段1的五层本地原型：多源接入、语义对象化、动态关联、四层时空模型、统一API网关及离线演示看板。复用真实ERP创建订单、MES下达/分配与SimPy生产仿真。", "",
             "## ② 输入是什么？", "", f'本次从零生成{summary["source_records"]}条源协议记录、{summary["signal_samples"]}条四路信号、{summary["image_count"]}张PNG和{summary["log_count"]}条设备日志。接入后共{summary["raw_messages"]}条八字段RawIngestMessage，{summary["rejected_messages"]}条坏样本拒收，{summary["duplicate_messages"]}条源重复消息去重。', "",
             "## ③ 输出是什么？", "", f'{summary["objects"]}个七字段语义对象、{summary["semantic_history"]}条历史、{summary["signal_cycles"]}个对齐物理周期、{summary["relations"]}条七字段关系及{summary["snapshots"]}个时空快照。', "",
             "| 仿真时刻 | CNC_01诊断 | 活跃风险 | 路径 |", "|---:|---|---:|---|"]
    for c in summary["checkpoints"]:
        lines.append(f'| {c["as_of"]}s | {c["status"]} | {c["active_impacts"]} | {"；".join(" → ".join(p) for p in c["paths"]) or "空"} |')
    lines += ["", "风险为诊断评估，未改变MES实际排程。恢复后仍保留历史审计；物料关系依据显式教学BOM。", "",
              "## ④ 接口是什么？", "", f'Object / State / Relation / Model / Event五类API，共{summary["api_standard_routes"]}条标准路由，兼容原semantic/relations/model路由；本次{len(summary["api_checks"])}次HTTP响应断言通过。', ""]
    lines += [f'- `{route}`' for route in summary["routes"]]
    lines += ["", f'独立在线事件分支状态：{summary["online_event_summary"]}。源协议记录真实经过WP2→WP3→WP4，未直接改图。', "",
              "## ⑤ 当前运行阶段是什么？", "", f'本次状态：**{summary["status"]}**。阶段1为本地Python独立运行、功能与接口实测；默认脚本执行后退出，可选--serve启动本地Uvicorn。**阶段2 Docker化按用户要求暂缓，尚未进入。**', "",
              "MQTT/OPC UA为本地模拟；SQLite为真实本地数据库；Neo4j仅参数化离线导出。合成图标签不代表视觉深度学习识别。",
              "", "查看：e2e_dashboard.html；计数及验收：e2e_summary.json；在线事件：online_event_evidence.json。"]
    return "\n".join(lines) + "\n"


def run_e2e(output):
    output = Path(output).resolve()
    legacy = (PROJECT / "minimal_factory").resolve()
    if output.is_relative_to(legacy) or legacy.is_relative_to(output):
        raise ValueError("E2E output must be separate from minimal_factory")
    for parent in output.parents:
        if any((parent / marker).exists() for marker in ("run.json", "connector_summary.json", "semantic_summary.json", "spatiotemporal_summary.json", "e2e_summary.json")):
            raise ValueError("E2E output must not be nested inside prior artifacts")
    output.mkdir(parents=True, exist_ok=False)
    execution = datetime.now(timezone(timedelta(hours=8), "Asia/Shanghai")).isoformat(timespec="seconds")
    try:
        print("A / WP1 · 创建真实订单、下达工单、运行6分钟仿真", flush=True)
        source = run_demo(output / "wp1")
        assert source["makespan_min"] == 6 and source["counts"]["signals"] == 360
        print("B / WP2 · 多源接入、7类脏数据隔离与重复去重", flush=True)
        ingest = connector_batch(output / "wp1", output / "wp2", exercise_invalid=True, repeat=2)
        assert ingest["counts"] == {"accepted": 2206, "duplicates": 2202, "rejected": 7}
        print("C / WP3 · 七字段映射、跨协议对齐与故障融合", flush=True)
        semantics = semantic_batch(output / "wp2", output / "wp3")
        assert (semantics["semantic_objects"], semantics["semantic_state_history"], semantics["signal_cycles"]) == (12, 2215, 360)
        print("D / WP4 · 动态关联、时间切片与跨层影响", flush=True)
        model = graph_batch(output / "wp3", output / "wp4")
        assert (model["nodes"], model["edges"]) == (12, 19)
        print("E / WP5 · 全路由实测、独立在线故障与恢复", flush=True)
        api = exercise_gateway(output)
        images = read_lines(output / "wp1/images.jsonl")
        snapshots = json.loads((output / "wp4/graph_snapshots.json").read_text(encoding="utf-8"))
        summary = {"status": "completed", "run_id": output.name, "executed_at": execution, "stage": "stage_1_local_python",
                   "dockerized": False, "source_records": sum(source["protocols"].values()), "signal_samples": source["counts"]["signals"],
                   "image_count": len(images), "log_count": source["counts"]["logs"], "makespan_min": source["makespan_min"],
                   "protocol_counts": ingest["by_protocol"], "modality_counts": ingest["by_modality"], "raw_messages": ingest["counts"]["accepted"],
                   "rejected_messages": ingest["counts"]["rejected"], "duplicate_messages": ingest["counts"]["duplicates"],
                   "objects": semantics["semantic_objects"], "semantic_history": semantics["semantic_state_history"], "signal_cycles": semantics["signal_cycles"],
                   "relations": model["edges"], "snapshots": len(snapshots), "risk_episodes": model["risk_episodes"], **api}
        assert summary["source_records"] == 2202 and summary["image_count"] == 9 and summary["log_count"] == 8
        print("F / 汇报五问与离线可视化看板", flush=True)
        write_json(output / "e2e_summary.json", summary)
        report = weekly_report(summary)
        (output / "weekly_5q_report.md").write_text(report, encoding="utf-8")
        render_dashboard(output, summary, snapshots, images)
        print(report, flush=True)
        print(f"完成：{output / 'e2e_dashboard.html'}", flush=True)
        return summary
    except Exception as exc:
        write_json(output / "e2e_summary.json", {"status": "failed", "executed_at": execution, "error": str(exc)})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args(argv)
    output = Path(args.output)
    if not output.is_absolute():
        output = PROJECT / output
    run_e2e(output)
    if args.serve:
        import uvicorn
        print(f"看板：http://{args.host}:{args.port} · Ctrl+C关闭", flush=True)
        uvicorn.run(create_gateway_app(output / "gateway"), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
