"""Evidence-backed subjective trajectory review; deterministic assembly, no provider calls.

The judgment manifest is explicitly authored by a reviewer. This module verifies
references, enumerates ALL decision receipts, and calculates declared aggregates.
It does not pretend a rule-based extractor produced the subjective judgments.
"""
import hashlib
import html
import json
import math
import os
import shutil
import zipfile
from collections import Counter
from pathlib import Path
from agentlab.diagnostic_explanation import fact


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_review(review, data, previous):
    if review['run_id'] != data['run_id']:
        raise ValueError('Review and source run differ')
    expected = {c['id'] for c in previous['subabilities']}
    ids = [r['id'] for r in review['subabilities']]
    if len(ids) != len(set(ids)) or set(ids) != expected:
        raise ValueError('Review must account for all 18 unique subabilities')
    steps = {d['decision'] for d in data['decisions']}
    for r in review['subabilities']:
        if r['score'] is not None and (type(r['score']) is not int or r['score'] not in (0, 1, 2)):
            raise ValueError('Scores must be 0/1/2 or explicitly unassessed')
        for key in ('reason', 'rule', 'engineering', 'confidence', 'improvement'):
            if not r.get(key):
                raise ValueError(f'Missing explanation: {r["id"]} {key}')
        for side in ('positive', 'negative'):
            for evidence in r[side]:
                if not evidence['steps'] or not set(evidence['steps']) <= steps:
                    raise ValueError('Evidence references unknown decisions')
        if r['score'] is not None and not (r['positive'] or r['negative']):
            raise ValueError('Scored judgment requires evidence')
        if r['score'] in (0, 1) and not r['negative']:
            raise ValueError('Low/mixed judgment requires explicit counterevidence')
    if not set(review['visual_spot_checks']) <= steps:
        raise ValueError('Invalid visual review references')


def aggregate(review, previous):
    dimensions = []
    for d in previous['dimensions']:
        rows = [r for r in review['subabilities'] if r['id'].startswith(d['id'] + '.')]
        scored = [r for r in rows if r['score'] is not None]
        points = sum(r['score'] for r in scored)
        dimensions.append(dict(id=d['id'], name=d['name'], old_score=d['score'],
                               score=round(points / (2 * len(scored)) * 100, 2) if scored else None,
                               earned=points, possible=2 * len(scored), covered=len(scored),
                               formula=f'{points} ÷ {2 * len(scored)} × 100' if scored else '未考察'))
    return dimensions


def parse_public(text):
    start = text.find('{"allowed_verbs"')
    if start < 0:
        return None
    return json.JSONDecoder().raw_decode(text[start:])[0]


def input_audit(data, source):
    rows, suspicious = [], []
    tasks = Counter()
    def scan(value, path, step):
        if isinstance(value, dict):
            for k, v in value.items():
                if any(word in k.lower() for word in ('subtask', 'predicate', 'ground_truth')):
                    suspicious.append(dict(step=step, path=path + '.' + k))
                scan(v, path + '.' + k, step)
        elif isinstance(value, list):
            for v in value:
                scan(v, path + '[]', step)
    for c in data['contexts']:
        for message in json.loads((source / c['file']).read_text()):
            for text in message.get('texts', []):
                public = parse_public(text)
                if public is None:
                    continue
                tasks[public['current_observation'].get('task', '')] += 1
                scan(public, '', c['decision'])
                rows.append(dict(step=c['decision'], seq=c['seq'], file=c['file'],
                                 sha256=digest(source / c['file']),
                                 throw_allowed='throw' in public['allowed_verbs']))
    return dict(inputs=len(rows), decisions=len({r['step'] for r in rows}),
                throw_allowed_inputs=sum(r['throw_allowed'] for r in rows),
                tasks=dict(tasks), structured_task_keys=suspicious, records=rows,
                limit='检查结构化字段和公开任务，不等于证明所有自由文本都不含提示或泄漏。')


