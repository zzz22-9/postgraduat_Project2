"""把已保存的两次教学实验生成不依赖后台/网络的静态演示页，不重跑仿真。"""
from datetime import datetime, timedelta, timezone
from html import escape
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = [("默认全合格", "SIM-97bdfc0915964e5ba0b0a33c3466a301"),
         ("订单 1 质检失败、返回组装", "SIM-531f1894c22543a392188e2c827729bc")]


def main():
    sections = []
    for title, run_id in CASES:
        folder = ROOT / "data" / "erp.simulations" / run_id
        metadata = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        if metadata["status"] != "completed":
            raise ValueError(f"{run_id} 尚未完成，不能作为完成结果导出")
        result = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
        total = result["makespan_min"]
        rows = "".join(f'<tr><td>{escape(o["work_order_id"])}</td><td>{o["quantity"]}</td>'
                       f'<td>{o["completed_min"]} 分钟</td><td>{o["waiting_min"]} 分钟</td>'
                       f'<td>{o["rework_quantity"]}</td><td>{escape(o["mes_status"])} / {escape(o["erp_status"])}</td></tr>'
                       for o in result["orders"])
        charts, details = [], []
        phases = {"normal": "正常加工", "repair": "返修", "reinspection": "复检"}
        colors = {"normal": "#187c70", "repair": "#ac7022", "reinspection": "#7162ab"}
        for eq in result["equipment"]:
            blocks = []
            for interval in result["intervals"]:
                if interval["equipment_id"] != eq["equipment_id"]:
                    continue
                start, end = interval["start_min"], interval["end_min"]
                label = f'{interval["work_order_id"]} · {phases[interval["phase"]]} · {start}—{end}'
                blocks.append(f'<div class="block" title="{escape(label, quote=True)}" '
                              f'style="left:{100*start/total}%;width:{100*(end-start)/total}%;background:{colors[interval["phase"]]}">'
                              f'{escape(interval["work_order_id"].replace("DEMO-WO-", "订单 "))}<br>{start}—{end}</div>')
                details.append(f'<tr><td>{escape(interval["work_order_id"])}</td><td>{escape(eq["name"])}</td>'
                               f'<td>{phases[interval["phase"]]}</td><td>{start}—{end}</td></tr>')
            charts.append(f'<div class="device">{escape(eq["name"])}<small>{eq["utilization"]}% 利用率</small></div>'
                          f'<div class="track">{"".join(blocks)}</div>')
        sections.append(f'''<section><h2>{escape(title)}</h2>
<p class="meta">来源批次：{run_id}<br>原运行时间（UTC）：{escape(metadata["created_at"])}</p>
<div class="metrics"><b>总生产时间 {total} 分钟</b><b>完工 {result["completed_quantity"]} 件</b>
<b>累计等待 {result["total_waiting_min"]} 分钟</b><b>返修 {result["rework_quantity"]} 件</b></div>
<div class="scroll"><table><thead><tr><th>工单</th><th>数量</th><th>完成时刻</th><th>等待</th><th>返修数量</th><th>副本 MES / ERP</th></tr></thead><tbody>{rows}</tbody></table></div>
<h3>设备时间图 · 虚拟分钟</h3><p>绿色：正常加工　棕色：返修　紫色：复检</p>
<div class="scroll"><div class="chart"><div></div><div class="axis"><span>0</span><span>{total/2:g}</span><span>{total}</span></div>{"".join(charts)}</div></div>
<details><summary>查看加工区间</summary><div class="scroll"><table><thead><tr><th>工单</th><th>设备</th><th>阶段</th><th>起止分钟</th></tr></thead><tbody>{"".join(details)}</tbody></table></div></details></section>''')
    generated = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    html = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>最小工厂 · 离线实验结果</title>
<style>body{margin:0;background:#f2f6f5;color:#183631;font:15px/1.7 "Microsoft YaHei",sans-serif}main{max-width:1120px;margin:auto;padding:28px}header{background:#17383a;color:white;padding:26px;border-radius:14px}h1{margin:0}section{background:white;border:1px solid #d9e5df;padding:24px;border-radius:14px;margin:24px 0}.meta,small{color:#63736f;overflow-wrap:anywhere}.metrics{display:flex;gap:24px;flex-wrap:wrap;margin:20px 0}table{border-collapse:collapse;width:100%;white-space:nowrap}td,th{padding:10px;text-align:left;border-bottom:1px solid #e0e9e5}.scroll{overflow-x:auto}.chart{display:grid;grid-template-columns:155px minmax(560px,1fr);min-width:740px;gap:14px 8px;align-items:center}.axis{display:flex;justify-content:space-between}.track{position:relative;height:55px;background:#f1f5f3}.block{position:absolute;height:46px;top:4px;color:white;font-size:12px;text-align:center;overflow:hidden;white-space:nowrap;border-radius:4px;box-sizing:border-box;border-right:1px solid white}.device small{display:block}summary{cursor:pointer;margin-top:24px}@media(max-width:650px){main{padding:12px}section{padding:16px}}@media print{section{break-inside:avoid}header{background:white;color:#183631}.block{print-color-adjust:exact}}</style></head><body><main>
<header><h1>最小工厂 · 离线实验结果</h1><p>已保存实验的静态快照。无需后台或网络，不能执行新仿真，也不代表服务当前在线。</p></header>
<p>输入工单 → 设备排队 → 自动报工 → 质检返修与复检 → 副本完工反馈</p>
<p>每台设备容量 1，整批流转，耗时 = 分钟／件 × 数量。返修和复检参与同一设备队列，原练习数据不受影响。</p>'''
    html += "".join(sections) + f'<footer>导出时间（北京时间）：{generated}。数据来自本项目保存的 input/run/summary 与 MES 副本；此页未重新计算实验。</footer></main></body></html>'
    output = ROOT / "离线演示.html"
    output.write_text(html, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
