"""Deterministic plain-language display of existing scores; never regrades evidence."""
from collections import Counter

VERBS = {
    'move_forward':'向前移动','move_backward':'向后移动','rotate_left':'向左转身','rotate_right':'向右转身',
    'look_down':'向下看','look_up':'向上看','pickup':'拿取物体','put':'放置物体','drop':'放下物体',
    'open':'打开物体或设备','close':'关闭物体或设备','toggle_on':'开启设备','toggle_off':'关闭设备电源',
    'fill_water':'接水','wait':'等待','ask_npc':'向人物问询','read':'读取线索',
    'gt_navigate':'可见通道选点导航','return_to_visited':'返回已探索的历史路径点',
}
FEEDBACK = {
    'water_unavailable':'当前不满足接水条件','already_filled_with_water':'容器已经装有水',
    'no_space':'环境报告没有放置空间','out_of_reach':'目标超出可达范围',
    'recall_dynamic_obstacle_blocked':'历史路线或终点被动态障碍阻挡',
    'recall_succeeded':'已返回历史位置与朝向',
    'gt_navigation_no_selectable_floor':'没有可验证的可选通道地面',
    'gt_navigation_arrived':'已到达所选地面点',
    'gt_navigation_blocked':'导航途中受阻','navigation_safety_health_drop':'导航途中掉血，停止继续移动',
    'gt_navigation_safety_health_drop':'导航途中掉血，停止继续移动',
    'gt_navigation_query_changed_frame':'通道查询改变观察帧，属于工程异常',
    'succeeded':'操作成功',
}


def plain(text):
    text=str(text or '')
    for old,new in sorted(FEEDBACK.items(),key=lambda kv:-len(kv[0])): text=text.replace(old,new)
    for old,new in [('GT导航','可见通道选点导航'),('GT 导航','可见通道选点导航'),
                    ('BEV','俯视地图'),('affordance操作柄','可交互对象指示'),
                    ('Provider','模型服务'),('NPC','场景人物'),('N/A','未评分')]: text=text.replace(old,new)
    return text


def fact(event,observations=None):
    p=event['payload'];stage=event['stage'];kind='环境记录';parts=[]
    if stage=='ACTION_EXECUTED':
        action=p.get('action') or {};verb=action.get('verb','操作');params=action.get('parameters') or {}
        title=VERBS.get(verb,verb)
        target=action.get('target') or {}
        target_name=(observations or {}).get(target.get('observation_id'),{}).get(target.get('handle'))
        if target_name: title+='（'+target_name+'）'
        if params.get('degrees') is not None: title+=f' {params["degrees"]} 度'
        if params.get('distance_m') is not None: title+=f' {params["distance_m"]:.3f} 米'
        parts.append('环境确认成功。' if p.get('success') is True else '环境确认未成功。' if p.get('success') is False else '结果未确认。')
        if p.get('message'): parts.append(p['message'])
        before=p.get('ego_state_before') or {};after=p.get('ego_state_after') or {}
        a,b=before.get('position_relative_spawn_m'),after.get('position_relative_spawn_m')
        if a is not None and b is not None and a!=b:parts.append(f'位置发生变化：{a} → {b}。')
        if p.get('navigation_substep'):parts.append('这是一次导航决策内部的动作，不是新的模型决策。')
    elif stage in ('NAVIGATION_COMPLETED','PATH_RECALL_COMPLETED'):
        title='选点导航结果' if stage=='NAVIGATION_COMPLETED' else '历史路径回溯结果'
        parts.append('成功。' if p.get('success') is True else '未成功。' if p.get('success') is False else '结果未确认。')
        # Recall message appends model intentions; don't present them as world facts.
        message=(p.get('message') or '').split('模型说明原因：',1)[0].rstrip('；')
        parts.append(message or FEEDBACK.get(p.get('feedback_code'),p.get('feedback_code','')))
        distance=p.get('travelled_m',p.get('distance_m'))
        if distance is not None:parts.append(f'记录距离：{distance} 米。')
    elif stage=='MODEL_INPUT_FACTS':
        title='当时实际提供给模型的状态'
        obs=p.get('observation') or {};ego=obs.get('ego_state') or {}
        if obs.get('public_feedback'):parts.append('公开反馈：'+str(obs['public_feedback']))
        visible=list(dict.fromkeys(o.get('label') for o in obs.get('interactables',[]) if o.get('label')))
        if visible:parts.append('当时接口提供的可交互对象：'+'、'.join(visible)+'。')
        if ego.get('camera_pitch_deg') is not None:parts.append(f'相机俯仰：{ego["camera_pitch_deg"]} 度。')
        if ego.get('position_relative_spawn_m') is not None:parts.append('当前位置：'+str(ego['position_relative_spawn_m'])+'。')
        if obs.get('health') is not None:parts.append(f'生命值：{obs["health"]}。')
    elif stage=='CAPABILITY_SCORED':
        title='任务判定结果'
        if p.get('subtasks_total') is not None:parts.append(f'必做子任务完成 {p.get("subtasks_done")} / {p["subtasks_total"]}。')
        if p.get('task_success') is not None:parts.append('整局已通关。' if p['task_success'] else '整局尚未通关。')
    elif stage=='TARGET_RESOLVED':
        title='实际操作对象的后台确认'
        parts.append('对象标识：'+str(p.get('private_object_id') or p.get('status','未记录'))+'。')
    elif stage=='RESPONSE_PARSED':
        title='模型当时的计划说明';kind='模型自述，不能单独作为成功证据'
        parts.append(p.get('decision_summary') or str(p.get('action') or '模型选择辅助行动'))
    elif stage=='GT_NAVIGATION_MODEL_OUTPUT':
        title='模型的选点回答';kind='模型选择，尚不代表到达'
        parts.append(p.get('content',''))
    elif stage=='RUN_STATE':
        title='运行维护或中断记录'
        if p.get('correction'):parts.append(str(p['correction'].get('message',p['correction'])))
        if p.get('maintenance'):parts.append('存在维护记录，不能自动归因于模型。')
        if p.get('auto_error'):parts.append('运行因服务或工程错误暂停。')
        if not parts:parts.append(str(p.get('status','运行状态更新')))
    else:
        title='记录';parts.append(str(p))
    return {'seq':event['seq'],'decision':event['decision'],'kind':kind,'title':plain(title),
            'text':plain(' '.join(str(v) for v in parts if v))}