def build_ledger(data, review):
    events = {e['seq']: e for e in data['events']}
    observations = {}
    for e in data['events']:
        if e['stage'] == 'MODEL_INPUT_FACTS':
            obs = e['payload'].get('observation') or {}
            observations[obs.get('observation_id')] = {o['handle']: o['label'] for o in obs.get('interactables', []) if o.get('handle') and o.get('label')}
    result = []
    for dec in data['decisions']:
        es = [events[s] for s in dec['event_seqs']]
        receipts = [e for e in es if e['stage'] in ('NAVIGATION_COMPLETED', 'PATH_RECALL_COMPLETED') or (e['stage'] == 'ACTION_EXECUTED' and not e['payload'].get('navigation_substep'))]
        if len(receipts) != 1:
            raise ValueError(f'Decision {dec["decision"]} has ambiguous receipts')
        receipt = receipts[0]
        stage, p = receipt['stage'], receipt['payload']
        direct = stage == 'ACTION_EXECUTED'
        verb = (p.get('action') or {}).get('verb', '')
        default = 'D5.1' if not direct or verb.startswith(('move_', 'rotate_', 'look_')) else 'D5.2' if verb in ('put', 'pickup', 'fill_water', 'drop') else 'D5.3' if verb in ('open', 'close', 'toggle_on', 'toggle_off') else 'D3.3' if verb == 'ask_npc' else 'D6.2'
        refs = [r['id'] for r in review['subabilities'] if any(dec['decision'] in e['steps'] for side in ('positive', 'negative') for e in r[side])]
        parsed = [e for e in es if e['stage'] == 'RESPONSE_PARSED']
        damage = [d for d in data['metrics']['safety']['damage_receipts'] if d['decision'] == dec['decision']]
        result.append(dict(step=dec['decision'], receipt_seq=receipt['seq'], type=stage,
                           action=verb, success=p.get('success'), code=p.get('feedback_code'),
                           summary=parsed[-1]['payload'].get('decision_summary', '') if parsed else '',
                           result=fact(receipt, observations), receipt=p,
                           damage=damage, mapped_capabilities=sorted(set(refs + [default])),
                           engineering=any(e['stage']=='RUN_STATE' and any(e['payload'].get(k) for k in ('maintenance','correction','auto_error')) for e in es),
                           facts=[fact(e, observations) for e in es if e['stage'] in ('ACTION_EXECUTED','NAVIGATION_COMPLETED','PATH_RECALL_COMPLETED','TARGET_RESOLVED','RUN_STATE')],
                           images=dec['images'], navigation=dec.get('navigation', []), contexts=dec['contexts']))
    # Use the decision grouping, not the older zero-based metrics.failure_receipts.step.
    if len(result) != data['metrics']['execution']['decision_steps']:
        raise ValueError('Incomplete decision coverage')
    return result


def radar(dimensions):
    def point(i, value):
        a = -math.pi/2 + i*math.pi/3
        return 300 + math.cos(a)*140*value, 215 + math.sin(a)*140*value
    h=['<svg viewBox="0 0 600 450" role="img" aria-label="本轮受测子项综合表现雷达图">']
    for v in (.25,.5,.75,1):
        h.append('<polygon points="'+' '.join(f'{x:.1f},{y:.1f}' for x,y in [point(i,v) for i in range(6)])+'" fill="none" stroke="#d2dfe7"/>')
    for i,d in enumerate(dimensions):
        x,y=point(i,1);lx,ly=point(i,1.34)
        h.append(f'<line x1="300" y1="215" x2="{x}" y2="{y}" stroke="#d2dfe7"/><text x="{lx}" y="{ly}" text-anchor="middle" font-size="14">{html.escape(d["name"])}</text><text x="{lx}" y="{ly+19}" text-anchor="middle" font-size="12">{d["score"] if d["score"] is not None else "未考察"} · 覆盖{d["covered"]}/3</text>')
    if all(d['score'] is not None for d in dimensions):
        pts=' '.join(f'{x:.1f},{y:.1f}' for x,y in [point(i,d['score']/100) for i,d in enumerate(dimensions)])
        h.append(f'<polygon points="{pts}" fill="#197b8c33" stroke="#197b8c" stroke-width="3"/>')
    h.append('</svg>')
    return ''.join(h)


