"""Embedded, offline presentation assets for evidence-first diagnostic reports."""
SIDEBAR = '''<button id="menuToggle" class="mobile-menu" aria-controls="reportSidebar" aria-expanded="false">☰ 报告目录</button>
<aside id="reportSidebar" class="report-sidebar"><div class="sidebar-brand">轨迹诊断<span>证据 · 评分 · 复核</span></div><p class="sidebar-label">阅读目录</p><nav aria-label="报告目录"><a href="#evidenceOverview">概览与六维入口</a><a href="#candidates">证据链与评分理由</a><a href="#auditPanel">逐步画面与动作</a><a href="#overview">分数汇总与雷达图</a><a href="#subabilities">本次评分细则</a><a href="#coverage">覆盖与遗漏检查</a><a href="#human">人工复核与导出</a></nav><p class="sidebar-label">按完整能力维度查看</p><div id="dimensionNav"></div><div class="sidebar-footer">图片与日志均可追溯<br>未评分不代表能力为零</div></aside>'''

CSS = '''
:root{--ink:#172e43;--muted:#64788a;--line:#dde6ee;--brand:#176881;--soft:#edf6f8;background:#f3f6fa;color:var(--ink)}
*{box-sizing:border-box}body{margin:0}main{max-width:1520px;margin:0 0 0 248px;padding:34px 38px 70px;min-width:0}
h1{font-size:28px;letter-spacing:-.5px;margin:0 0 10px}h2{font-size:21px;margin:0 0 14px}h3{line-height:1.5}p{line-height:1.75}button,a,select,input,summary{outline-offset:4px}button{transition:background .15s}button:focus-visible,a:focus-visible,summary:focus-visible{outline:3px solid #72b5cd}section{border:1px solid var(--line);box-shadow:0 3px 14px #18374d04;padding:24px;margin:22px 0;scroll-margin-top:20px}.notice{background:#fffbef}.mini{color:var(--muted)}
.report-sidebar{width:248px;position:fixed;inset:0 auto 0 0;overflow-y:auto;background:#fff;border-right:1px solid var(--line);padding:28px 16px;z-index:30}.sidebar-brand{padding:0 12px 22px;font-size:23px;font-weight:750;color:#16475f}.sidebar-brand span{display:block;font-size:12px;font-weight:400;color:var(--muted);margin-top:9px}.sidebar-label{font-size:12px;color:#7b8c9b;margin:20px 12px 8px}.report-sidebar nav{position:static;display:flex;flex-direction:column;gap:4px;padding:0;background:none}.report-sidebar nav a{text-decoration:none;font-size:14px;padding:11px 12px;border-radius:8px;color:#42576a}.report-sidebar nav a:hover,.report-sidebar nav a.active{background:var(--soft);color:var(--brand);font-weight:650}.dimension-link{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;border:0;background:transparent;padding:11px 12px;color:#42576a;text-align:left;font-size:14px}.dimension-link.active{background:var(--soft);font-weight:650;color:var(--brand)}.nav-count{font-size:11px;background:#eef2f6;color:#697d90;border-radius:5px;padding:2px 6px}.sidebar-footer{border-top:1px solid var(--line);margin:24px 12px 0;padding-top:18px;color:#7b8c9b;font-size:12px;line-height:1.8}.mobile-menu{display:none}
.report-header{margin-bottom:24px}.report-header .subtitle{color:var(--muted);font-size:13px}.version-note{padding:10px 14px;border:1px solid #e6ddc3;border-radius:9px;background:#fffaf0;font-size:13px;color:#71623b;overflow-wrap:anywhere}.version-note a{color:inherit}
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0}.stat-tile{border:1px solid var(--line);border-radius:10px;padding:16px;background:#f9fbfd}.stat-tile strong{font-size:30px;display:block;line-height:1.3}.stat-tile span{font-size:13px;color:var(--muted)}.dimension-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:20px 0}.dimension-card{padding:18px;text-align:left;border:1px solid var(--line);background:#fff;border-radius:10px;color:var(--ink);min-width:0}.dimension-card:hover{border-color:#78a9bc;background:#f4fafb}.dimension-card h3{font-size:15px;margin:0 0 12px}.dimension-card b{font-size:22px;color:var(--brand)}.dimension-card p{font-size:12px;color:var(--muted);margin:8px 0 0}.index-details{border-top:1px solid var(--line);padding-top:16px;overflow:auto}.index-details summary{font-size:13px;color:var(--brand)}
.evidence-toolbar{background:#f7f9fc;border:1px solid var(--line);padding:14px;border-radius:10px;margin-bottom:16px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}.evidence-toolbar select{max-width:100%;font-size:13px}.evidence-toolbar input{flex:1;min-width:180px;font-size:14px}.evidence-toolbar label{font-size:12px;color:var(--muted)}.result-count{font-size:13px;color:var(--muted);margin:12px 0}.candidate-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;align-items:start}.candidate{border:1px solid var(--line);border-radius:12px;border-top:3px solid #6c9bac;padding:20px;margin:0;min-width:0;box-shadow:0 3px 12px #18374d04;scroll-margin-top:20px}.candidate.expanded{grid-column:1/-1}.candidate .candidate-heading{display:flex;gap:14px;align-items:flex-start;justify-content:space-between}.candidate .candidate-heading h3{font-size:15px;margin:0;flex:1}.candidate .candidate-heading .right{margin:0;flex-shrink:0;padding:8px 10px;border-radius:8px;background:#eaf4ef;color:#276248;font-size:15px}.candidate[data-counted="false"] .candidate-heading .right{background:#f0f3f7;color:#667787;font-size:13px}.candidate[data-counted="false"]{border-top-color:#b1bfcb}.candidate>.candidate-range{font-size:12px;color:var(--muted);margin:10px 0}.candidate-range .pill{display:block;width:fit-content;margin-top:7px;font-size:11px}.card-reading-label{color:var(--brand);font-size:11px;font-weight:650;letter-spacing:1px;margin:16px 0 5px}.compact-reason{font-size:14px;margin:0;line-height:1.8}.compact-rule{font-size:12px;color:var(--muted);padding:10px 12px;background:#f6f8fb;border-radius:7px;margin:12px 0}.fact-preview{border-left:2px solid #cadde5;padding-left:11px;margin:14px 0}.fact-preview p{font-size:12px;color:#4d6579;margin:6px 0}.card-detail{border-top:1px solid var(--line);padding-top:12px;margin-top:16px}.card-detail>summary{font-size:13px;color:var(--brand);font-weight:650;padding:4px 0}.card-detail[open]>summary{margin-bottom:20px}.candidate-empty{padding:36px;border:1px dashed #b9cbd8;border-radius:10px;text-align:center;color:var(--muted);grid-column:1/-1}.chain-timeline li{border-radius:0 8px 8px 0}.visual-evidence figure{margin:0;border:1px solid var(--line);padding:12px;border-radius:9px}.visual-evidence img{max-height:680px;object-fit:contain;background:#f8fafc}.images img{background:#f6f8fb}.score-rule{font-size:13px}.card details>summary{line-height:1.6}.report-header p{margin:8px 0}.source-note{font-size:12px;color:var(--muted)}.grid3{grid-template-columns:repeat(3,minmax(0,1fr))}table{overflow-wrap:anywhere}.images a{overflow-wrap:anywhere}#evidenceOverview>p,#candidates>p{font-size:13px;color:var(--muted)}
@media(min-width:1800px){main{margin-left:calc(248px + (100vw - 1800px)/2)}}
@media(max-width:1150px){main{padding:28px 24px}.dimension-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.candidate-grid{grid-template-columns:1fr}.grid3{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:900px){.report-sidebar{transform:translateX(-100%);box-shadow:5px 0 20px #18374d20}.report-sidebar.visible{transform:translateX(0)}.mobile-menu{display:block;position:fixed;top:12px;left:12px;z-index:40;background:#fff;box-shadow:0 2px 10px #18374d15;font-size:13px}main{margin:0;padding:66px 14px 35px}section,.candidate{scroll-margin-top:62px}.report-sidebar{padding-top:64px}section{padding:17px}.stat-grid{grid-template-columns:repeat(2,1fr)}.grid,.grid3{grid-template-columns:1fr}.score-rule td:first-child{min-width:70px}.dimension-card{padding:14px}.candidate{padding:16px}.evidence-toolbar input{width:100%;flex-basis:100%}h1{font-size:24px}.candidate-heading{flex-wrap:wrap}.report-sidebar nav a{padding:9px 12px}}
'''

