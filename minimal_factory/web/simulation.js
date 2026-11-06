// 仿真页面只编辑运行参数；生产和资源规则由后端验证。
const simStateNames={running:'计算中',completed:'已完成',failed:'失败',interrupted:'已中断'};
const simPhaseNames={normal:'正常加工',repair:'返修',reinspection:'复检'};
const simEventNames={resource_requested:'申请设备',resource_acquired:'获得设备',processing_started:'开始加工',processing_finished:'加工结束',resource_released:'释放设备',quality_result:'质检结果',rework_created:'创建返修任务',order_started:'订单进入系统',order_completed:'订单完工'};
Object.assign(simEventNames,{mes_to_field_mqtt_dispatched:'MQTT 工序派发',field_opcua_signal_reported:'OPC UA 信号采集',field_vision_http_reported:'HTTP 视觉质检',field_modbus_reported:'Modbus 寄存器采集',device_anomaly_affected_work_order:'设备异常影响工单',device_recovered:'设备恢复'});
const simModes={normal:'正常生产模式 (Normal - 12分钟基准)',anomaly:'多模态异常级联演示 (Anomaly - 三设备故障/返工级联)',sampled:'随机抽样模式 (Sampled)'};
const simView={loaded:false,loading:false,snapshot:null,base:{source:'work_orders',work_order_ids:[]},source:'work_orders',mode:'normal',runs:[],result:null,timer:null,polling:false,error:'',eventOrder:'',eventEquipment:'',eventPage:0};
function simDefaultBase(){
  const released=state.workorders.filter(w=>w.status==='released');
  return {source:'work_orders',work_order_ids:released.length===1?[released[0].work_order_id]:[]};
}
function stopSimPolling(){clearTimeout(simView.timer);simView.timer=null}
function simStatus(status){return badge(status,simStateNames[status]||status)}
async function loadSim(){
  if(simView.loading)return;
  simView.loading=true;simView.error='';
  try{
    simView.base=simDefaultBase();simView.source='work_orders';
    simView.runs=await api('/simulation/runs');
    simView.snapshot=simView.base.work_order_ids.length?await api('/simulation/preview','POST',simView.base):null;
    simView.loaded=true;
  }catch(e){simView.error=e.message}
  finally{simView.loading=false;if(page==='simulation')render()}
}
function simHistory(){
  return `<details class="panel sim-history" open><summary>实验历史 · ${simView.runs.length} 次</summary>${simView.runs.length?`<div class="table-wrap"><table><thead><tr><th>运行编号 / 时间</th><th>来源</th><th>工单</th><th>状态</th><th>操作</th></tr></thead><tbody>${simView.runs.map(r=>`<tr><td><span class="sim-run-id">${esc(r.run_id)}</span><span class="sub">${esc(time(r.created_at))}</span></td><td>${{demo:'教学案例',work_orders:'工单副本',replay:'复制实验'}[r.source]}</td><td>${r.completed_orders} / ${r.total_orders}</td><td>${simStatus(r.status)}</td><td><div class="actions"><button class="small" data-sim="open" data-id="${r.run_id}">查看结果</button><button class="small" data-sim="copy" data-id="${r.run_id}">复制参数</button></div></td></tr>`).join('')}</tbody></table></div>`:empty('还没有正式批次。选择现有 MES 工单与运行模式，由你点击“开始仿真”生成。')}</details>`;
}
function simEditor(w){
  const eligible=w.operations.filter(op=>op.kind==='inspection'&&w.operations.some(t=>t.kind!=='inspection'&&t.sequence<op.sequence));
  const failure=w.failure,source=w.operations.find(o=>o.operation_id===failure?.inspection_operation_id);
  const targets=source?w.operations.filter(o=>o.kind!=='inspection'&&o.sequence<source.sequence):[];
  return `<article class="sim-order-editor" data-wo="${esc(w.work_order_id)}"><h3>${esc(w.product_name)}</h3><div class="muted sim-run-id">${esc(w.work_order_id)}</div><div class="form-grid"><label>本批数量<input class="sim-quantity" type="number" min="1" step="1" required value="${w.quantity}"></label><label>指定一次质检失败<select class="sim-failure-source" aria-label="${esc(w.product_name)}失败工序"><option value="">全部合格</option>${eligible.map(op=>`<option value="${esc(op.operation_id)}" ${failure?.inspection_operation_id===op.operation_id?'selected':''}>${op.sequence}. ${esc(op.name)}</option>`).join('')}</select></label></div><div class="sim-minutes">${w.operations.map(op=>`<label>${op.sequence}. ${esc(op.name)} · 分钟／件<input class="sim-minutes-input" data-op="${esc(op.operation_id)}" type="number" min="1" max="10000" step="1" required value="${op.minutes}"><small class="muted">${esc(simView.snapshot.equipment.find(e=>e.equipment_id===op.equipment_id)?.name||op.equipment_id)}</small></label>`).join('')}</div>${failure?`<div class="form-grid"><label>本次不合格数量<input class="sim-rejected" type="number" min="1" max="${w.quantity}" step="1" required value="${failure.quantity}"></label><label>返回哪道工序<select class="sim-failure-target">${targets.map(op=>`<option value="${esc(op.operation_id)}" ${failure.target_operation_id===op.operation_id?'selected':''}>${op.sequence}. ${esc(op.name)}</option>`).join('')}</select></label></div><p class="footnote">只在此工序的首次质检注入失败；返修后复检固定通过。原工单不受影响。</p>`:''}</article>`;
}
function captureSimEdits(){
  if(!simView.snapshot)return;
  if($('#sim-field-mode'))simView.mode=$('#sim-field-mode').value;
  for(const editor of content.querySelectorAll('.sim-order-editor')){
    const w=simView.snapshot.orders.find(x=>x.work_order_id===editor.dataset.wo);
    w.quantity=Number(editor.querySelector('.sim-quantity').value);
    for(const input of editor.querySelectorAll('.sim-minutes-input'))w.operations.find(o=>o.operation_id===input.dataset.op).minutes=Number(input.value);
    const source=w.operations.find(o=>o.operation_id===editor.querySelector('.sim-failure-source').value);
    if(source){
      const candidates=w.operations.filter(o=>o.kind!=='inspection'&&o.sequence<source.sequence);
      const chosen=editor.querySelector('.sim-failure-target')?.value;
      w.failure={inspection_operation_id:source.operation_id,target_operation_id:candidates.find(o=>o.operation_id===chosen)?.operation_id||candidates[0].operation_id,quantity:Number(editor.querySelector('.sim-rejected')?.value||1)};
    }else w.failure=null;
  }
}
function simRequest(){
  return {...simView.base,overrides:Object.fromEntries(simView.snapshot.orders.map(w=>[w.work_order_id,{
    quantity:w.quantity,minutes:Object.fromEntries(w.operations.map(o=>[o.operation_id,o.minutes])),failure:w.failure,
    field_protocols:{...(w.field_protocols||{}),mode:simView.mode}
  }]))};
}
function renderSimulation(){
  stopSimPolling();
  if(!simView.loaded){
    content.innerHTML=heading('SIMPY / EXPERIMENTS','生产仿真','让设备排队、加工时间和返修过程自动产生数据。')+
      (simView.error?`<div class="error">${esc(simView.error)}</div><button data-sim="retry">重试读取</button>`:empty('正在读取实验配置…'));
    if(!simView.loading&&!simView.error)void loadSim();
    return;
  }
  content.innerHTML=heading('SIMPY / EXPERIMENTS','生产仿真','在独立副本中运行，按虚拟分钟观察生产。',`<button data-sim="new">新实验</button><button data-sim="history">刷新实验记录</button>`)+
    `<div class="sim-intro"><h2>输入工单 → 自动执行 → 可追溯结果</h2><p>每次实验复制工艺和设备配置，从空闲状态开始。仿真不会修改原订单，不继承现实占用和维护状态。设备队列先到先服务；整批完成后流入下一工序。</p></div>`+
    (simView.error?`<div class="error" role="alert">${esc(simView.error)}</div>`:'')+
    (simView.result?simResult(simView.result):simConfig())+simHistory();
  if($('#sim-config-form')){
    $('#sim-config-form').oninput=captureSimEdits;
    $('#sim-config-form').onchange=captureSimEdits;
    $('#sim-config-form').onsubmit=async ev=>{
      ev.preventDefault();captureSimEdits();
      const b=$('#sim-start');b.disabled=true;simView.error='';
      try{const started=await api('/simulation/runs','POST',simRequest());await openSimResult(started.run_id)}
      catch(e){simView.error=e.message;render()}finally{if($('#sim-start'))$('#sim-start').disabled=false}
    };
    for(const select of content.querySelectorAll('.sim-failure-source'))select.onchange=()=>{captureSimEdits();render()};
  }
  if($('#sim-source'))$('#sim-source').onchange=()=>{
    simView.source=$('#sim-source').value;simView.snapshot=null;simView.error='';render()
  };
  if($('#sim-field-mode'))$('#sim-field-mode').onchange=()=>{captureSimEdits();render()};
  if($('#sim-event-order'))$('#sim-event-order').onchange=()=>{simView.eventOrder=$('#sim-event-order').value;simView.eventPage=0;render()};
  if($('#sim-event-equipment'))$('#sim-event-equipment').onchange=()=>{simView.eventEquipment=$('#sim-event-equipment').value;simView.eventPage=0;render()};
  if(simView.result?.status==='running')scheduleSimPoll();
}
function simConfig(){
  const existing=state.workorders.filter(w=>w.status!=='cancelled');
  return `<section class="panel"><h2>配置实验</h2>${simView.base.source==='replay'&&simView.snapshot?`<p class="guide">正在复制已保存的输入快照，可直接重跑或调整参数。原实验和来源工单保持不变。</p>`:''}<div class="sim-source"><label>输入来源<select id="sim-source"><option value="work_orders" ${simView.source==='work_orders'?'selected':''}>选择现有 MES 工单</option><option value="demo" ${simView.source==='demo'?'selected':''}>默认教学案例 · 两张订单</option></select></label><button data-sim="load">载入输入配置</button></div>${simView.source==='work_orders'?`<div class="sim-selection">${existing.map(w=>`<label><input type="checkbox" class="sim-select-wo" value="${esc(w.work_order_id)}" ${simView.base.work_order_ids?.includes(w.work_order_id)?'checked':''}><span>${esc(w.product_name)} · ${w.quantity} 件 · ${esc(labels[w.status])}<br><span class="sim-run-id">${esc(w.work_order_id)}</span></span></label>`).join('')||empty('暂无可选工单。请先在 MES 准备已分配设备的工单。')}</div><p class="footnote">最多选择 50 张。已完工工单也可复制；每道工序需已分配设备。</p>`:''}${simView.snapshot?`<form id="sim-config-form"><div class="sim-mode-selector"><label for="sim-field-mode">运行模式</label><select id="sim-field-mode">${Object.entries(simModes).map(([value,label])=>`<option value="${value}" ${simView.mode===value?'selected':''}>${label}</option>`).join('')}</select><p>${simView.mode==='anomaly'?'三设备异常素材触发停机、返工与复检；本工单默认 12 → 28 分钟，延误 +16。':simView.mode==='sampled'?'使用已保存种子抽样正常/异常素材；复制参数可重放抽样结果。':'正常素材，不注入现场故障；当前法兰 2 件、3/2/1 分钟每件的基准为 12 分钟。'}</p></div><p class="muted">每件时长 × 本批数量 = 该工序占用设备的时间。所有工单在 t=0 进入系统。</p>${simView.snapshot.orders.map(simEditor).join('')}<div class="actions"><button class="primary" id="sim-start" type="submit">开始仿真</button><span class="muted">点击才创建独立批次；不改变主库工单。</span></div></form>`:empty('选择工单后点击“载入输入配置”。')}</section>`;
}
function simChart(result){
  const total=result.makespan_min;
  const woLabels=Object.fromEntries(result.orders.map((w,i)=>[w.work_order_id,`订单 ${i+1}`]));
  return `<section class="panel"><h2>设备时间图</h2><div class="sim-legend"><span style="--color:#268a77">正常加工</span><span style="--color:#b27125">返修</span><span style="--color:#6e67b0">复检</span></div><p class="muted">横轴为虚拟分钟；空白表示设备空闲。指向色块查看工单与起止时间，明细见下表。</p><div class="sim-chart-scroll"><div class="sim-chart"><div></div><div class="sim-axis">${Array.from({length:6},(_,i)=>`<span>${Number((total*i/5).toFixed(1))}</span>`).join('')}</div>${result.equipment.map(eq=>`<div class="sim-chart-label">${esc(eq.name)}<br>${eq.utilization}% 利用率</div><div class="sim-chart-track">${result.intervals.filter(i=>i.equipment_id===eq.equipment_id).map(i=>`<div class="sim-block ${i.phase}" style="left:${100*i.start_min/total}%;width:${100*(i.end_min-i.start_min)/total}%" title="${esc(woLabels[i.work_order_id])} · ${esc(i.name)} · ${simPhaseNames[i.phase]} · ${i.start_min}—${i.end_min} 分钟"><span>${esc(woLabels[i.work_order_id])}</span><br>${i.start_min}—${i.end_min}</div>`).join('')}</div>`).join('')}</div></div><details><summary>加工区间明细 · ${result.intervals.length} 段</summary><div class="table-wrap"><table><thead><tr><th>工单</th><th>设备 / 工序</th><th>阶段</th><th>起止分钟</th><th>数量</th></tr></thead><tbody>${result.intervals.map(i=>`<tr><td>${esc(woLabels[i.work_order_id])}</td><td>${esc(result.equipment.find(e=>e.equipment_id===i.equipment_id)?.name)} / ${esc(i.name)}</td><td>${simPhaseNames[i.phase]}</td><td>${i.start_min}—${i.end_min}</td><td>${i.quantity}</td></tr>`).join('')}</tbody></table></div></details></section>`;
}
function simMinutes(value){return Number.isFinite(Number(value))?Number(Number(value).toFixed(3)):'—'}
function simImageUrl(path){
  const name=String(path||'').replace(/\\/g,'/').split('/').pop();
  return /^(normal|defect)_(front|side)\.png$/.test(name)?`/static/rendered_v1/${name}`:'';
}
function simPhysicalValues(protocol,payload={}){
  if(protocol==='HTTP-Vision-v1')return `螺栓 ${payload.detected_bolts??'—'}/${payload.expected_bolts??4} · ${payload.defect_detected?'缺失螺栓':'装配合格'}`;
  const values=payload.values,regs=payload.registers;
  if(values)return `电流 ${simMinutes(values.current_a)} A · 温度 ${simMinutes(values.temperature_c)} ℃ · 负载 ${simMinutes(values.load_percent)} %`;
  if(regs)return `电流 ${simMinutes(regs['40001']/1000)} A · 温度 ${simMinutes(regs['40002']/10)} ℃ · 负载 ${simMinutes(regs['40003']/10)} %`;
  return '未保存观测数值';
}
function simDelaySummary(r){
  const rows=r.delay_summary||r.summary?.delay_summary||[];
  if(!rows.length)return '';
  return `<section class="sim-delay-section" aria-label="工单计划与实际延误对比">${rows.map(w=>`<article class="sim-delay-banner ${w.anomaly_status==='DELAYED_BY_FAULT'?'fault':'normal'}"><div class="panel-head"><h2>工单计划 vs 实际</h2><span class="sim-signal ${w.anomaly_status==='DELAYED_BY_FAULT'?'fault':'normal'}">${esc(w.anomaly_status)}</span></div><div class="sim-delay-values"><div><small>计划总时长</small><strong>${simMinutes(w.planned_minutes)}<span> 虚拟分钟</span></strong></div><div><small>实际总时长</small><strong>${simMinutes(w.actual_minutes)}<span> 虚拟分钟</span></strong></div><div><small>${w.anomaly_status==='DELAYED_BY_FAULT'?'故障延误时长':'延误时长'}</small><strong>+${simMinutes(w.delay_minutes)}<span> 虚拟分钟</span></strong></div></div><p class="sim-run-id">${esc(w.work_order_id)}</p></article>`).join('')}</section>`;
}
function simVisionFigure(payload,caption){
  const url=simImageUrl(payload?.image_path);
  return url?`<figure><a href="${url}" target="_blank" rel="noopener" title="点击查看原图"><img src="${url}" alt="${esc(caption)} · 螺栓 ${esc(payload.detected_bolts)}/${esc(payload.expected_bolts)}" loading="lazy" width="800" height="600"></a><figcaption>${esc(caption)} · 螺栓 ${esc(payload.detected_bolts)}/${esc(payload.expected_bolts)} · 点击放大</figcaption></figure>`:'';
}
function simEquipmentAlerts(r){
  const alerts=r.equipment_alerts||r.summary?.equipment_alerts||[];
  if(!alerts.length)return '';
  const order=['E-ASM','E-QC','E-MOVE'];
  return `<section class="panel"><h2>三设备多模态监控</h2><p class="muted">红灯保留本批次曾发生的故障；恢复时刻与当前状态同时展示。</p><div class="sim-alert-grid">${[...alerts].sort((a,b)=>order.indexOf(a.equipment_id)-order.indexOf(b.equipment_id)).map(a=>{
    const payload=a.evidence||a.latest_evidence||{},faults=a.fault_records||[];
    const imagePayload={...payload,image_path:payload.image_path||a.source_file};
    const timeline=faults.map(f=>`第 ${simMinutes(f.virtual_minute)} 分钟故障 → ${f.recovered_virtual_minute!=null?`第 ${simMinutes(f.recovered_virtual_minute)} 分钟维修/返工恢复为 available`:'尚未恢复'}`).join('；');
    return `<article class="sim-alert-card ${a.had_fault?'fault':'normal'}" data-equipment="${esc(a.equipment_id)}"><div class="panel-head"><h3>${esc(a.device_code||a.equipment_name)}</h3><span class="sim-signal ${a.had_fault?'fault':'normal'}">${a.had_fault?'曾触发 FAULT':'NORMAL'}</span></div><p class="sim-protocol-label">${esc(a.protocol)}</p><p class="sim-recovery">${esc(a.had_fault?timeline:'本批次未触发故障')}<br>当前状态：<strong>${esc(a.current_status||'available')}</strong></p><p class="sim-alarm-message">${esc(a.alarm_message||'设备运行正常，本批次无故障报警。')}</p><p class="sim-physical">${esc(simPhysicalValues(a.protocol,payload))}</p>${a.protocol==='HTTP-Vision-v1'?`<div class="sim-vision-images">${simVisionFigure(imagePayload,a.had_fault?'首次质检 · 缺陷法兰':'质检抓拍 · 合格法兰')}${a.had_fault&&a.latest_evidence?.image_path&&a.latest_evidence.image_path!==imagePayload.image_path?simVisionFigure(a.latest_evidence,'返工后复检 · 合格法兰'):''}</div>`:''}</article>`;
  }).join('')}</div></section>`;
}
function simProtocolSummary(message){
  const p=message.payload||{};
  return message.protocol==='MQTT-Dispatch-v1'?`${p.command||'START'} · 派发 ${p.planned_qty??'—'} 件`:simPhysicalValues(message.protocol,p);
}
function simProtocolMessages(r){
  const messages=[...(r.protocol_messages||r.summary?.protocol_messages||[])].sort((a,b)=>a.virtual_minute-b.virtual_minute);
  return `<section class="panel" id="sim-protocol-messages"><h2>四类异构协议报文流 · ${messages.length} 条</h2><p class="muted">按虚拟分钟排序；展开查看本批次保存的完整原始 JSON。报文由本地素材回放生成。</p>${messages.length?`<div class="table-wrap"><table><thead><tr><th>虚拟分钟</th><th>协议类型</th><th>设备</th><th>是否异常</th><th>摘要 / 原始报文</th><th>源文件路径</th></tr></thead><tbody>${messages.map(m=>{
    let raw=m.raw_json??m;
    if(typeof raw==='string'){try{raw=JSON.parse(raw)}catch{}}
    const text=typeof raw==='string'?raw:JSON.stringify(raw,null,2);
    return `<tr><td>t=${simMinutes(m.virtual_minute)}</td><td><span class="sim-protocol-badge">${esc(m.protocol)}</span></td><td>${esc(m.device_code||m.equipment_id)}</td><td><span class="sim-signal ${m.is_anomaly?'fault':'normal'}">${m.is_anomaly?'异常':'正常'}</span></td><td>${esc(simProtocolSummary(m))}<details><summary>查看完整 raw_json</summary><pre class="sim-raw-json">${esc(text)}</pre></details></td><td><span class="sim-source-path">${esc(m.source_file)}</span></td></tr>`;
  }).join('')}</tbody></table></div>`:empty('此批次尚无现场协议报文；早期历史批次可能未记录此字段。')}</section>`;
}
function simResult(r){
  const summary=r.summary;
  const links=`<a href="/simulation/runs/${r.run_id}/export?format=json">完整 JSON</a><a href="/simulation/runs/${r.run_id}/export?format=events_csv">事件 CSV</a>${summary?`<a href="/simulation/runs/${r.run_id}/export?format=summary_csv">汇总 CSV</a>`:''}`;
  let html=`<section class="panel"><div class="panel-head"><h2>实验结果</h2>${simStatus(r.status)}</div><p class="sim-run-id">${r.run_id}</p><p class="muted">${esc(time(r.created_at))} · ${r.total_orders} 张工单 · 独立数据库副本</p><div class="actions"><button data-sim="copy" data-id="${r.run_id}">复制参数重新运行</button>${links}</div></section>`;
  if(r.status==='running')html+=`<div class="sim-pending"><strong>正在计算生产过程…</strong><p>已完成 ${r.completed_orders} / ${r.total_orders} 张工单，当前虚拟时间 ${r.sim_time_min} 分钟。</p><p>页面会自动更新；离开页面不会取消后台计算。</p></div>`;
  if(r.error)html+=`<div class="error">本次实验未完成：${esc(r.error)}。已保留输入和已产生的事件。</div>`;
  if(summary){
    html+=simDelaySummary(r)+simEquipmentAlerts(r);
    html+=`<div class="metrics">${[['总生产时间',summary.makespan_min+' 分钟'],['完工数量',summary.completed_quantity+' 件'],['累计等待',summary.total_waiting_min+' 分钟'],['返修数量',summary.rework_quantity+' 件']].map(([label,value])=>`<div class="metric"><small>${label}</small><strong class="value" style="font-size:25px">${value}</strong></div>`).join('')}</div>`;
    html+=`<section class="panel"><h2>订单结果与完工反馈</h2><div class="table-wrap"><table><thead><tr><th>工单 / 产品</th><th>数量</th><th>完成时刻</th><th>等待时间</th><th>返修数量</th><th>副本 MES / ERP</th></tr></thead><tbody>${summary.orders.map((w,i)=>`<tr><td>订单 ${i+1} · ${esc(w.product_name)}<span class="sub sim-run-id">${esc(w.work_order_id)}</span></td><td>${w.quantity}</td><td>${w.completed_min} 分钟</td><td>${w.waiting_min} 分钟</td><td>${w.rework_quantity}</td><td>${esc(labels[w.mes_status])} / ${esc(labels[w.erp_status])}</td></tr>`).join('')}</tbody></table></div><p class="footnote">等待时间累计正常加工、返修与复检的设备排队。利用率按设备占用时间 ÷ 本次总生产时间计算。</p></section>`+simChart(summary);
  }
  return html+simProtocolMessages(r)+simEvents(r)+`<details class="panel"><summary>本次输入快照与来源</summary><p class="muted">编号保留在本批次独立命名空间内；不会把仿真结果写回原工单。</p>${r.input.orders.map(w=>`<p><strong>${esc(w.product_name)}</strong> · ${w.quantity} 件 · ${esc(w.work_order_id)}<br>${w.operations.map(o=>`${esc(o.name)}（${o.minutes} 分钟／件）`).join(' → ')}<br>现场模式：${esc(w.field_protocols?.mode||'normal')} · ${w.failure?`另指定质检失败 ${w.failure.quantity} 件`:'无额外手工质检失败配置'}</p>`).join('')}</details>`;
}
function simEvents(r){
  const filtered=r.events.filter(e=>(!simView.eventOrder||e.work_order_id===simView.eventOrder)&&(!simView.eventEquipment||e.equipment_id===simView.eventEquipment));
  const start=simView.eventPage*50,visible=filtered.slice(start,start+50);
  return `<section class="panel"><h2>生产事件 · ${filtered.length} 条</h2><div class="sim-event-filters"><select id="sim-event-order" aria-label="按工单筛选事件"><option value="">全部工单</option>${r.input.orders.map((w,i)=>`<option value="${esc(w.work_order_id)}" ${simView.eventOrder===w.work_order_id?'selected':''}>订单 ${i+1} · ${esc(w.product_name)}</option>`).join('')}</select><select id="sim-event-equipment" aria-label="按设备筛选事件"><option value="">全部设备</option>${r.input.equipment.map(e=>`<option value="${esc(e.equipment_id)}" ${simView.eventEquipment===e.equipment_id?'selected':''}>${esc(e.name)}</option>`).join('')}</select></div><div class="table-wrap"><table><thead><tr><th>序号 / 虚拟分钟</th><th>事件</th><th>工单 / 工序</th><th>设备 / 阶段</th><th>数量 / 结果</th></tr></thead><tbody>${visible.map(e=>`<tr><td>${e.sequence} / t=${e.sim_time_min}</td><td>${simEventNames[e.event_type]||esc(e.event_type)}</td><td><span class="sim-run-id">${esc(e.work_order_id)}</span><span class="sub">${esc(e.operation_id||'—')}</span></td><td>${esc(e.equipment_id||'—')}<span class="sub">${simPhaseNames[e.phase]}</span></td><td>${e.quantity} 件${e.wait_min!==undefined?` · 等待 ${e.wait_min} 分钟`:''}${e.accepted_quantity!==undefined?` · 合格 ${e.accepted_quantity} / 不合格 ${e.rejected_quantity}`:''}</td></tr>`).join('')}</tbody></table></div>${!visible.length?empty('没有匹配的事件。'):''}<div class="actions"><button data-sim="prev" ${start===0?'disabled':''}>上一页</button><span>${filtered.length?start+1:0}—${Math.min(start+50,filtered.length)} / ${filtered.length}</span><button data-sim="next" ${start+50>=filtered.length?'disabled':''}>下一页</button></div><p class="footnote">虚拟分钟来自 SimPy；真实记录时间保留在导出数据中，两者互不替代。</p></section>`;
}
async function openSimResult(id){
  stopSimPolling();
  const [result,runs]=await Promise.all([api(`/simulation/runs/${id}`),api('/simulation/runs')]);
  simView.result=result;simView.runs=runs;simView.error='';simView.eventOrder='';simView.eventEquipment='';simView.eventPage=0;
  if(page==='simulation'){render();content.scrollIntoView({block:'start'})}
}
function scheduleSimPoll(){
  simView.timer=setTimeout(async()=>{
    if(page!=='simulation'||!simView.result||simView.polling)return;
    simView.polling=true;const id=simView.result.run_id;
    try{
      const r=await api(`/simulation/runs/${id}`);
      if(simView.result?.run_id===id){simView.result=r;if(r.status!=='running')simView.runs=await api('/simulation/runs');if(page==='simulation')render()}
    }catch(e){simView.error=e.message;if(page==='simulation')render()}
    finally{simView.polling=false}
  },1000);
}
content.addEventListener('click',async ev=>{
  const b=ev.target.closest('[data-sim]');if(!b||b.disabled)return;
  const action=b.dataset.sim;simView.error='';
  try{
    b.disabled=true;
    if(action==='retry'){simView.error='';await loadSim();return}
    if(action==='new'){simView.result=null;simView.source='work_orders';simView.mode='normal';simView.base=simDefaultBase();simView.snapshot=simView.base.work_order_ids.length?await api('/simulation/preview','POST',simView.base):null}
    if(action==='load'){
      const ids=[...content.querySelectorAll('.sim-select-wo:checked')].map(e=>e.value);
      const base=simView.source==='demo'?{source:'demo'}:{source:'work_orders',work_order_ids:ids};
      const snapshot=await api('/simulation/preview','POST',base);simView.base=base;simView.snapshot=snapshot;
    }
    if(action==='history'){captureSimEdits();simView.runs=await api('/simulation/runs')}
    if(action==='open'){await openSimResult(b.dataset.id);return}
    if(action==='copy'){
      const base={source:'replay',replay_run_id:b.dataset.id};
      const snapshot=await api('/simulation/preview','POST',base);
      simView.base=base;simView.snapshot=snapshot;simView.source=snapshot.source;simView.mode=snapshot.orders[0]?.field_protocols?.mode||'normal';simView.result=null;
    }
    if(action==='prev')simView.eventPage=Math.max(0,simView.eventPage-1);
    if(action==='next')simView.eventPage++;
    if(page==='simulation'){render();if(['new','copy','load'].includes(action))content.scrollIntoView({block:'start'})}
  }catch(e){simView.error=e.message;if(page==='simulation')render()}
  finally{b.disabled=false}
});