def copy_file(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def build(source, manifest, out):
    source, manifest, out = Path(source).resolve(), Path(manifest).resolve(), Path(out).resolve()
    if out == source or source in out.parents:
        raise ValueError('Use an independent output directory to preserve original scores')
    data = json.loads((source/'extracted.json').read_text())
    previous = json.loads((source/'diagnosis.json').read_text())
    review = json.loads(manifest.read_text())
    validate_review(review, data, previous)
    dimensions = aggregate(review, previous)
    ledger = build_ledger(data, review)
    audit = input_audit(data, source)
    provenance = dict(source_extracted_sha256=digest(source/'extracted.json'),
                      source_diagnosis_sha256=digest(source/'diagnosis.json'),
                      judgment_manifest_sha256=digest(manifest),
                      audit_sha256_tail=data['metrics']['source']['audit_sha256_tail'])
    names = {c['id']:dict(name=c['name'], dimension=c['dimension']) for c in previous['subabilities']}
    old_candidates = [dict(id=r['id'], capability=r['capability'], start=r['start'], end=r['end'],
                          status=r['status'], old_score=r.get('score'), reason=r.get('reason'),
                          disposition='作为整段综合判断的证据；不按候选次数重复加权',
                          new_score=next(c['score'] for c in review['subabilities'] if c['id']==r['capability']))
                      for r in previous['records'] if r['source']=='model_review']
    payload=dict(review=review, dimensions=dimensions, names=names, ledger=ledger,
                 metrics=data['metrics'], input_audit=audit, provenance=provenance,
                 old_candidates=old_candidates, visual_manifest=data.get('visual_manifest', []))
    out.mkdir(parents=True, exist_ok=True)
    for folder in ('images', 'contexts'):
        for f in (source/folder).iterdir():
            if f.is_file(): copy_file(f,out/folder/f.name)
    # Existing full evidence UI remains an exact historical drilldown.
    copy_file(source/'report.html',out/'historical-evidence.html')
    for name in ('diagnosis.json','extracted.json','radar.svg'):
        copy_file(source/name,out/name)
    for f in (source/'reviews').glob('*.json'):
        if not f.name.endswith('.request.json'): copy_file(f,out/'reviews'/f.name)
    (out/'holistic-review.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
    (out/'judgment-manifest.json').write_text(json.dumps(review,ensure_ascii=False,indent=2)+'\n')
    embedded=json.dumps(payload,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    (out/'report.html').write_text(TEMPLATE.replace('@@DATA@@',embedded).replace('@@RADAR@@',radar(dimensions)).replace('@@RUN@@',html.escape(data['run_id'])).replace('@@SCENARIO@@',html.escape(data['metrics']['condition']['scenario_id'])).replace('@@STEPS@@',str(len(ledger))).replace('@@FAILURES@@',str(sum(r['success'] is False for r in ledger))))
    (out/'holistic-radar.svg').write_text(radar(dimensions).replace('<svg ','<svg xmlns="http://www.w3.org/2000/svg" ',1))
    with zipfile.ZipFile(out/'report-bundle.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for f in sorted(out.rglob('*')):
            if f.is_file() and f.name!='report-bundle.zip':
                archive.write(f,f.relative_to(out))
    return payload

TEMPLATE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>@@SCENARIO@@ · 整段轨迹综合评分</title>
<style>
:root{font-family:system-ui,-apple-system,sans-serif;color:#183449;background:#f3f6fa;font-size:15px}*{box-sizing:border-box}body{margin:0}aside{position:fixed;inset:0 auto 0 0;width:235px;background:white;border-right:1px solid #dbe5ed;padding:30px 18px;overflow:auto}aside h2{font-size:23px;margin:0 10px 8px}aside small{display:block;margin:0 10px 26px;color:#718593}aside a{display:block;padding:11px 10px;border-radius:8px;font-size:14px;color:#405e71;text-decoration:none}aside a:hover,aside a.active{background:#eaf5f5;color:#16697a}aside .sep{font-size:12px;color:#8999a7;margin:20px 10px 8px}main{margin-left:235px;padding:34px;max-width:1700px}h1{font-size:29px;margin:0 0 10px}h2{font-size:22px}h3{font-size:17px}h4{font-size:14px;margin:0 0 8px}p,li{line-height:1.8}p{margin:10px 0}a{color:#126b89}section,.card{background:white;border:1px solid #dce6ed;border-radius:12px;padding:23px;margin:20px 0;scroll-margin-top:18px}section>h2{margin-top:0}.muted{color:#6b8292;font-size:13px}.banner{padding:13px 17px;background:#fff8e8;border:1px solid #ead8ac;border-radius:9px;font-size:13px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.stats,.dimension-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.stat,.dimension-tile{padding:18px;background:#f7fafc;border:1px solid #dce6ed;border-radius:10px}.stat strong{font-size:26px;display:block}.dimension-tile{display:block;text-decoration:none;color:inherit}.dimension-tile strong{font-size:27px;color:#19788a}.dimension-tile p{font-size:12px;color:#6b8292}.dimension-tile h3{margin-top:0}.score{font-size:27px;font-weight:750;white-space:nowrap;color:#19788a}.score.zero{color:#b4423e}.score.na{font-size:19px;color:#6d7d89}.row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.between{justify-content:space-between}.pill{display:inline-block;padding:3px 8px;border-radius:6px;background:#edf2f6;font-size:12px}.card{border-top:3px solid #6e9caa;padding:21px}.card h3{margin:0}.positive,.negative{padding:16px;border-radius:9px;background:#eff8f3}.negative{background:#fff4f0}.positive h4{color:#2b6e4a}.negative h4{color:#9f4a37}.evidence-item{border-bottom:1px solid #dce4e7;padding:9px 0;font-size:14px}.evidence-item:last-child{border:0}.step-link{display:inline-block;padding:3px 7px;margin:4px 4px 0 0;border:1px solid #c9d9e2;border-radius:5px;background:#fff;color:#276d86;cursor:pointer;font-size:12px}.explain{border-left:3px solid #6197a7;padding:10px 14px;background:#f4f8fb;margin:15px 0}.engineering{font-size:13px;color:#795a34;background:#fffaf0;border-radius:8px;padding:12px}.images{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.images img{width:100%;background:#edf3f7;border-radius:8px}.images figure{margin:0;min-width:0}.images figcaption{font-size:12px;color:#677f90;margin-bottom:8px}.images a{overflow-wrap:anywhere}svg{width:100%}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:10px;text-align:left;border-bottom:1px solid #dee6ed;vertical-align:top;line-height:1.65}.table-scroll{overflow:auto}details{margin-top:14px}summary{cursor:pointer;color:#1a6e87;line-height:1.7}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto;background:#f5f8fb;padding:12px;font-size:12px}button,input,select{font:inherit;border:1px solid #bdccd7;border-radius:6px;background:white;padding:7px;color:#295a70}button{cursor:pointer}input[type=number]{width:85px}.step-grid{display:flex;flex-wrap:wrap;gap:5px}.step-grid button{font-size:12px;min-width:39px}.failed{background:#fff0ed;color:#a73e33;border-color:#e7b8af}.has-damage{box-shadow:inset 0 -3px #b24444}.selected{outline:2px solid #23748b}.audit-panel{border-color:#7eaab9}.metric-bar{height:10px;background:#e9f0f4;border-radius:8px;overflow:hidden;margin:10px 0}.metric-bar span{display:block;height:100%;background:#398398}.footer{font-size:12px;color:#6b8292}#menu{display:none}.missing{background:#f8fafc}.dimension-section>h2{display:flex;justify-content:space-between;gap:20px}.navigation-private{border-left:3px solid #b98c46;padding-left:12px}
@media(max-width:1100px){main{padding:25px}.dimension-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.grid{grid-template-columns:1fr}}
@media(max-width:800px){aside{transform:translateX(-100%);z-index:20;padding-top:60px;width:245px;box-shadow:5px 0 20px #17344820}aside.open{transform:translateX(0)}#menu{display:block;position:fixed;top:10px;left:10px;z-index:30;background:white}main{margin:0;padding:62px 12px 25px}section,.card{padding:16px;scroll-margin-top:60px}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.images{grid-template-columns:1fr}.dimension-grid{grid-template-columns:1fr 1fr}.dimension-tile{padding:13px}.score{font-size:23px}h1{font-size:25px}.row input{max-width:100%}}
</style></head><body><button id="menu" aria-controls="sidebar" aria-expanded="false">☰ 目录</button><aside id="sidebar"><h2>整段轨迹诊断</h2><small>成功 · 失败 · 改进 · 归因</small><a href="#overview">综合结果与雷达图</a><a href="#method">评分方式与适用范围</a><div class="sep">六个完整能力维度</div><nav id="dimensionNav"></nav><div class="sep">逐条核对</div><a href="#audit">@@STEPS@@步画面与行为</a><a href="#failures">全部@@FAILURES@@次失败请求</a><a href="#dispositions">旧38条候选如何处理</a><a href="#engineering">工程影响与完整输入检查</a><a href="historical-evidence.html">历史片段评分报告 ↗</a></aside>
<main><header><h1>@@SCENARIO@@ · 整段轨迹综合评分报告</h1><p class="muted">@@RUN@@ · @@STEPS@@步 · 本轮助手主观复核，证据与算分过程公开</p><p class="banner">评价对象是当前地图、选点导航、回溯、记忆及人物接口辅助下的整段表现。运行中有模型服务切换和工程维护，不作为单一基模的正式成绩。</p></header>
<section id="overview"><h2 id="headline"></h2><div id="stats" class="stats"></div><div class="grid"><div>@@RADAR@@<p class="muted">雷达分数只汇总本轮实际评价的子项；未考察子项不当作0。图形不是完整能力认证，不给覆盖不全的六维总分。</p></div><div><h3>本次最重要的修正</h3><div id="findings"></div><p class="muted">“主观综合判断”表示证据支持下的解释，不是经过校准的心理能力测量；可直接核对、质疑和修改审查清单。</p></div></div><div id="dimensionTiles" class="dimension-grid"></div></section>
<section id="method"><h2>0—2分怎么给，百分制怎么算</h2><p>本报告新增<b>整段综合层</b>：先检查全部决策，再围绕每项子能力并列成功、反例、后续改善和工程影响。每项只给一次综合等级，重复窗口和同一事件的多个旧候选不重复加权。</p><div class="grid"><div><p><b>0分：</b>本项核心目标未有效达成；例如收到危险提示后仍反复受伤，没有形成有效避险。</p><p><b>1分：</b>部分条件或机会完成，同时存在重要未解决问题；例如记得餐桌位置，却丢失自己暂存食材的位置线索。</p><p><b>2分：</b>本项已观察的必要条件完成，反例经核查不构成该项未完成；允许正常探索和纠正，不要求零次失败。</p><p><b>未考察：</b>没有充分机会或独立证据，不硬填0，也不填满分。</p></div><div><p>每项具体条件见下方卡片；安全、恢复、导航的目标包含持续有效性，终局成功不能覆盖此前安全失败。</p><p><b>维度百分制 = 已评价子项等级之和 ÷（已评价子项数 × 2）×100。</b>三项等权，不让某项因案例特别多而压过另外两项。</p><p>例如“空间与物理认知”：空间关系2分、区域连接判断1分、物理量判断未考察，得到3÷4×100=75分，覆盖2/3。不是75%的题目通过率。</p><p>旧报告是片段加权，本报告是子项等权综合判断，两列差异是评法修正，不能当作模型性能变化。</p></div></div><details><summary>作者、数据来源与检查边界</summary><div id="provenance"></div></details></section>
<div id="dimensions"></div>
<section id="audit" class="audit-panel"><h2>逐步核对：输入画面 → 决策说明 → 实际结果</h2><p class="muted">红底是请求未成功；下红线是本步掉血。全部@@STEPS@@步均有执行结果。模型说明仅是自述，成功以回执和任务判定为准。</p><div id="stepGrid" class="step-grid"></div><div class="row" style="margin-top:18px"><button id="prev">上一步</button><input id="stepInput" type="number" min="1" max="@@STEPS@@" value="1"><button id="next">下一步</button><span id="stepLabel"></span></div><div id="stepText"></div><div id="mainImages" class="images"></div><details id="navImages"><summary>本步选点导航图：模型输入与事后规划分开展示</summary><div id="navigationImages"></div></details><details><summary>本步实际回执与引用事实</summary><div id="stepFacts"></div></details><details><summary>实际输入文本和原始回执文件</summary><div id="contextLinks"></div></details></section>
<section id="failures"><h2>全量失败请求：不遗漏、不重复计数</h2><p class="muted">采用审计中1—@@STEPS@@的决策分组。旧指标部分step字段从0开始；本页按事件编号校正显示，不改原始审计。自动导航内部动作不是额外的模型决策。</p><div class="table-scroll"><table><thead><tr><th>步骤</th><th>行为与反馈</th><th>评价入口</th><th>归因说明</th></tr></thead><tbody id="failureRows"></tbody></table></div></section>
<section id="dispositions"><h2>原来38条候选，现在怎样处理</h2><p>不删除原始结果。旧候选只作为综合证据，不按局部／全局重复次数投票；旧未计分片段也进入判断或工程说明。下列“综合分”是其所属子项的整段等级，不是把旧候选逐条重新赋分。</p><div class="table-scroll"><table><thead><tr><th>原片段</th><th>完整子能力</th><th>旧处理</th><th>所属子项综合分</th><th>核对</th></tr></thead><tbody id="oldRows"></tbody></table></div></section>
<section id="engineering"><h2>工程影响与输入检查</h2><div id="inputAudit"></div><div id="engineeringRows"></div><p id="safetyNote" class="banner"></p><p id="followupNote"></p></section>
<section><h2>下载、分享与复现</h2><p><a href="report-bundle.zip">完整离线报告包</a> · <a href="holistic-review.json">综合判断与全量证据JSON</a> · <a href="judgment-manifest.json">可修改的主观审查清单</a> · <a href="holistic-radar.svg">综合雷达图</a> · <a href="historical-evidence.html">历史片段报告</a></p><p class="footer">分享请发送整个ZIP，解压后打开report.html。图片保留为本地文件，网页不连接8852、Unity或外部模型服务。历史精确请求文件未随包分发；当前逐步输入及回执已保留。</p></section>
<script type="application/json" id="payload">@@DATA@@</script><script>
'use strict';const P=JSON.parse(document.getElementById('payload').textContent),$=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const full=id=>P.names[id].dimension+' → '+P.names[id].name,score=v=>v===null?'未考察':v+' / 2 分',stepButtons=steps=>steps.map(n=>'<button class="step-link" data-step="'+n+'">第'+n+'步</button>').join('');
const m=P.metrics,byType=m.execution.by_decision_type,firstProgress=m.task.progress_curve.find(r=>r.done>0);
$('headline').textContent=P.review.headline;$('findings').innerHTML=P.review.findings.map(f=>'<p><b>'+esc(f.title)+'：</b>'+esc(f.text)+'</p>').join('');$('safetyNote').textContent=P.review.safety_note;$('followupNote').textContent=P.review.followup_note;
$('stats').innerHTML=[['任务完成／子任务',m.task.subtasks_done+' / '+m.task.subtasks_total],['决策／失败请求',P.ledger.length+' / '+P.ledger.filter(r=>r.success===false).length],['生命值／损伤事件',m.safety.initial_health+' → '+m.safety.final_health+' / '+m.safety.damage_events+'次'],['首次子目标完成',firstProgress?'第'+firstProgress.decision+'步':'未完成'],['选点导航成功',(byType.gt_navigate?.successes||0)+' / '+(byType.gt_navigate?.attempts||0)],['历史回溯成功',(byType.return_to_visited?.successes||0)+' / '+(byType.return_to_visited?.attempts||0)]].map(([k,v])=>'<div class="stat"><span class="muted">'+k+'</span><strong>'+v+'</strong></div>').join('');
$('dimensionNav').innerHTML=P.dimensions.map(d=>'<a href="#'+d.id+'">'+esc(d.name)+'</a>').join('');
$('dimensionTiles').innerHTML=P.dimensions.map(d=>'<a class="dimension-tile" href="#'+d.id+'"><h3>'+esc(d.name)+'</h3><strong>'+d.score+'</strong> / 100<p>覆盖'+d.covered+'/3 · '+d.formula+'</p><p>历史片段汇总：'+(d.old_score===null?'未评分':d.old_score)+'；点击看正反证据</p></a>').join('');
function evidence(items){return items.length?items.map(e=>'<div class="evidence-item">'+esc(e.text)+'<div>'+stepButtons(e.steps)+'</div></div>').join(''):'<p class="muted">未发现足以支持该侧结论的独立证据；不补造案例。</p>';}
$('dimensions').innerHTML=P.dimensions.map(d=>'<section class="dimension-section" id="'+d.id+'"><h2>'+esc(d.name)+'<span class="score '+(d.score===0?'zero':'')+'">'+d.score+' / 100</span></h2><p class="muted">'+d.formula+' = '+d.score+' · '+d.covered+'/3项参与计算。该维度各子项等权；覆盖不足不代表其余子项也通过。</p>'+P.review.subabilities.filter(c=>c.id.startsWith(d.id+'.')).map(c=>'<article class="card '+(c.score===null?'missing':'')+'" id="'+c.id+'"><div class="row between"><h3>'+esc(full(c.id))+'</h3><span class="score '+(c.score===0?'zero':c.score===null?'na':'')+'">'+score(c.score)+'</span></div><p class="muted">评价目标：'+esc(c.goal)+' · 把握程度：'+esc(c.confidence)+'</p><div class="explain"><b>综合判断与给分理由</b><p>'+esc(c.reason)+'</p></div><div class="grid"><div class="positive"><h4>成功或有效调整的证据</h4>'+evidence(c.positive)+'</div><div class="negative"><h4>失败、反例或需要澄清的记录</h4>'+evidence(c.negative)+'</div></div><p><b>后来有没有改善：</b>'+esc(c.improvement)+'</p><p class="engineering"><b>工程与辅助影响：</b>'+esc(c.engineering)+'</p><details><summary>本项评分边界与历史候选</summary><p>'+esc(c.rule)+'</p>'+P.old_candidates.filter(r=>r.capability===c.id).map(r=>'<p><a href="historical-evidence.html#cand-'+esc(r.id)+'">原证据：第'+r.start+'—'+r.end+'步</a> · '+esc(r.reason)+'</p>').join('')+'</details></article>').join('')+'</section>').join('');
$('provenance').innerHTML='<p>'+esc(P.review.reviewer)+'</p><p>'+esc(P.review.distinction)+'</p>'+P.review.review_limits.map(t=>'<p>'+esc(t)+'</p>').join('')+'<pre>'+esc(JSON.stringify(P.provenance,null,2))+'</pre>';
$('stepGrid').innerHTML=P.ledger.map(r=>'<button data-step="'+r.step+'" class="'+(r.success===false?'failed ':'')+(r.damage.length?'has-damage':'')+'" title="'+esc(r.code)+'">'+r.step+'</button>').join('');
function gallery(images){return '<div class="images">'+images.map(i=>'<figure><figcaption>'+esc(i.view||i.kind||'原图')+'</figcaption><a href="'+esc(i.file)+'" target="_blank"><img loading="lazy" src="'+esc(i.file)+'" alt="'+esc(i.view||i.kind||'原图')+'"></a></figure>').join('')+'</div>';}
function showStep(n){n=Math.max(1,Math.min(P.ledger.length,Number(n)||1));const r=P.ledger[n-1];$('stepInput').value=n;$('stepLabel').textContent='第'+n+'步 · '+(r.success?'请求成功':'请求未成功')+' · 回执#'+r.receipt_seq;
 $('stepText').innerHTML='<p><b>模型决策说明（自述）：</b>'+esc(r.summary)+'</p><p><b>实际结果：</b>'+esc(r.result.text)+'</p>'+(r.damage.length?'<p class="engineering"><b>本步扣血：</b>'+r.damage.map(d=>d.lost+'点 · '+esc(d.cause)).join('；')+'</p>':'')+'<p>'+r.mapped_capabilities.map(c=>'<a href="#'+c+'">'+esc(full(c))+'</a>').join(' · ')+'</p>';
 $('mainImages').innerHTML=gallery(r.images);$('navImages').hidden=!r.navigation.length;
 $('navigationImages').innerHTML=r.navigation.map(a=>'<div class="'+(a.visibility==='server_private'?'navigation-private':'')+'"><h3>'+esc(({GT_NAVIGATION_PREPARED:'通道生成检查（后端）',GT_NAVIGATION_INPUT:'选点模型实际收到的通道图',GT_NAVIGATION_PLANNED:'选点后生成的箭头／规划图'})[a.stage]||a.stage)+'</h3><p class="muted">'+(a.visibility==='server_private'?'仅研究者可见，不能当作模型输入':'保留原始阶段标记：'+esc(a.visibility))+'</p>'+gallery(a.images)+'<details><summary>坐标与原始元数据</summary><pre>'+esc(JSON.stringify(a.metadata,null,2))+'</pre></details></div>').join('');
 $('stepFacts').innerHTML=r.facts.map(f=>'<details><summary>#'+f.seq+' · '+esc(f.title)+' · '+esc(f.kind)+'</summary><p>'+esc(f.text)+'</p></details>').join('');
 $('contextLinks').innerHTML=r.contexts.map(f=>'<p><a target="_blank" href="'+esc(f)+'">实际主决策输入 '+esc(f)+'</a></p>').join('')+'<pre>'+esc(JSON.stringify(r.receipt,null,2))+'</pre>';
 document.querySelectorAll('#stepGrid button').forEach(b=>b.classList.toggle('selected',Number(b.dataset.step)===n));
}
const engineeringCodes=new Set(['gt_navigation_query_changed_frame','recall_carry_sync_failed','no_space']);
$('failureRows').innerHTML=P.ledger.filter(r=>r.success===false).map(r=>'<tr><td>'+stepButtons([r.step])+'</td><td>'+esc(r.result.title)+'<br>'+esc(r.result.text)+'</td><td>'+r.mapped_capabilities.map(c=>'<a href="#'+c+'">'+esc(full(c))+'</a>').join('<br>')+'</td><td>'+(engineeringCodes.has(r.code)?'有已确认工程错误或反馈更正，不能直接归责模型。':r.type==='NAVIGATION_COMPLETED'?'选点与自动执行共同影响；受阻／损伤是事实，模型责任另判。':r.type==='PATH_RECALL_COMPLETED'?'动态通路拒绝是环境反馈；不等于坐标记忆错误。':'保留失败；需结合条件与后续调整，正常探测不自动算能力错误。')+'</td></tr>').join('');
const labels={not_scored:'原来未评分',duplicate:'重复合并',evidence_checked_semantics_unverified:'原来计分',rejected:'引用核验未通过'};
$('oldRows').innerHTML=P.old_candidates.map(r=>'<tr><td>第'+r.start+'—'+r.end+'步</td><td>'+esc(full(r.capability))+'</td><td>'+esc(labels[r.status]||r.status)+(r.old_score===null?'':' · '+r.old_score+'/2')+'</td><td><a href="#'+r.capability+'">'+score(r.new_score)+'</a></td><td><a href="historical-evidence.html#cand-'+esc(r.id)+'">原始理由与证据</a></td></tr>').join('');
$('inputAudit').innerHTML='<p><b>完整主决策输入检查：</b>'+P.input_audit.inputs+'份，覆盖'+P.input_audit.decisions+'步；允许throw的输入 '+P.input_audit.throw_allowed_inputs+'份；检测到结构化子任务／真值字段 '+P.input_audit.structured_task_keys.length+'处。</p><p>公开任务全文：'+esc(Object.keys(P.input_audit.tasks).join('；'))+'</p><p class="muted">'+esc(P.input_audit.limit)+'</p>';
$('engineeringRows').innerHTML=P.ledger.filter(r=>r.engineering).map(r=>'<p>'+stepButtons([r.step])+' '+r.facts.filter(f=>f.title==='运行维护或中断记录').map(f=>esc(f.text)).join('；')+'</p>').join('');
document.addEventListener('click',e=>{const b=e.target.closest('[data-step]');if(b){showStep(b.dataset.step);$('audit').scrollIntoView();}const a=e.target.closest('aside a');if(a){$('sidebar').classList.remove('open');$('menu').setAttribute('aria-expanded','false');}});
$('prev').onclick=()=>showStep(Number($('stepInput').value)-1);$('next').onclick=()=>showStep(Number($('stepInput').value)+1);$('stepInput').onchange=()=>showStep($('stepInput').value);$('menu').onclick=()=>{$('menu').setAttribute('aria-expanded',String($('sidebar').classList.toggle('open')));};
let pending=false;function updateNav(){pending=false;const sections=[...document.querySelectorAll('main section[id]')],current=sections.filter(s=>s.getBoundingClientRect().top<=130).at(-1)||sections[0];document.querySelectorAll('aside a').forEach(a=>a.classList.toggle('active',a.hash==='#'+current.id));}addEventListener('scroll',()=>{if(!pending){pending=true;requestAnimationFrame(updateNav);}},{passive:true});showStep(1);updateNav();if(location.hash.startsWith('#step-')){showStep(location.hash.slice(6));requestAnimationFrame(()=>$('audit').scrollIntoView());}
</script></main></body></html>'''