def build_explanations(data,summary):
    maximum=data.get('rubric',{}).get('max_score',summary.get('max_score',2))
    rules={r['id']:dict(r) for r in summary['subabilities']}
    if 'D4.3' in rules and maximum==2 and data.get('rubric',{}).get('version')=='behavior_diagnostic_pilot_v1':
        rules['D4.3'].update(
            opportunity='遇到可以解决的失败后，看接下来12次决策中有没有实际调整做法，以及有没有把原来失败的事情做成；同一问题的重复失败合并评价',
            scores=[
                '没做有效调整，也没解决问题：例如一直朝挡路的椅子走，仍然到不了门口',
                '调整了，但没解决问题：例如实际换了路线尝试绕开椅子，仍然没到门口',
                '调整后解决了问题：例如绕开椅子，成功到了原本要去的门口；换做法也必须确实满足原任务，不能随便换个目标算成功',
            ],
            exclude='模型服务故障或环境反馈错误不计分。只是说要换办法，不算实际调整。运行结束时还没解决、又没留足12次决策供观察的，不评分，也不算零分',
        )
    events={e['seq']:e for e in data['events']}
    observations={}
    for e in data['events']:
        if e['stage']=='MODEL_INPUT_FACTS':
            obs=e['payload'].get('observation') or {}
            observations[obs.get('observation_id')]={o['handle']:o['label'] for o in obs.get('interactables',[])
                                                    if o.get('handle') and o.get('label')}
    counted={rid for rule in summary['subabilities'] for rid in rule['scored_ids']}
    explanations={};chain_number=0
    for r in summary['records']:
        rule=rules.get(r.get('capability'),{})
        if r['source']=='model_review':chain_number+=1
        score=r.get('score')
        criterion=rule.get('scores',[])[score] if type(score) is int and 0<=score<=maximum and rule.get('scores') else '证据不足或不适用，不记为零分'
        if r['id'] in counted:
            outcome=f'本条计入评分：{score} / {maximum} 分。'
        elif r['status']=='rejected':outcome='本条不计分：引用或事实断言核验未通过。'
        elif r['status']=='duplicate':outcome='本条不重复计分：与另一条证据链合并。'
        elif r['status']=='review_conflict':outcome='本条不计分：重叠片段的评审分数冲突。'
        else:outcome='本条不计分：证据不足、未满足评分条件，或不属于当前计分来源。'
        explanations[r['id']]={
            'number':chain_number if r['source']=='model_review' else None,
            'dimension':rule.get('dimension','未知维度'),'subability':rule.get('name','未知子能力'),
            'mapping':rule.get('dimension','未知维度')+' → '+rule.get('name','未知子能力'),
            'counted':r['id'] in counted,'outcome':outcome,'criterion':plain(criterion),
            'opportunity':plain(rule.get('opportunity')),'reason':plain(r.get('reason')),
            'uncertainty':plain(r.get('uncertainty')),
            'timeline':[fact(events[n],observations) if n in events else {'seq':n,'decision':None,'title':'引用不存在','text':'无法核验','kind':'无效引用'}
                        for n in sorted(set(r.get('evidence',[])))],
        }
    aggregates={}
    for rule in summary['subabilities']:
        rows=[r for r in summary['records'] if r['id'] in rule['scored_ids']]
        distribution=Counter(r['score'] for r in rows)
        earned=sum(r['score'] for r in rows)
        aggregates[rule['id']]={'earned':earned,'possible':maximum*len(rows),
            'formula':f'{earned} ÷ {maximum*len(rows)} × 100 = {rule["score"]} 分' if rows else '没有有效判定点，未评分',
            'distribution':'、'.join(f'{distribution[s]} 条得 {s} 分' for s in range(maximum+1) if distribution[s])}
    return {'records':explanations,'aggregates':aggregates,'rules':rules,'max_score':maximum}
