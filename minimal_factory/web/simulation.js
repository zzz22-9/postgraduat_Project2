// 仿真页面只编辑运行参数；生产和资源规则由后端验证。
const simStateNames={running:'计算中',completed:'已完成',failed:'失败',interrupted:'已中断'};
const simPhaseNames={normal:'正常加工',repair:'返修',reinspection:'复检'};
const simEventNames={resource_requested:'申请设备',resource_acquired:'获得设备',processing_started:'开始加工',processing_finished:'加工结束',resource_released:'释放设备',quality_result:'质检结果',rework_created:'创建返修任务',order_started:'订单进入系统',order_completed:'订单完工'};
const simView={loaded:false,loading:false,snapshot:null,base:{source:'demo'},source:'demo',runs:[],result:null,timer:null,polling:false,error:'',eventOrder:'',eventEquipment:'',eventPage:0};
function stopSimPolling(){clearTimeout(simView.timer);simView.timer=null}
function simStatus(status){return badge(status,simStateNames[status]||status)}
async function loadSim(){
  if(simView.loading)return;
  simView.loading=true;simView.error='';
  try{
    const [runs,snapshot]=await Promise.all([api('/simulation/runs'),api('/simulation/preview','POST',{source:'demo'})]);
    simView.runs=runs;simView.snapshot=snapshot;simView.loaded=true;
  }catch(e){simView.error=e.message}
  finally{simView.loading=false;if(page==='simulation')render()}
}
function simHistory(){
  return `<details class="panel sim-history" open><summary>实验历史 · ${simView.runs.length} 次</summary>${simView.runs.length?`<div class="table-wrap"><table><thead><tr><th>运行编号 / 时间</th><th>来源</th><th>工单</th><th>状态</th><th>操作</th></tr></thead><tbody>${simView.runs.map(r=>`<tr><td><span class="sim-run-id">${esc(r.run_id)}</span><span class="sub">${esc(time(r.created_at))}</span></td><td>${{demo:'教学案例',work_orders:'工单副本',replay:'复制实验'}[r.source]}</td><td>${r.completed_orders} / ${r.total_orders}</td><td>${simStatus(r.status)}</td><td><div class="actions"><button class="small" data-sim="open" data-id="${r.run_id}">查看结果</button><button class="small" data-sim="copy" data-id="${r.run_id}">复制参数</button></div></td></tr>`).join('')}</tbody></table></div>`:empty('还没有实验。先运行默认案例，观察两张订单的设备排队。')}</details>`;
}
function simEditor(w){
  const eligible=w.operations.filter(op=>op.kind==='inspection'&&w.operations.some(t=>t.kind!=='inspection'&&t.sequence<op.sequence));
  const failure=w.failure,source=w.operations.find(o=>o.operation_id===failure?.inspection_operation_id);
  const targets=source?w.operations.filter(o=>o.kind!=='inspection'&&o.sequence<source.sequence):[];
  return `<article class="sim-order-editor" data-wo="${esc(w.work_order_id)}"><h3>${esc(w.product_name)}</h3><div class="muted sim-run-id">${esc(w.work_order_id)}</div><div class="form-grid"><label>本批数量<input class="sim-quantity" type="number" min="1" step="1" required value="${w.quantity}"></label><label>指定一次质检失败<select class="sim-failure-source" aria-label="${esc(w.product_name)}失败工序"><option value="">全部合格</option>${eligible.map(op=>`<option value="${esc(op.operation_id)}" ${failure?.inspection_operation_id===op.operation_id?'selected':''}>${op.sequence}. ${esc(op.name)}</option>`).join('')}</select></label></div><div class="sim-minutes">${w.operations.map(op=>`<label>${op.sequence}. ${esc(op.name)} · 分钟／件<input class="sim-minutes-input" data-op="${esc(op.operation_id)}" type="number" min="1" max="10000" step="1" required value="${op.minutes}"><small class="muted">${esc(simView.snapshot.equipment.find(e=>e.equipment_id===op.equipment_id)?.name||op.equipment_id)}</small></label>`).join('')}</div>${failure?`<div class="form-grid"><label>本次不合格数量<input class="sim-rejected" type="number" min="1" max="${w.quantity}" step="1" required value="${failure.quantity}"></label><label>返回哪道工序<select class="sim-failure-target">${targets.map(op=>`<option value="${esc(op.operation_id)}" ${failure.target_operation_id===op.operation_id?'selected':''}>${op.sequence}. ${esc(op.name)}</option>`).join('')}</select></label></div><p class="footnote">只在此工序的首次质检注入失败；返修后复检固定通过。原工单不受影响。</p>`:''}</article>`;
}
function captureSimEdits(){
  if(!simView.snapshot)return;
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
    quantity:w.quantity,minutes:Object.fromEntries(w.operations.map(o=>[o.operation_id,o.minutes])),failure:w.failure
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
  if($('#sim-event-order'))$('#sim-event-order').onchange=()=>{simView.eventOrder=$('#sim-event-order').value;simView.eventPage=0;render()};
  if($('#sim-event-equipment'))$('#sim-event-equipment').onchange=()=>{simView.eventEquipment=$('#sim-event-equipment').value;simView.eventPage=0;render()};
  if(simView.result?.status==='running')scheduleSimPoll();
}
function simConfig(){
  const existing=state.workorders.filter(w=>w.status!=='cancelled');
  return `<section class="panel"><h2>配置实验</h2>${simView.base.source==='replay'&&simView.snapshot?`<p class="guide">正在复制已保存的输入快照，可直接重跑或调整参数。原实验和来源工单保持不变。</p>`:''}<div class="sim-source"><label>输入来源<select id="sim-source"><option value="demo" ${simView.source==='demo'?'selected':''}>默认教学案例 · 两张订单</option><option value="work_orders" ${simView.source==='work_orders'?'selected':''}>选择现有 MES 工单</option></select></label><button data-sim="load">载入输入配置</button></div>${simView.source==='work_orders'?`<div class="sim-selection">${existing.map(w=>`<label><input type="checkbox" class="sim-select-wo" value="${esc(w.work_order_id)}" ${simView.base.work_order_ids?.includes(w.work_order_id)?'checked':''}><span>${esc(w.product_name)} · ${w.quantity} 件 · ${esc(labels[w.status])}<br><span class="sim-run-id">${esc(w.work_order_id)}</span></span></label>`).join('')||empty('暂无可选工单。可以先运行默认案例。')}</div><p class="footnote">最多选择 50 张。已完工工单也可复制；每道工序需已分配设备。</p>`:''}${simView.snapshot?`<form id="sim-config-form"><p class="muted">每件时长 × 本批数量 = 该工序占用设备的时间。所有工单在 t=0 进入系统。</p>${simView.snapshot.orders.map(simEditor).join('')}<div class="actions"><button class="primary" id="sim-start" type="submit">开始仿真</button><span class="muted">计算使用虚拟时间，无需按生产分钟等待。</span></div></form>`:empty('选择来源后点击“载入输入配置”。')}</section>`;
}
function simChart(result){
  const total=result.makespan_min;
  const woLabels=Object.fromEntries(result.orders.map((w,i)=>[w.work_order_id,`订单 ${i+1}`]));
  return `<section class="panel"><h2>设备时间图</h2><div class="sim-legend"><span style="--color:#268a77">正常加工</span><span style="--color:#b27125">返修</span><span style="--color:#6e67b0">复检</span></div><p class="muted">横轴为虚拟分钟；空白表示设备空闲。指向色块查看工单与起止时间，明细见下表。</p><div class="sim-chart-scroll"><div class="sim-chart"><div></div><div class="sim-axis">${Array.from({length:6},(_,i)=>`<span>${Number((total*i/5).toFixed(1))}</span>`).join('')}</div>${result.equipment.map(eq=>`<div class="sim-chart-label">${esc(eq.name)}<br>${eq.utilization}% 利用率</div><div class="sim-chart-track">${result.intervals.filter(i=>i.equipment_id===eq.equipment_id).map(i=>`<div class="sim-block ${i.phase}" style="left:${100*i.start_min/total}%;width:${100*(i.end_min-i.start_min)/total}%" title="${esc(woLabels[i.work_order_id])} · ${esc(i.name)} · ${simPhaseNames[i.phase]} · ${i.start_min}—${i.end_min} 分钟"><span>${esc(woLabels[i.work_order_id])}</span><br>${i.start_min}—${i.end_min}</div>`).join('')}</div>`).join('')}</div></div><details><summary>加工区间明细 · ${result.intervals.length} 段</summary><div class="table-wrap"><table><thead><tr><th>工单</th><th>设备 / 工序</th><th>阶段</th><th>起止分钟</th><th>数量</th></tr></thead><tbody>${result.intervals.map(i=>`<tr><td>${esc(woLabels[i.work_order_id])}</td><td>${esc(result.equipment.find(e=>e.equipment_id===i.equipment_id)?.name)} / ${esc(i.name)}</td><td>${simPhaseNames[i.phase]}</td><td>${i.start_min}—${i.end_min}</td><td>${i.quantity}</td></tr>`).join('')}</tbody></table></div></details></section>`;
}
function simResult(r){
  const summary=r.summary;
  const links=`<a href="/simulation/runs/${r.run_id}/export?format=json">完整 JSON</a><a href="/simulation/runs/${r.run_id}/export?format=events_csv">事件 CSV</a>${summary?`<a href="/simulation/runs/${r.run_id}/export?format=summary_csv">汇总 CSV</a>`:''}`;
  let html=`<section class="panel"><div class="panel-head"><h2>实验结果</h2>${simStatus(r.status)}</div><p class="sim-run-id">${r.run_id}</p><p class="muted">${esc(time(r.created_at))} · ${r.total_orders} 张工单 · 独立数据库副本</p><div class="actions"><button data-sim="copy" data-id="${r.run_id}">复制参数重新运行</button>${links}</div></section>`;
  if(r.status==='running')html+=`<div class="sim-pending"><strong>正在计算生产过程…</strong><p>已完成 ${r.completed_orders} / ${r.total_orders} 张工单，当前虚拟时间 ${r.sim_time_min} 分钟。</p><p>页面会自动更新；离开页面不会取消后台计算。</p></div>`;
  if(r.error)html+=`<div class="error">本次实验未完成：${esc(r.error)}。已保留输入和已产生的事件。</div>`;
  if(summary){
    html+=`<div class="metrics">${[['总生产时间',summary.makespan_min+' 分钟'],['完工数量',summary.completed_quantity+' 件'],['累计等待',summary.total_waiting_min+' 分钟'],['返修数量',summary.rework_quantity+' 件']].map(([label,value])=>`<div class="metric"><small>${label}</small><strong class="value" style="font-size:25px">${value}</strong></div>`).join('')}</div>`;
    html+=`<section class="panel"><h2>订单结果与完工反馈</h2><div class="table-wrap"><table><thead><tr><th>工单 / 产品</th><th>数量</th><th>完成时刻</th><th>等待时间</th><th>返修数量</th><th>副本 MES / ERP</th></tr></thead><tbody>${summary.orders.map((w,i)=>`<tr><td>订单 ${i+1} · ${esc(w.product_name)}<span class="sub sim-run-id">${esc(w.work_order_id)}</span></td><td>${w.quantity}</td><td>${w.completed_min} 分钟</td><td>${w.waiting_min} 分钟</td><td>${w.rework_quantity}</td><td>${esc(labels[w.mes_status])} / ${esc(labels[w.erp_status])}</td></tr>`).join('')}</tbody></table></div><p class="footnote">等待时间累计正常加工、返修与复检的设备排队。利用率按设备占用时间 ÷ 本次总生产时间计算。</p></section>`+simChart(summary);
  }
  return html+simEvents(r)+`<details class="panel"><summary>本次输入快照与来源</summary><p class="muted">编号保留在本批次独立命名空间内；不会把仿真结果写回原工单。</p>${r.input.orders.map(w=>`<p><strong>${esc(w.product_name)}</strong> · ${w.quantity} 件 · ${esc(w.work_order_id)}<br>${w.operations.map(o=>`${esc(o.name)}（${o.minutes} 分钟／件）`).join(' → ')}<br>${w.failure?`指定失败 ${w.failure.quantity} 件，复检固定通过`:'全部合格'}</p>`).join('')}</details>`;
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
    if(action==='new'){simView.result=null;simView.source='demo';simView.base={source:'demo'};simView.snapshot=await api('/simulation/preview','POST',simView.base)}
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
      simView.base=base;simView.snapshot=snapshot;simView.source=snapshot.source;simView.result=null;
    }
    if(action==='prev')simView.eventPage=Math.max(0,simView.eventPage-1);
    if(action==='next')simView.eventPage++;
    if(page==='simulation'){render();if(['new','copy','load'].includes(action))content.scrollIntoView({block:'start'})}
  }catch(e){simView.error=e.message;if(page==='simulation')render()}
  finally{b.disabled=false}
});
