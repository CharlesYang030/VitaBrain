"""Portable human-audit report. All text embedded, original images in sibling folder."""
import html
import json
import math
from pathlib import Path

from agentlab.web.replay import atomic_write
from agentlab.diagnostic_explanation import build_explanations
from agentlab.diagnostic_report_layout import CSS, SIDEBAR, JS


def radar(dimensions):
    cx,cy,r=310,230,145
    def point(i,value):
        angle=-math.pi/2+i*math.pi/3
        return cx+r*value*math.cos(angle),cy+r*value*math.sin(angle)
    parts=['<svg viewBox="0 0 620 480" role="img" aria-label="六维行为诊断雷达，缺失值留空">']
    for fraction in (.25,.5,.75,1):
        points=' '.join(f'{x:.1f},{y:.1f}' for x,y in [point(i,fraction) for i in range(6)])
        parts.append(f'<polygon points="{points}" fill="none" stroke="#cbd5e1"/>')
    for i,d in enumerate(dimensions):
        x,y=point(i,1);lx,ly=point(i,1.24)
        parts.append(f'<line x1="{cx}" y1="{cy}" x2="{x}" y2="{y}" stroke="#cbd5e1"/>')
        name=html.escape(d['name'])
        val='未评分' if d['score'] is None else str(d['score'])+' 分'
        parts.append(f'<text x="{lx}" y="{ly}" text-anchor="middle" font-size="13">{name}</text><text x="{lx}" y="{ly+18}" text-anchor="middle" font-size="12">{val} · 覆盖{d["covered_subabilities"]}/3 项 · {d["n"]}个判定点</text>')
        if d['score'] is not None:
            px,py=point(i,d['score']/100)
            parts.append(f'<line x1="{cx}" y1="{cy}" x2="{px}" y2="{py}" stroke="#b45309" stroke-width="3"/><circle cx="{px}" cy="{py}" r="5" fill="#b45309"/>')
    if all(d['score'] is not None for d in dimensions):
        points=' '.join(f'{x:.1f},{y:.1f}' for x,y in [point(i,d['score']/100) for i,d in enumerate(dimensions)])
        parts.append(f'<polygon points="{points}" fill="#b4530915" stroke="#b45309"/>')
    parts.append('</svg>')
    return ''.join(parts)