JS = '''
function decorateEvidenceCards(rows){
 for(const r of rows){
  const card=$('cand-'+r.id),x=reading.records[r.id];if(!card)continue;
  card.dataset.counted=String(x.counted);
  const heading=card.children[0],range=card.children[1];heading.classList.add('candidate-heading');range.classList.add('candidate-range');
  const body=document.createElement('details');body.className='card-detail';
  const toggle=document.createElement('summary');toggle.textContent='展开完整证据、图片与评分细则';body.append(toggle);
  while(card.children.length>2)body.append(card.children[2]);
  const title=document.createElement('p');title.className='card-reading-label';title.textContent=x.counted?'为什么给这个分':'为什么没有计分';card.append(title);
  const reason=document.createElement('p');reason.className='compact-reason';reason.textContent=x.reason;card.append(reason);
  const rule=document.createElement('p');rule.className='compact-rule';rule.textContent=x.counted?'对应标准：'+x.criterion:x.outcome;card.append(rule);
  const facts=x.timeline.filter(f=>!f.kind.startsWith('模型'));const selected=[...new Set([facts[0],facts[Math.floor(facts.length/2)],facts[facts.length-1]])].filter(Boolean);
  const preview=document.createElement('div');preview.className='fact-preview';
  for(const f of selected){const p=document.createElement('p');p.textContent='第 '+f.decision+' 步 · '+f.title;preview.append(p);}card.append(preview);
  body.addEventListener('toggle',()=>card.classList.toggle('expanded',body.open));card.append(body);
 }
 if(!rows.length)$('candidateList').innerHTML='<div class="candidate-empty">没有符合当前筛选的记录。可清除筛选，或从左侧选择其他维度。</div>';
}
function clearEvidenceFilters(){
 $('dimensionFilter').value='';$('capFilter').value='';$('statusFilter').value='';$('evidenceSearch').value='';$('sourceFilter').value='model_review';
 document.querySelectorAll('.dimension-link').forEach(b=>b.classList.remove('active'));
}
function selectDimension(id){
 clearEvidenceFilters();$('dimensionFilter').value=id;$('sourceFilter').value=id==='D5'?'deterministic_receipt':'model_review';
 document.querySelectorAll('.dimension-link').forEach(b=>b.classList.toggle('active',b.dataset.dimension===id));renderCandidates();$('candidates').scrollIntoView({block:'start'});
}
function initReportLayout(){
 const candidates=$('candidates');$('evidenceOverview').after(candidates);
 const totals=[['模型提出的候选',semantic.length],['实际计分的证据',scoredChains.length],['找到但未评分',semantic.filter(r=>r.status==='not_scored').length],['重复合并的候选',semantic.filter(r=>r.status==='duplicate').length]];
 $('reportStats').innerHTML=totals.map(([label,n])=>'<div class="stat-tile"><strong>'+n+'</strong><span>'+label+'</span></div>').join('');
 for(const d of summary.dimensions){
  const source=d.id==='D5'?'deterministic_receipt':'model_review',rows=records.filter(r=>r.capability.startsWith(d.id+'.')&&r.source===source),scored=rows.filter(r=>reading.records[r.id].counted).length;
  $('dimensionFilter').insertAdjacentHTML('beforeend','<option value="'+d.id+'">'+esc(d.name)+'</option>');
  $('dimensionNav').insertAdjacentHTML('beforeend','<button class="dimension-link" data-dimension="'+d.id+'">'+esc(d.name)+'<span class="nav-count">'+rows.length+'</span></button>');
  $('dimensionCards').insertAdjacentHTML('beforeend','<button class="dimension-card" data-dimension="'+d.id+'"><h3>'+esc(d.name)+'</h3><b>'+scored+'</b> 条计分 / '+rows.length+' 条记录<p>'+(source==='deterministic_receipt'?'程序操作回执':'评审模型证据链')+' · 覆盖 '+d.covered_subabilities+'/3 项子能力</p><p>参考分：'+(d.score===null?'未评分':d.score+' / 100')+' · 点击查看证据</p></button>');
 }
 $('dimensionFilter').onchange=()=>{const v=$('dimensionFilter').value;if(v)selectDimension(v);else{clearEvidenceFilters();renderCandidates();}};
 $('evidenceSearch').oninput=renderCandidates;
 $('resetFilters').onclick=()=>{clearEvidenceFilters();renderCandidates();};
 $('menuToggle').onclick=()=>{const visible=$('reportSidebar').classList.toggle('visible');$('menuToggle').setAttribute('aria-expanded',String(visible));};
 document.querySelectorAll('.report-sidebar nav a').forEach(a=>a.addEventListener('click',()=>{$('reportSidebar').classList.remove('visible');$('menuToggle').setAttribute('aria-expanded','false');}));
 const sections=[...document.querySelectorAll('main>section[id]')];
 let scrollPending=false;
 function highlightSection(){scrollPending=false;const current=sections.filter(s=>s.getBoundingClientRect().top<=120).at(-1)||sections[0];document.querySelectorAll('.report-sidebar nav a').forEach(a=>a.classList.toggle('active',a.hash==='#'+current.id));}
 addEventListener('scroll',()=>{if(!scrollPending){scrollPending=true;requestAnimationFrame(highlightSection);}}, {passive:true});highlightSection();
 document.addEventListener('click',e=>{const b=e.target.closest('[data-dimension]');if(b){selectDimension(b.dataset.dimension);$('reportSidebar').classList.remove('visible');$('menuToggle').setAttribute('aria-expanded','false');}});
 if(location.hash.startsWith('#cand-')){const r=records.find(r=>'#cand-'+r.id===location.hash);if(r){clearEvidenceFilters();$('sourceFilter').value=r.source;$('capFilter').value=r.capability;renderCandidates();const card=$('cand-'+r.id);card.querySelector('.card-detail').open=true;card.querySelector('.evidence-chain').open=true;requestAnimationFrame(()=>card.scrollIntoView());}}
}
'''
