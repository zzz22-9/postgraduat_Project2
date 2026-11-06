// Node标准库功能测试：使用临时沙盒结果，不连接正式服务，不创建正式批次。
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const fixtureDir=process.argv[2];
const normal=JSON.parse(fs.readFileSync(path.join(fixtureDir,'normal.json'),'utf8'));
const anomaly=JSON.parse(fs.readFileSync(path.join(fixtureDir,'anomaly.json'),'utf8'));
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const handlers={},requests=[];
const ctx=vm.createContext({
  esc,JSON,Number,Object,String,Array,console,setTimeout,clearTimeout,
  page:'test',state:{workorders:[{work_order_id:'TEST-WO',status:'released'}]},
  labels:{completed:'已完成',released:'待开工'},time:v=>v,
  badge:(s,t)=>`<span>${esc(t||s)}</span>`,empty:t=>`<div>${esc(t)}</div>`,heading:()=>'',
  $:()=>null,render:()=>{},
  content:{addEventListener:(event,handler)=>handlers[event]=handler,querySelectorAll:()=>[]},
  api:async(url,method='GET',body)=>{
    requests.push({url,method,body});
    if(url==='/simulation/runs')return [];
    if(url==='/erp/feed')return {dispatch_outbox:[]};
    if(url==='/simulation/preview')return normal.input;
    throw new Error('Unexpected API request: '+url);
  }
});
const run=code=>vm.runInContext(code,ctx);
vm.runInContext(fs.readFileSync(path.join(root,'web/simulation.js'),'utf8'),ctx);
ctx.normal=normal;ctx.anomaly=anomaly;
let checks=0;
function check(fn){fn();checks++}
(async()=>{
  check(()=>assert.equal(run('simImageUrl("C:\\\\folder\\\\defect_front.png")'),'/static/rendered_v1/defect_front.png'));
  check(()=>assert.equal(run('simImageUrl("javascript:alert(1)")'),''));
  check(()=>assert.equal(run('simImageUrl("C:/data/erp.db")'),''));
  run('simView.snapshot=normal.input;simView.base={source:"work_orders",work_order_ids:[normal.input.orders[0].work_order_id]}');
  for(const mode of ['normal','anomaly','sampled']){
    ctx.selectedMode=mode;run('simView.mode=selectedMode');
    const req=JSON.parse(run('JSON.stringify(simRequest())'));
    check(()=>assert.equal(req.overrides[normal.input.orders[0].work_order_id].field_protocols.mode,mode));
    check(()=>assert.equal(req.source,'work_orders'));
    check(()=>assert.equal(req.overrides[normal.input.orders[0].work_order_id].quantity,2));
  }
  const config=run('simConfig()');
  check(()=>assert.ok(config.includes('id="sim-field-mode"')&&config.includes('Normal - 12分钟基准')&&config.includes('Sampled')));
  check(()=>assert.ok(config.includes('id="sim-start"')));
  const normalHtml=run('simResult(normal)'),anomalyHtml=run('simResult(anomaly)');
  check(()=>assert.ok(normalHtml.includes('NORMAL')&&normalHtml.includes('12<span> 虚拟分钟')));
  check(()=>assert.ok(anomalyHtml.includes('DELAYED_BY_FAULT')&&anomalyHtml.includes('28<span> 虚拟分钟')&&anomalyHtml.includes('+16')));
  check(()=>assert.equal((anomalyHtml.match(/曾触发 FAULT/g)||[]).length,3));
  check(()=>assert.ok(anomalyHtml.includes('第 6 分钟故障')&&anomalyHtml.includes('第 12 分钟维修/返工恢复为 available')));
  check(()=>assert.ok(anomalyHtml.includes('螺栓 3/4')&&anomalyHtml.includes('螺栓 4/4')));
  check(()=>assert.ok(normalHtml.includes('src="/static/rendered_v1/normal_front.png"')));
  check(()=>assert.ok(anomalyHtml.includes('src="/static/rendered_v1/defect_front.png"')));
  for(const protocol of ['MQTT','OPC-UA','HTTP-Vision-Webhook','Modbus-TCP']){
    check(()=>assert.ok(anomalyHtml.includes(protocol)));
  }
  check(()=>assert.equal((anomalyHtml.match(/查看完整 raw_json/g)||[]).length,10));
  for(const address of ['mqtt://127.0.0.1:1883','factory/line1/CNC_01/command','opc.tcp://127.0.0.1:4840/freeopcua/server/','http://127.0.0.1:8000/mes/vision/webhook','modbus://192.168.10.30:502']){
    check(()=>assert.ok(anomalyHtml.includes(address)));
  }
  for(const key of ['payload_json','nodes','detections','raw_registers','response_frame_hex'])check(()=>assert.ok(anomalyHtml.includes(key)));
  ctx.erpMessage={message_id:'TEST-HTTP',protocol:'HTTP-REST',order_id:normal.input.orders[0].order_id,
    timestamp:'2026-11-07T00:00:00Z',endpoint:'http://127.0.0.1:8000/mes/dispatch',
    payload:{order_id:normal.input.orders[0].order_id,product_code:'P001',qty:2,route_steps:[]},
    http:{url:'http://127.0.0.1:8000/mes/dispatch',method:'POST',request_headers:{'Content-Type':'application/json'},status_code:201},delivery_state:'acknowledged'};
  run('simView.erpFeed=[erpMessage]');
  const five=run('simProtocolMessages(anomaly)');
  check(()=>assert.ok(five.includes('HTTP-REST')&&five.includes('Status Code: 201')&&five.includes('Request Headers')));
  check(()=>assert.ok(five.includes('http://127.0.0.1:8000/mes/dispatch')&&five.includes('route_steps')));
  check(()=>assert.equal((five.match(/查看完整 raw_json/g)||[]).length,11));
  run('simView.erpFeed=[]');
  const shuffled=structuredClone(anomaly);shuffled.protocol_messages.reverse();ctx.shuffled=shuffled;
  const stream=run('simProtocolMessages(shuffled)');
  check(()=>assert.ok(stream.indexOf('t=0</td>')<stream.indexOf('t=6</td>')));
  const malicious=structuredClone(anomaly);
  malicious.equipment_alerts[0].alarm_message='<img src=x onerror=alert(1)>';
  malicious.protocol_messages[0].payload.command='</pre><script>alert(1)</script>';
  ctx.malicious=malicious;const safe=run('simEquipmentAlerts(malicious)+simProtocolMessages(malicious)');
  check(()=>assert.ok(!safe.includes('<img src=x')&&!safe.includes('<script>alert(1)')&&safe.includes('&lt;script&gt;')));
  ctx.legacy={...normal,summary:{...normal.summary}};
  delete ctx.legacy.equipment_alerts;delete ctx.legacy.delay_summary;delete ctx.legacy.protocol_messages;
  delete ctx.legacy.summary.equipment_alerts;delete ctx.legacy.summary.delay_summary;delete ctx.legacy.summary.protocol_messages;
  check(()=>assert.ok(run('simResult(legacy)').includes('早期历史批次')));
  await run('loadSim()');
  check(()=>assert.ok(requests.some(r=>r.url==='/simulation/preview'&&r.body.source==='work_orders')));
  check(()=>assert.ok(!requests.some(r=>r.url==='/simulation/runs'&&r.method==='POST')));
  await handlers.click({target:{closest:()=>({disabled:false,dataset:{sim:'new'}})}});
  check(()=>assert.equal(run('simView.mode'),'normal'));
  check(()=>assert.ok(!requests.some(r=>r.url==='/simulation/runs'&&r.method==='POST')));
  const index=fs.readFileSync(path.join(root,'web/index.html'),'utf8');
  const styles=[...index.matchAll(/<style>([\s\S]*?)<\/style>/g)].map(m=>m[1]).join('\n')+'\n'+fs.readFileSync(path.join(root,'web/simulation.css'),'utf8');
  for(const [mode,html] of [['normal',normalHtml],['anomaly',anomalyHtml]]){
    fs.writeFileSync(path.join(fixtureDir,`ui_${mode}.html`),`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>测试沙盒 · ${mode}</title><style>${styles}</style><body><main><h1>测试沙盒预览 · ${mode}</h1><p>只读取自动化测试数据；未创建正式仿真批次。</p>${html}</main></body></html>`);
  }
  console.log(`PASS: ${checks} frontend assertions; normal/anomaly HTML rendered; no formal run request.`);
})().catch(error=>{console.error(error);process.exitCode=1});