def write_report(data,summary,out_dir):
    out_dir=Path(out_dir)
    # Include full actual model TEXT inputs for local-file viewing without fetch;
    # images remain lossless original files, loaded only for the selected step.
    context_texts={c['file']:json.loads((out_dir/c['file']).read_text()) for c in data['contexts']}
    payload={'data':data,'summary':summary,'context_texts':context_texts,
             'reading':build_explanations(data,summary)}
    embedded=json.dumps(payload,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    page=TEMPLATE.replace('@@LAYOUT_CSS@@',CSS).replace('@@SIDEBAR@@',SIDEBAR).replace('@@LAYOUT_JS@@',JS).replace('@@RUN@@',html.escape(data['run_id'])).replace('@@RADAR@@',radar(summary['dimensions'])).replace('@@DATA@@',embedded).replace('@@SCALE@@',html.escape(str(data['rubric'].get('version','旧版')))+' · 0—'+str(payload['reading']['max_score'])+'分')
    version_note=('保留历史原始评分，尚未按最新三档细则重新评审。' if data['rubric'].get('version')=='behavior_diagnostic_pilot_v1' else '评分按本次固定量表计算；布局调整不改变已有分数。')
    page=page.replace('@@VERSION_NOTE@@',version_note)
    atomic_write(out_dir/'report.html',page.encode())
    # Standalone vector can be exported independently of the report.
    atomic_write(out_dir/'radar.svg',radar(summary['dimensions']).replace('<svg ','<svg xmlns="http://www.w3.org/2000/svg" ',1).encode())


TEMPLATE='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>六维诊断审查 · @@RUN@@</title>
<style>:root{font-family:system-ui;color:#182438;background:#eef2f6}body{margin:0}main{max-width:1260px;margin:auto;padding:24px}h1{font-size:26px}h2{font-size:20px}h3{font-size:16px}p{line-height:1.65}section,.card{background:white;border:1px solid #d4dce5;border-radius:12px;padding:18px;margin:16px 0}.notice{background:#fffbeb;border-color:#f4ce78}.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.mini{font-size:12px;color:#526277}button,select,input,textarea{font:inherit;border:1px solid #b8c4d3;border-radius:6px;padding:7px;background:white}button{cursor:pointer;color:#075985}button:hover{background:#e0f2fe}nav{position:sticky;top:0;background:#eef2f6f5;z-index:5;padding:10px;display:flex;gap:10px;flex-wrap:wrap}nav a{color:#075985}table{border-collapse:collapse;width:100%;font-size:13px}td,th{text-align:left;border-bottom:1px solid #e3e8ef;padding:8px;vertical-align:top}.pill{display:inline-block;border-radius:5px;padding:3px 7px;background:#e2e8f0;font-size:12px}.bad{background:#fee2e2}.warn{background:#fef3c7}.good{background:#dcfce7}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;max-height:450px;overflow:auto;background:#f8fafc;padding:12px}.stepgrid{display:flex;flex-wrap:wrap;gap:4px}.stepgrid button{min-width:42px;font-size:12px}.images img{width:100%;border-radius:8px}.images a{font-size:12px}.images{margin-top:10px}.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.candidate{border-left:4px solid #d3a044;scroll-margin-top:65px}.chain-timeline{padding-left:24px}.chain-timeline li{padding:12px;border-left:2px solid #cbd5e1;margin:12px 0;background:#f8fafc}.score-rule td:first-child{min-width:170px}.subscore{font-size:24px;font-weight:700}.hidden{display:none}summary{cursor:pointer}svg{width:100%}textarea{box-sizing:border-box;width:100%;min-height:60px}#auditPanel{scroll-margin-top:65px}details[open]{margin-top:10px}.right{margin-left:auto}code{overflow-wrap:anywhere}@media(max-width:850px){.grid,.grid3{grid-template-columns:1fr}main{padding:12px}table{font-size:12px}}@@LAYOUT_CSS@@</style></head>
<body>@@SIDEBAR@@<main><header class="report-header"><h1>六维行为诊断 · 证据审查报告</h1><p class="subtitle">@@RUN@@ · 从行为事实到能力归因，再到评分理由</p><p class="version-note">本报告使用：@@SCALE@@。@@VERSION_NOTE@@</p></header>
<section class="notice"><b>先核对证据，再判断分数是否合理</b><p>建议从维度卡片选一条证据链，依次看“关联哪项能力 → 实际发生了什么 → 为什么这样评分”。本报告尚未人工校准；引用核验通过不等于能力归因正确。行动执行维度来自程序动作结果，其余维度来自评审模型提出的片段。</p><details><summary>查看本次评分的已知局限与风险</summary><div id="limits"></div></details></section>
<section id="evidenceOverview"><h2>先选一个维度，查看对应证据</h2><p id="chainCounts"></p><div id="reportStats" class="stat-grid"></div><div id="dimensionCards" class="dimension-grid"></div><p class="source-note">完整维度名称始终显示。行动执行来自程序操作回执，其余维度来自评审模型提出的行为片段；计分条数及覆盖范围需一起看。</p><details id="scoreIndex" class="index-details"><summary>展开已计分证据索引表</summary><div id="chainTable"></div></details><button id="showAllChains">查看全部模型候选</button></section>
<section id="overview"><h2>证据链如何汇总成维度分数</h2><div id="basic"></div><div class="grid"><div>@@RADAR@@</div><div><h3>评分怎么来的</h3><p id="scoreScale"></p><div id="dimensionCalculations"></div><p><b>如何理解：</b>例如83.33分表示这些已计分记录获得了满分的83.33%，不是83.33%的通关概率，也不表示掌握了83.33%的能力。必须同时看证据条数与子能力覆盖；没有证据的子能力仍未评分。</p><p>黄色数值是未校准的试评分。完整行为片段与单次执行请求的统计单位不同，不能直接将六条轴当作同一精度的心理能力测量。</p><div id="reviewCost"></div><details><summary>运行配置、维护与模型切换</summary><pre id="condition"></pre></details></div></div></section>
<section id="subabilities"><h2>十八项子能力与固定评分细则</h2><div id="subGrid" class="grid3"></div></section>
<section id="candidates"><h2>证据链与评分理由</h2><p>卡片直接展示对应能力、步骤、评分与理由；展开后可按时间核对事实、原图和具体评分规则。</p><div class="evidence-toolbar"><select id="dimensionFilter" aria-label="能力维度"><option value="">全部维度</option></select><select id="sourceFilter"><option value="model_review">评审模型找到的证据链</option><option value="deterministic_receipt">程序统计的动作结果</option><option value="">两类全部显示</option></select><select id="capFilter"><option value="">全部能力</option></select><select id="statusFilter"><option value="">全部状态</option><option value="counted">实际计分</option><option value="uncounted">未计入分数（全部原因）</option><option value="evidence_checked_semantics_unverified">模型候选：引用核验通过</option><option value="receipt_verified">程序回执</option><option value="rejected">核验失败</option><option value="not_scored">未评分</option><option value="duplicate">重复</option><option value="review_conflict">冲突</option></select><input id="evidenceSearch" type="search" aria-label="搜索证据" placeholder="搜索目标、理由、关键词或步骤号"><button id="resetFilters">清除筛选</button></div><p id="candidateCount" class="result-count"></p><div id="candidateList" class="candidate-grid"></div></section>
<section id="coverage"><h2>完整决策覆盖清单</h2><p>绿色表示该步所属局部块返回了完整结构评审；不代表该步全部行为已被正确识别。点击每一步核对原始记录，并可在下方补标遗漏。</p><div id="stepGrid" class="stepgrid"></div><h3>全部失败决策：检查是否被诊断遗漏</h3><p>每条失败都由程序完整枚举。没有候选引用不一定是漏检，也可能没有诊断机会；下表将这类情况明确暴露出来供人工复核。</p><div id="failureInventory"></div><h3>各分段对十八项的检查结果</h3><div id="jobList"></div></section>
<section id="auditPanel"><h2>查看步骤与证据</h2><div class="row"><button id="prev">上一步</button><input id="stepInput" type="number" min="1" value="1"><button id="next">下一步</button><select id="navigationStep"><option value="">跳到选点导航步骤…</option></select><span id="stepInfo"></span></div><h3>主决策模型输入</h3><div id="images" class="grid3 images"></div><div id="navigationPanel"></div><div id="stepCandidates"></div><div id="events"></div><details><summary>实际发送给待测模型的完整文本输入（含记忆）</summary><div id="contexts"></div></details></section>
<section id="human"><h2>人工复核与漏检补标</h2><p>候选卡可标记正确、错误、部分正确，并编辑分数。人工意见不会偷偷改变自动分数；保存于当前浏览器，须导出 JSON 才能交付或迁移。你还可以补充自动系统完全没有找到的片段。</p><div class="row"><label>起步 <input id="missingStart" type="number" min="1" value="1"></label><label>止步 <input id="missingEnd" type="number" min="1" value="1"></label><select id="missingCap"></select><select id="missingScore"><option value="">未评分</option></select></div><textarea id="missingReason" placeholder="遗漏了什么行为？哪些回执/步骤支持它？"></textarea><button id="addMissing">添加遗漏片段</button><div id="missingList"></div><hr><button id="exportReview">导出人工复核 JSON</button><span id="savedInfo" class="mini"></span></section>
<section><h2>复现与文件</h2><p>原始审计保持不变。本报告所有文本已内嵌，三视图从同目录 images/ 按需读取；分享请使用完整 report-bundle.zip，解压后打开 report.html。无外部图片地址，不需要 8852 或 Unity 在线。</p><p><a href="diagnosis.json">自动评分 JSON</a> · <a href="extracted.json">全量事实索引</a> · <a href="radar.svg">导出雷达 SVG</a></p></section>
<script type="application/json" id="payload">@@DATA@@</script><script>
'use strict';
const {data,summary,context_texts,reading}=JSON.parse(document.getElementById('payload').textContent),$=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ev=new Map(data.events.map(e=>[e.seq,e])),records=summary.records,storageKey='behavior-review:'+data.run_id+':'+data.extraction_key;
const maxScore=reading.max_score||2;
const visualById=new Map((data.visual_manifest||[]).map(i=>[i.id,i]));
const capById=new Map(summary.subabilities.map(c=>[c.id,c]));
function fullName(id){const c=capById.get(id);return c?c.dimension+' → '+c.name:'未知子能力';}
function chainName(id){const x=reading.records[id];return x?.number?'证据链 '+x.number+' · '+x.mapping:'动作结果 · '+(x?.mapping||'未知子能力');}
let human={run_id:data.run_id,extraction_key:data.extraction_key,records:{},missing:[]};try{let saved=localStorage.getItem(storageKey);if(!saved){for(const key of data.annotation_compatible_keys||[]){saved=localStorage.getItem('behavior-review:'+data.run_id+':'+key);if(saved)break;}}if(saved){human=JSON.parse(saved);human.extraction_key=data.extraction_key;}}catch(e){};
function persist(){try{localStorage.setItem(storageKey,JSON.stringify(human));$('savedInfo').textContent='已保存到本浏览器；请导出备份。';}catch(e){$('savedInfo').textContent='浏览器未允许本地保存，请立即导出 JSON。';}}
function label(status){return ({receipt_verified:'程序回执已确认',evidence_checked_semantics_unverified:'引用事实已核验；能力归因待人工确认',not_scored:'未评分',rejected:'核验失败，不计分',duplicate:'重复，不计分',review_conflict:'评分冲突，不计分'})[status]||status;}
$('missingScore').insertAdjacentHTML('beforeend',Array.from({length:maxScore+1},(_,i)=>'<option>'+i+'</option>').join(''));
$('scoreScale').textContent='每条证据按本报告固定量表记0—'+maxScore+'分；百分制 = 实得分总和 ÷（计分条数 × '+maxScore+'）×100。例如'+(maxScore===5?'5、4、3、2、1共15分，满分25分，换算60分。':'2、2、1、1、0共6分，满分10分，换算60分。')+'各子能力分开展示；维度合并其计分记录，证据多的子能力占比更大。未评分、重复、核验失败及冲突不进入分母。程序操作回执仅是二元完成代理，不等同于完整行为诊断。';
$('limits').innerHTML=(summary.quality_flags||[]).map(t=>'<p><b>'+esc(t)+'</b></p>').join('')+summary.limitations.map(t=>'<p class="mini">'+esc(t)+'</p>').join('');
const m=data.metrics,c=m.cost,t=m.task;
$('basic').innerHTML='<p><b>'+esc(m.condition.scenario_id)+' · '+(t.observed_success===true?'已通关':t.observed_success===false?'未通关':'待核验')+' · '+t.subtasks_done+'/'+t.subtasks_total+' 子任务 · '+m.execution.decision_steps+' 决策 · 最终 '+m.safety.final_health+' HP</b></p><p class="notice">'+m.validity.notes.map(esc).join('；')+'</p>';
$('condition').textContent=JSON.stringify({condition:m.condition,validity:m.validity},null,2);
$('reviewCost').innerHTML='<p>本次离线评审成本（独立于智能体运行）：'+summary.review_cost.calls+' 次返回用量的调用；输入 '+summary.review_cost.prompt_tokens.toLocaleString()+' / 输出 '+summary.review_cost.completion_tokens.toLocaleString()+' tokens。</p>';
const semantic=records.filter(r=>r.source==='model_review'),scoredChains=semantic.filter(r=>reading.records[r.id].counted);
$('chainCounts').textContent='评审模型提出 '+semantic.length+' 条候选，其中 '+scoredChains.length+' 条进入试评分；其余保留供你检查排除是否合理。另有 '+records.filter(r=>r.source==='deterministic_receipt'&&reading.records[r.id].counted).length+' 条程序确认的动作结果，单独展示。';
$('chainTable').innerHTML='<table><tr><th>证据链与步骤</th><th>对应维度 → 子能力</th><th>为什么给这个分</th><th>计入分数</th><th>查看</th></tr>'+scoredChains.map(r=>{const x=reading.records[r.id];return '<tr><td>证据链 '+x.number+'<br>第 '+r.start+'—'+r.end+' 步</td><td><b>'+esc(x.dimension)+'</b><br>'+esc(x.subability)+'</td><td>'+esc(x.criterion)+'</td><td>'+r.score+' / '+maxScore+' 分</td><td><button data-candidate="'+esc(r.id)+'">展开证据与评分</button></td></tr>';}).join('')+'</table>';
$('dimensionCalculations').innerHTML=summary.dimensions.map(d=>{const caps=summary.subabilities.filter(c=>c.id.startsWith(d.id+'.')),earned=caps.reduce((n,c)=>n+reading.aggregates[c.id].earned,0),possible=caps.reduce((n,c)=>n+reading.aggregates[c.id].possible,0);return '<p><b>'+esc(d.name)+'：</b>'+(possible?earned+' ÷ '+possible+' × 100 = '+d.score+' 分':'没有有效判定点，未评分')+'；覆盖 '+d.covered_subabilities+' / 3 项子能力。</p>';}).join('');
for(const cap of summary.subabilities){
 const option='<option value="'+esc(cap.id)+'">'+esc(fullName(cap.id))+'</option>';$('capFilter').insertAdjacentHTML('beforeend',option);$('missingCap').insertAdjacentHTML('beforeend',option);
 const calc=reading.aggregates[cap.id];
 $('subGrid').insertAdjacentHTML('beforeend','<div class="card"><p class="mini">'+esc(cap.dimension)+'</p><b>'+esc(cap.name)+'</b><div class="subscore">'+(cap.score===null?'未评分':cap.score+' 分')+'</div><p>'+esc(calc.formula)+'</p><p class="mini">'+esc(calc.distribution)+' · 有效 '+cap.n+' / 候选 '+cap.candidate_count+' · '+esc(cap.basis)+'</p><details><summary>对应评分规则</summary><p>什么情况可以评价：'+esc(reading.rules[cap.id].opportunity)+'</p>'+reading.rules[cap.id].scores.map((s,i)=>'<p>'+i+' 分：'+esc(s)+'</p>').join('')+'<p>不应据此评分的情况：'+esc(reading.rules[cap.id].exclude)+'</p></details><button data-filter="'+esc(cap.id)+'">查看相关证据链</button></div>');
}
function renderCandidates(){
 const cap=$('capFilter').value,status=$('statusFilter').value,source=$('sourceFilter').value,dimension=$('dimensionFilter').value,query=$('evidenceSearch').value.trim().toLowerCase();
 const rows=records.filter(r=>{
  const x=reading.records[r.id],statusMatch=!status||(status==='counted'?x.counted:status==='uncounted'?!x.counted:r.status===status);
  const searchMatch=!query||[x.mapping,x.reason,x.criterion,r.goal_key,...x.timeline.map(f=>f.text)].join(' ').toLowerCase().includes(query)||(/^\\d+$/.test(query)&&r.start<=Number(query)&&Number(query)<=r.end);
  return (!cap||r.capability===cap)&&statusMatch&&(!source||r.source===source)&&(!dimension||r.capability.startsWith(dimension+'.'))&&searchMatch;
 });$('candidateCount').textContent='当前显示 '+rows.length+' 条记录 · '+rows.filter(r=>reading.records[r.id].counted).length+' 条计分';
 $('candidateList').innerHTML=rows.map(r=>{
  const h=human.records[r.id]||{},x=reading.records[r.id],rule=reading.rules[r.capability];
  const timeline=x.timeline.map(f=>'<li><div class="row"><b>第 '+esc(f.decision??'未知')+' 步 · '+esc(f.title)+'</b><span class="pill '+(f.kind.startsWith('模型')?'warn':'good')+'">'+esc(f.kind)+'</span></div><p>'+esc(f.text)+'</p><button data-event="'+f.seq+'">查看本步画面与原始记录</button></li>').join('');
  const visual=(r.visual_evidence||[]).map(v=>{const im=visualById.get(v.image_id);return im?'<figure><figcaption><b>第 '+im.decision+' 步 · '+esc(im.kind==='actual_main_model_input'?'模型当时的三视图与地图输入':'选点通道与选择结果')+'</b><p>评审对画面的解读（待人工核查）：'+esc(v.claim)+'</p></figcaption><a href="'+esc(im.file)+'" target="_blank"><img loading="lazy" src="'+esc(im.file)+'" alt="'+esc(v.image_id)+'"></a><button data-step="'+im.decision+'">核对本步原始画面与日志</button></figure>':'<p class="bad">图片引用未找到：'+esc(v.image_id)+'</p>';}).join('');
  const rubric=rule?'<table class="score-rule"><tr><th>分数</th><th>这项子能力的具体标准</th></tr>'+rule.scores.map((v,i)=>'<tr class="'+(r.score===i?'warn':'')+'"><td>'+i+' 分'+(r.score===i?' ← 本条原始评审选择':'')+'</td><td>'+esc(v)+'</td></tr>').join('')+'</table>':'';
  return '<article class="card candidate" id="cand-'+esc(r.id)+'"><div class="row"><h3>'+esc(chainName(r.id))+'</h3><b class="right">'+(x.counted?r.score+' / '+maxScore+' 分':'本条未计分')+'</b></div><p><b>范围：第 '+esc(r.start)+'—'+esc(r.end)+' 步</b> · <span class="pill '+(r.status==='rejected'?'bad':'warn')+'">'+esc(label(r.status))+'</span></p><h3>为什么关联到“'+esc(x.subability)+'”？</h3><p>这项子能力考察：'+esc(x.opportunity)+'。</p><p><b>评审模型给出的解释：</b>'+esc(x.reason)+'</p><details class="evidence-chain"><summary>按时间展开证据链（'+x.timeline.length+' 条引用）</summary><ol class="chain-timeline">'+timeline+'</ol></details>'+(visual?'<details class="visual-evidence"><summary>展开评审实际引用的图片与解读（'+r.visual_evidence.length+' 张）</summary><div class="images grid">'+visual+'</div></details>':'')+'<h3>如何评分，为什么计入或排除？</h3><p><b>'+esc(x.outcome)+'</b></p><p>原始候选分数：'+(r.score===null?'未给数值':esc(r.score)+' / '+maxScore+' 分')+'；对应规则：'+esc(x.criterion)+'。</p>'+rubric+((r.validation||[]).length?'<p class="bad">核验未通过的原因：'+esc(r.validation.join('；'))+'</p>':'')+(r.duplicate_of?'<p>与 <button data-candidate="'+esc(r.duplicate_of)+'">'+esc(chainName(r.duplicate_of))+'</button> 合并，仅计算一次。</p>':'')+(r.format_correction?'<p class="mini">格式修正：'+esc(r.format_correction.reason)+'。原编号：'+esc(r.format_correction.original_range.join('—'))+'</p>':'')+((r.historical_premise_steps||[]).length?'<p class="mini">历史前提另见第 '+esc(r.historical_premise_steps.join('、'))+' 步；用于核对之前已知的信息，不计为本片段的新结果。</p>':'')+((r.followup_observation_steps||[]).length?'<p class="mini">另引用紧邻的第 '+esc(r.followup_observation_steps.join('、'))+' 步输入画面／状态，用于核对片段末尾动作的结果，仍需检查中间有无干预。</p>':'')+'<p><b>仍需你核对：</b>'+esc(x.uncertainty||'引用事实成立，不代表归因和评分必然正确。')+'</p><p class="mini">辅助条件：'+esc((r.assistance||[]).join('、')||'未单独记录')+'</p><details><summary>技术详情：精确断言、事件编号与原始候选</summary><pre>'+esc(JSON.stringify(r,null,2))+'</pre></details><div class="row"><label>人工判断 <select data-human="'+esc(r.id)+'" data-field="verdict">'+['未复核','正确','部分正确','错误','不确定'].map(v=>'<option '+(h.verdict===v?'selected':'')+'>'+v+'</option>').join('')+'</select></label><label>人工分数 <select data-human="'+esc(r.id)+'" data-field="score">'+['',...(rule?.allowed_scores||Array.from({length:maxScore+1},(_,i)=>i)).map(String)].map(v=>'<option value="'+v+'" '+(String(h.score??'')===v?'selected':'')+'>'+(v===''?'未评分':v+' 分')+'</option>').join('')+'</select></label></div><textarea data-human="'+esc(r.id)+'" data-field="note" placeholder="说明错误、遗漏或支持证据">'+esc(h.note||'')+'</textarea></article>';
 }).join('');
 decorateEvidenceCards(rows);
}
$('capFilter').onchange=()=>{$('dimensionFilter').value='';document.querySelectorAll('.dimension-link').forEach(b=>b.classList.remove('active'));if($('capFilter').value.startsWith('D5.'))$('sourceFilter').value='deterministic_receipt';else if($('capFilter').value)$('sourceFilter').value='model_review';renderCandidates();};
$('statusFilter').onchange=$('sourceFilter').onchange=renderCandidates;renderCandidates();
$('showAllChains').onclick=()=>{clearEvidenceFilters();renderCandidates();$('candidates').scrollIntoView();};
$('stepGrid').innerHTML=summary.coverage.map(c=>'<button data-step="'+c.decision+'" class="'+(c.chunk_complete?'good':'bad')+'" title="候选关联数 '+c.candidate_ids.length+'">'+c.decision+'</button>').join('');
$('failureInventory').innerHTML='<table><tr><th>决策</th><th>反馈</th><th>诊断引用</th><th>检查状态</th></tr>'+(summary.failure_inventory||[]).map(r=>'<tr><td><button data-event="'+r.seq+'">'+r.decision+'</button></td><td>'+esc(r.code)+'</td><td>'+esc(r.review_candidate_ids.join('、')||'—')+'</td><td>'+esc(r.coverage_note)+'</td></tr>').join('')+'</table>';
$('jobList').innerHTML=summary.reviews.map(r=>'<details><summary>'+esc(r.job_id)+' · '+esc(r.status)+' · 输入 '+esc(r.usage?.prompt_tokens??'未知')+' tokens</summary><p><a target="_blank" href="reviews/'+esc(r.job_id)+'-'+esc((r.cache_key||'').slice(0,16))+'.request.json">查看实际送审内容、固定提示词与参数</a></p><table><tr><th>子能力</th><th>机会状态</th><th>原因</th></tr>'+((r.parsed||{}).coverage||[]).map(c=>'<tr><td>'+esc(fullName(c.capability))+'</td><td>'+esc(({candidate:'提出了候选',no_opportunity:'未发现适用机会',insufficient_evidence:'证据不足'})[c.status]||c.status)+'</td><td>'+esc(c.reason)+'</td></tr>').join('')+'</table><details><summary>评审原文（可查漏评）</summary><pre>'+esc(r.content||r.error)+'</pre></details></details>').join('');
const navSteps=data.decisions.filter(d=>(d.navigation||[]).length);
$('navigationStep').insertAdjacentHTML('beforeend',navSteps.map(d=>'<option value="'+d.decision+'">第 '+d.decision+' 步 · 选点导航</option>').join(''));
function gallery(images){return '<div class="grid3 images">'+images.map(i=>'<div><a href="'+esc(i.file)+'" target="_blank">'+esc(i.view)+' · 点击原图</a><img src="'+esc(i.file)+'" alt="'+esc(i.view)+'" loading="lazy"></div>').join('')+'</div>';}
function navigationHtml(d){
 const artifacts=d.navigation||[];if(!artifacts.length)return '';
 const input=artifacts.filter(a=>a.stage==='GT_NAVIGATION_INPUT'),prepared=artifacts.filter(a=>a.stage==='GT_NAVIGATION_PREPARED'),planned=artifacts.filter(a=>a.stage==='GT_NAVIGATION_PLANNED');
 const events=d.event_seqs.map(n=>ev.get(n)),outputs=events.filter(e=>e.stage==='GT_NAVIGATION_MODEL_OUTPUT'),results=events.filter(e=>e.stage==='NAVIGATION_COMPLETED');
 let view='<section><h3>独立选点导航 · 通道图 → 模型选点 → 规划 → 执行</h3><p class="mini">以下通道图是第二次选点调用的实际图片，与上面的主决策输入分开展示。规划后的箭头和私有路径图不当作选点前输入。</p>';
 if(!input.length)view+='<p class="notice">本步在生成可选通道阶段停止，未向选点模型发送图片；没有选点调用，不补造选点结果。</p>';
 for(const a of prepared)view+='<details><summary>通道生成检查 · 事件 #'+a.seq+'</summary><pre>'+esc(JSON.stringify(a.metadata,null,2))+'</pre>'+(!input.length?gallery(a.images):'')+'</details>';
 for(const a of input)view+='<h3>1. 实际发送的左／正／右通道标注图 · #'+a.seq+'</h3>'+gallery(a.images)+'<details><summary>选点模型实际收到的图文解释和提示词</summary><pre>'+esc(JSON.stringify(context_texts[a.context_file],null,2))+'</pre></details>';
 for(const e of outputs){let selection;try{selection=JSON.parse(e.payload.content.replace(/^```(?:json)?\\s*|\\s*```$/g,''));}catch(error){selection=null;}
  view+='<h3>2. 模型选点响应 · #'+e.seq+'</h3>'+(selection?'<p><b>视图：'+esc(selection.view_id||'front')+' · 归一化坐标：('+esc(selection.x)+', '+esc(selection.y)+')</b></p><p>模型理由（自述）：'+esc(selection.reason)+'</p>':'')+'<details><summary>原始响应</summary><pre>'+esc(e.payload.content)+'</pre></details>';
 }
 for(const a of planned)view+='<h3>3. '+(a.visibility==='server_private'?'规划路径 BEV · 仅研究者，未发送给选点模型':'规划后生成的目标箭头 · 所选视图')+' · #'+a.seq+'</h3>'+gallery(a.images)+'<details><summary>坐标、距离及规划元数据</summary><pre>'+esc(JSON.stringify(a.metadata,null,2))+'</pre></details>';
 if(input.length&&!planned.length)view+='<p class="notice">没有成功规划产物；请查看模型响应与执行反馈，不生成虚构箭头或路径图。</p>';
 for(const e of results)view+='<h3>4. 执行结果 · #'+e.seq+'</h3><p><b>'+esc(e.payload.success===true?'到达成功':e.payload.success===false?'未到达':'结果待核对')+' · '+esc(e.payload.feedback_code)+'</b></p><p>'+esc(e.payload.message)+'</p><details><summary>执行回执</summary><pre>'+esc(JSON.stringify(e.payload,null,2))+'</pre></details>';
 return view+'</section>';
}
function showStep(n,seq){n=Math.max(1,Math.min(data.decisions.length,Number(n)||1));const d=data.decisions[n-1];$('stepInput').value=n;$('stepInput').max=data.decisions.length;$('stepInfo').textContent='决策 '+n+' / '+data.decisions.length+' · '+d.event_seqs.length+' 条投影事件';$('navigationStep').value=(d.navigation||[]).length?String(n):'';
 $('images').innerHTML=d.images.map(i=>'<div><a href="'+esc(i.file)+'" target="_blank">'+esc(i.view)+' · 点击查看原图</a><img src="'+esc(i.file)+'" alt="第'+n+'步 '+esc(i.view)+'" loading="lazy"></div>').join('');
 $('navigationPanel').innerHTML=navigationHtml(d);
 $('stepCandidates').innerHTML='<p>关联候选：'+records.filter(r=>r.start<=n&&n<=r.end).map(r=>'<button data-candidate="'+esc(r.id)+'">'+esc(chainName(r.id))+'</button>').join(' · ')+'</p>';
 $('events').innerHTML=d.event_seqs.map(id=>{const e=ev.get(id);return '<details id="event-'+id+'" '+(seq===id?'open':'')+'><summary>#'+id+' '+esc(e.stage)+'</summary><pre>'+esc(JSON.stringify(e.payload,null,2))+'</pre></details>';}).join('');
 $('contexts').innerHTML=d.contexts.map(f=>'<details><summary>'+esc(f)+'</summary><pre>'+esc(JSON.stringify(context_texts[f],null,2))+'</pre></details>').join('');
 $('missingStart').value=$('missingEnd').value=n;
}
$('stepInput').onchange=()=>showStep($('stepInput').value);$('prev').onclick=()=>showStep(Number($('stepInput').value)-1);$('next').onclick=()=>showStep(Number($('stepInput').value)+1);
$('navigationStep').onchange=()=>{if($('navigationStep').value){showStep($('navigationStep').value);$('navigationPanel').scrollIntoView();}};
document.addEventListener('click',e=>{const t=e.target.closest('[data-step],[data-event],[data-filter],[data-candidate]');if(!t)return;if(t.dataset.candidate){const r=records.find(r=>r.id===t.dataset.candidate);if(!r)return;clearEvidenceFilters();$('sourceFilter').value=r.source;$('capFilter').value=r.capability;$('statusFilter').value='';renderCandidates();const card=$('cand-'+r.id);card.querySelector('.card-detail').open=true;card.querySelector('.evidence-chain').open=true;card.classList.add('expanded');card.scrollIntoView({block:'start'});}else if(t.dataset.filter){clearEvidenceFilters();$('capFilter').value=t.dataset.filter;$('sourceFilter').value=t.dataset.filter.startsWith('D5.')?'deterministic_receipt':'model_review';$('statusFilter').value='';renderCandidates();$('candidates').scrollIntoView();}else{const s=t.dataset.event?ev.get(Number(t.dataset.event))?.decision:Number(t.dataset.step);showStep(s,Number(t.dataset.event));$('auditPanel').scrollIntoView();}});
document.addEventListener('change',e=>{const t=e.target;if(!t.dataset.human)return;const r=human.records[t.dataset.human]||{};r[t.dataset.field]=t.value;human.records[t.dataset.human]=r;persist();});
function missingList(){$('missingList').innerHTML=human.missing.map((r,i)=>'<p>'+esc((i+1)+'. '+fullName(r.capability)+' 步 '+r.start+'—'+r.end+'：'+r.reason)+'</p>').join('');}
$('addMissing').onclick=()=>{const start=Number($('missingStart').value),end=Number($('missingEnd').value);if(!(start>=1&&end>=start&&end<=data.decisions.length&&$('missingReason').value.trim())){alert('请填写有效步骤范围和遗漏说明');return;}human.missing.push({start,end,capability:$('missingCap').value,score:$('missingScore').value===''?null:Number($('missingScore').value),reason:$('missingReason').value});$('missingReason').value='';persist();missingList();};
$('exportReview').onclick=()=>{const blob=new Blob([JSON.stringify({...human,exported_at:new Date().toISOString()},null,2)],{type:'application/json'}),u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download=data.run_id+'-human-review.json';a.click();setTimeout(()=>URL.revokeObjectURL(u),5000);};
@@LAYOUT_JS@@
missingList();showStep(1);initReportLayout();
</script></main></body></html>'''
