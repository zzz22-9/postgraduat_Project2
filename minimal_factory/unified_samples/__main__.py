"""Run from minimal_factory: python -m unified_samples build|query|snapshot|overview|export."""
import argparse
import json
from pathlib import Path

from .store import DEFAULT_DB, build, check, overview, query, snapshot


def export_examples(db):
    out = Path(db).parent / "examples"
    out.mkdir(exist_ok=True)
    data = {"overview": overview(db),
            "order2_images": query(db, work_order="DEMO-WO-2", modality="image"),
            "order2_at_180s": query(db, work_order="DEMO-WO-2", start=180, end=180)}
    for at in [179, 180, 359, 360]:
        data[f"snapshot_{at}s"] = snapshot(db, at=at)
    for name, value in data.items():
        (out / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counts = data["overview"]["counts"]
    report = ["# 统一样本库：本轮导入与查询结果", "",
              "本地SQLite原型；所有场景均为合成回放。图片时间由教学配置指定，不是历史实拍。", "",
              f"已入库：{counts['scenarios']}个场景、{counts['objects']}个对象、{counts['assets']}个图片资产、"
              f"{counts['modalities']['signal']}条信号观测、{counts['modalities']['image']}条图片观测、{counts['modalities']['event']}条组装事件观测。", "",
              "6个图片资产在三个场景中被引用12次；正常541条信号用于正常和缺件场景，异常541条用于电流升高场景，因此存储1623条信号观测，并非新增了1623次采集。", "",
              "|场景|信号条数|超过4 A的采样点|结束前图片|", "|---|---:|---:|---|"]
    for row in data["overview"]["scenarios"]:
        report.append(f"|{row['title']}|{row['samples']}|{row['above_4A_count']}|{'缺一螺栓' if row['scenario_id']=='missing_bolt' else '四颗齐全'}|")
    report += ["", "## 订单2的三种数据在第180秒相遇", "",
               "查询 normal + DEMO-WO-2 + [180,180]秒，返回1条开始事件、1条信号、2张视角图片。此时订单1组装已结束，设备切换到订单2。", "",
               "见 [查询原始结果](order2_at_180s.json)。事件先于信号和图片；同一时刻的事件按源sequence排序。", "",
               "## 图片观测（正常场景）", ""]
    for item in data["order2_images"]["records"]:
        report += [f"**第{item['time_s']:g}秒 · {item['payload']['state']} · {item['payload']['view']}**", "",
                   f"![合成场景图片](../{item['relative_path']})", ""]
    report += ["## 时间快照", "", "|时刻|设备状态|正在服务的工单|", "|---|---|---|"]
    for at in [179, 180, 359, 360]:
        snap = data[f"snapshot_{at}s"]
        equipment = next(o for o in snap["objects"] if o["object_type"] == "Equipment")
        report.append(f"|{at}秒|{equipment['state']}|{equipment['state_context'].get('work_order_id') or '无'}|")
    report += ["", "快照同时包含企业—工厂—产线—设备层级，以及订单→工单→工序关系。层级是教学配置；processing/serves_work_order关系按加工区间生效。", "",
               "订单/工单整体状态保持unknown：本次只导入组装事件，不能据此推断质检通过或订单全部完成。", "",
               "## 当前边界", "", "完成：文件接入、可追溯存储、统一对象/观测字段、规则关系和时间快照查询。", "",
               "未完成：真实设备接入、图片内容识别、因果推断、在线API、Docker、生产级统一服务。4 A仅是教学阈值，缺件与电流升高是独立对照。", ""]
    (out / "导入与查询结果.md").write_text("\n".join(report), encoding="utf-8")
    return {"examples_directory": str(out), "report": str(out / "导入与查询结果.md")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build", "check", "query", "snapshot", "overview", "export"])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--scenario", default="normal")
    parser.add_argument("--equipment", default="E-ASM")
    parser.add_argument("--work-order")
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--end", type=float, default=540)
    parser.add_argument("--at", type=float, default=180)
    parser.add_argument("--modality", choices=["image", "signal", "event"])
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    try:
        if args.command == "query":
            result = query(args.db, args.scenario, args.equipment, args.work_order, args.start, args.end, args.modality, args.limit, args.offset)
        elif args.command == "snapshot":
            result = snapshot(args.db, args.scenario, args.at)
        else:
            result = {"build": build, "check": check, "overview": overview, "export": export_examples}[args.command](args.db)
    except (ValueError, OSError) as error:
        parser.exit(1, f"Error: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
