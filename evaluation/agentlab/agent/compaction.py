"""Episode-local, same-provider memory compaction. Never reads environment truth.

Only the prompt projection is replaced; StructuredMemory and the hash-chain audit
remain intact. Provenance validation does not certify the semantics of a summary.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from agentlab.agent.text_projection import VERSION as TEXT_PROJECTION_VERSION
from agentlab.providers.base import ProviderOutputUnavailable

VERSION = 'model_self_compaction_v1'


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()[:24]


class BudgetExceeded(RuntimeError):
    """Legacy compatibility error; soft budget overruns no longer raise this."""


class CompactionOutputUnavailable(ProviderOutputUnavailable):
    """Output remained truncated after bounded recovery; no action may follow."""


def parse_summary(content):
    """Accept one enclosing Markdown fence, without repairing incomplete JSON."""
    match = re.fullmatch(r'\s*```(?:json)?[ \t]*\r?\n(.*?)\r?\n```\s*',
                         content, flags=re.DOTALL | re.IGNORECASE)
    return json.loads(match.group(1) if match else content), bool(match)


@dataclass(frozen=True)
class Budget:
    trigger: int
    target: int
    hard: int


def budget_for(config, floors=1):
    frames = config.visual_frame_count
    mapped = config.fog_gt_enabled or config.spatial_memory_enabled
    values = ({1:(14000,9000,16000),5:(18000,11000,22000),10:(24000,14000,30000)}
              if mapped else {1:(12000,7000,15000),5:(16000,9000,20000),10:(22000,12000,27000)})[frames]
    extra = 2000 * max(0, floors-1) if mapped else 0
    # One current front depth image, independent of history window/floor count.
    # Explicit per-run budget overrides below remain authoritative.
    if getattr(config, "depth_image_enabled", False):
        extra += 2000
    return Budget(*(getattr(config, 'memory_compaction_'+name+'_tokens', None) or (v+extra)
                    for name,v in zip(('trigger','target','hard_limit'), values)))


class TokenMeter:
    def __init__(self):
        self.encoding = None
        self.method = 'utf8_estimate_plus_visual_reserve'
        try:
            import tiktoken
            self.encoding = tiktoken.get_encoding('o200k_base')
            self.method = 'o200k_base_plus_calibrated_visual_reserve'
        except Exception:
            pass
        self.image_reserve = 1600.0
        self.text_ratio = 1.0
        self.calibrated = False

    def count(self, text):
        if self.encoding:
            return len(self.encoding.encode(text, disallowed_special=()))
        return math.ceil(len(text.encode('utf-8'))/3)

    def parts(self, messages):
        texts=[]; images=0
        for message in messages:
            content=message['content']
            if isinstance(content,str):texts.append(content)
            else:
                for part in content:
                    if part['type']=='text':texts.append(part['text'])
                    elif part['type']=='image_url':images+=1
        return sum(self.count(t) for t in texts)+16, images

    def estimate(self, messages):
        text, images = self.parts(messages)
        return math.ceil(text*self.text_ratio+images*self.image_reserve)

    def calibrate(self, messages, usage):
        actual=(usage or {}).get('prompt_tokens') or (usage or {}).get('input_tokens')
        if not isinstance(actual,(int,float)) or actual <= 0:return
        text, images=self.parts(messages)
        if images:
            self.image_reserve=max(0,(actual-text*self.text_ratio)/images)
        else:
            self.text_ratio=max(.5,min(3,actual/max(1,text)))
        self.calibrated=True


class SelfCompactor:
    def __init__(self, config, skill_path):
        self.config=config
        self.skill=skill_path.read_text(encoding='utf-8')
        self.skill_sha256=hashlib.sha256(self.skill.encode()).hexdigest()
        self.meter=TokenMeter()
        self.summary=None
        self.pending={}
        self.seen=set()
        self.last_step=-100
        self.after_tokens=0
        self.revision=0
        self.status='idle'
        self.calls=[]
        self.last_budget=budget_for(config)
        self.last_error=None
        self._previous_plan=None
        self.last_projection={}

    @staticmethod
    def provenance(value):
        observations=set();facts=set()
        def visit(v):
            if isinstance(v,dict):
                for k,w in v.items():
                    if k in ('observation_id','source_observation_id','final_observation_id','previous_observation_id') and isinstance(w,str):observations.add(w)
                    if k=='source_observation_ids' and isinstance(w,list):observations.update(x for x in w if isinstance(x,str))
                    if k=='fact_id' and isinstance(w,str):facts.add(w)
                    if k=='memory_fact_ids' and isinstance(w,list):facts.update(x for x in w if isinstance(x,str))
                    visit(w)
            elif isinstance(v,list):
                for w in v:visit(w)
        visit(value)
        return dict(source_observation_ids=sorted(observations),memory_fact_ids=sorted(facts))

    def snapshot(self):
        return dict(enabled=True,version=VERSION,status=self.status,revision=self.revision,
                    budget=asdict(self.last_budget) if self.last_budget else None,
                    pending_records=len(self.pending),summary=self.summary,
                    last_error=self.last_error,skill_sha256=self.skill_sha256,
                    token_estimator=self.meter.method,calibrated=self.meter.calibrated,
                    calls=copy.deepcopy(self.calls),estimated_after_tokens=self.after_tokens,
                    text_projection=copy.deepcopy(self.last_projection),
                    text_projection_version=TEXT_PROJECTION_VERSION,
                    budget_policy="soft_limits_preserve_pending_v2",
                    estimated_input_tokens=getattr(self,'estimated_input_tokens',None))

    def ingest(self, body):
        # Inputs have already passed public_observation and memory provenance checks.
        rows=[]
        for section in ('structured_memory','recent_public_results'):
            rows.extend((section,row) for row in body.get(section,[]))
        for node in (body.get('spatial_memory') or {}).get('nodes',[]):
            for key in ('memory_refs','public_events'):
                rows.extend(('map_history',dict(node_id=node['id'],kind=key,value=row))
                            for row in node.get(key,[]))
        recall=(body.get('path_recall_state') or {}).get('last_return')
        if isinstance(recall,dict) and recall:
            rows.append(('recall_history',recall))
        plan=(body.get('task_support') or {}).get('current_plan')
        if isinstance(plan,dict) and plan:
            if self._previous_plan is not None and fingerprint(plan)!=fingerprint(self._previous_plan):
                rows.append(('superseded_plan',self._previous_plan))
            self._previous_plan=copy.deepcopy(plan)
        for section,row in rows:
            key=fingerprint(dict(section=section,value=row))
            if key in self.seen:continue
            self.seen.add(key)
            self.pending[key]=dict(id=key,section=section,value=copy.deepcopy(row))

    def projection(self, body, summary, pending):
        result=copy.deepcopy(body)
        result.pop('structured_memory',None)
        # Current actions, feedback and actionable map IDs remain live.
        result['recent_public_results']=body.get('recent_public_results',[])[-1:]
        spatial=result.get('spatial_memory') or {}
        info=result.get('information_contract') or {}
        if info.get('map_assistance')==spatial.get('assistance') and 'map_assistance' in info:
            info['map_assistance']={'see':'spatial_memory.assistance'}
        for view in (result.get('current_observation',{}).get('view_contract') or {}).get('views',[]):
            view.pop('sha256',None) # already delivered in each image caption; retained in audit
        for node in spatial.get('nodes',[]):
            # These full historical records are ingested above, then summarized by
            # the tested model. Keeping duplicates here would defeat compaction.
            node.pop('memory_refs',None)
            node.pop('public_events',None)
        candidates=spatial.get('recall_candidates',[])
        if candidates:
            hints={row.get('returnable_hint') for row in candidates}
            if len(hints)==1 and next(iter(hints)):
                spatial['recall_returnable_rule']=next(iter(hints))
                for row in candidates:row.pop('returnable_hint',None)
        # Full recall narrative is already retained once in the model-owned
        # history. Only obsolete receipts lose their duplicated prose here;
        # a receipt for the current observation remains verbatim.
        recall=(result.get('path_recall_state') or {}).get('last_return')
        current_oid=(result.get('current_observation') or {}).get('observation_id')
        if isinstance(recall,dict) and recall and current_oid and recall.get('final_observation_id') and recall['final_observation_id']!=current_oid:
            record_id=fingerprint(dict(section='recall_history',value=recall))
            for key in ('message','reason','current_goal'):
                recall.pop(key,None)
            recall.update(historical_receipt=True,history_record_id=record_id)
        from agentlab.agent.text_projection import compact_public_text
        result,self.last_projection=compact_public_text(result,self.meter.count)
        result['memory_context']=dict(version=VERSION,summary=summary,
            uncompressed_delta=list(pending.values()),
            rule='摘要由待测模型生成，并非环境认证。当前观测和较新的动作回执优先；历史指代不可直接用作当前动作目标。地图事件、旧回溯详情和过期计划在此摘要和增量中；historical_receipt只是旧回执，不代表当前状态。')
        return result

    @staticmethod
    def replace(messages, body, suffix):
        result=copy.deepcopy(messages)
        result[-1]['content'][-1]['text']=dumps(body)+suffix
        return result

    def prepare(self, messages, manifest, step, complete, audit, wait=None, output_budget=None):
        # ContextBuilder always ends the final user text with body JSON + schema.
        last=messages[-1]['content'][-1]['text']
        body,end=json.JSONDecoder().raw_decode(last)
        suffix=last[end:]
        self.ingest(body)
        floors=(manifest.get('visual_input') or {}).get('map_image_count',1)
        budget=self.last_budget=budget_for(self.config,floors)
        current=self.replace(messages,self.projection(body,self.summary,self.pending),suffix)
        before=self.meter.estimate(current)
        # Crossing the soft ceiling never bypasses the compression cooldown.
        due=(before >= budget.trigger and
             (step-self.last_step>=4 or before-self.after_tokens>=2000))
        if due and self.pending:
            self.status='compressing'
            fixed=self.replace(messages,self.projection(body,None,{}),suffix)
            fixed_tokens=self.meter.estimate(fixed)
            # Target is TOTAL decision input, never just the summary length.
            allowance=max(768,math.floor((budget.target-fixed_tokens)/self.meter.text_ratio)-192)
            sources=list(self.pending.values())
            source_ids={row['id'] for row in sources}
            if self.summary:
                source_ids.update(e['id'] for e in self.summary['entries'])
            source_by_id={row['id']:row for row in sources}
            if self.summary:source_by_id.update({e['id']:e for e in self.summary['entries']})
            multiplier = 1
            exhausted_truncation = False
            for attempt in range(3):
                if attempt:(wait or time.sleep)(20)
                request=[{'role':'system','content':self.skill}, {'role':'user','content':dumps(dict(
                    version=VERSION,interface_mode=self.config.interface_mode.value,
                    budget=asdict(budget),summary_token_budget=allowance,
                    previous_summary=self.summary,delta=sources,
                    retry_error=self.last_error if attempt else None))}]
                audit('MEMORY_COMPACTION_REQUESTED',dict(messages=request,step=step,
                    estimated_before_tokens=before,fixed_input_tokens=fixed_tokens,
                    summary_token_budget=allowance,skill_sha256=self.skill_sha256,attempt=attempt+1,
                    output_multiplier=multiplier,
                    completion_limit=next(iter(output_budget(multiplier).values()), None)
                        if callable(output_budget) else None))
                started=time.perf_counter()
                call=None
                truncated=False
                try:
                    response=(complete(request,output_token_multiplier=multiplier)
                              if multiplier != 1 else complete(request))
                    choices=(response.raw or {}).get('choices') or []
                    truncated=bool(choices and choices[0].get('finish_reason')=='length')
                    call=dict(step=step,attempt=attempt+1,latency_ms=response.latency_ms,
                              usage=response.usage,accepted=False,output_multiplier=multiplier,
                              output_truncated=truncated)
                    self.calls.append(call)
                    audit('MEMORY_COMPACTION_RESPONDED',dict(content=response.content,
                        usage=response.usage,latency_ms=response.latency_ms,raw=response.raw))
                    data,fence_removed=parse_summary(response.content)
                    if truncated:
                        raise ValueError('provider marked summary output as truncated')
                    if not isinstance(data,dict) or set(data)!={'entries'} or not isinstance(data['entries'],list):
                        raise ValueError('expected {"entries": [...]}')
                    if len(data['entries'])>48:raise ValueError('at most 48 summary entries')
                    if sources and not data['entries']:raise ValueError('empty summary loses all evidence')
                    for entry in data['entries']:
                        if not isinstance(entry,dict) or set(entry)!={'kind','text','sources'}:
                            raise ValueError('entry requires kind/text/sources only')
                        if entry['kind'] not in ('goal','progress','fact','hypothesis','failure','route','constraint'):
                            raise ValueError('unknown summary kind')
                        if not isinstance(entry['text'],str) or not entry['text'].strip():
                            raise ValueError('empty summary text')
                        if not isinstance(entry['sources'],list) or not entry['sources'] or any(
                                not isinstance(s,str) or s not in source_ids for s in entry['sources']):
                            raise ValueError('summary cites an undelivered source')
                    summary_tokens=self.meter.count(dumps(data))
                    summary_over_target=summary_tokens>allowance
                    candidate=dict(revision=self.revision+1,origin='agent_summary_unverified',
                        entries=[dict(e,id=f"M{self.revision+1}:{i}",
                            **self.provenance([source_by_id[s] for s in e["sources"]])) for i,e in enumerate(data['entries'])])
                    after=self.replace(messages,self.projection(body,candidate,{}),suffix)
                    estimate=self.meter.estimate(after)
                    self.summary=candidate;self.pending={};self.revision+=1
                    self.last_step=step;self.after_tokens=estimate;current=after
                    call.update(accepted=True,estimated_before_tokens=before,estimated_after_tokens=estimate,
                        summary_tokens=summary_tokens,summary_over_target=summary_over_target,
                        outer_markdown_fence_removed=fence_removed)
                    self.last_error=None;self.status='ready'
                    audit('MEMORY_COMPACTED',dict(**self.snapshot(),target_reached=estimate<=budget.target))
                    break
                except Exception as error:
                    from agentlab.providers.openai_compatible import ProviderRequestCancelled
                    if isinstance(error,ProviderRequestCancelled):
                        self.status='cancelled';raise
                    if call is None:
                        self.calls.append(dict(step=step,attempt=attempt+1,accepted=False,
                            latency_ms=(time.perf_counter()-started)*1000,usage={},error=str(error)))
                    self.last_error=str(error)
                    audit('MEMORY_COMPACTION_REJECTED',dict(error=str(error),attempt=attempt+1,
                        output_truncated=truncated,output_multiplier=multiplier,
                        previous_summary_retained=True,environment_action_executed=False))
                    if call is None:
                        self.status='unavailable'
                        raise CompactionOutputUnavailable(
                            '记忆摘要接口请求失败；已暂停并保留原摘要和新增记录，未执行本步动作。'
                            '需要恢复模型服务后重试，不能据此判定任务失败。') from error
                    # Transport retries already have the Provider's bounded backoff.
                    # Do not multiply transport retries with an outer request loop.
                    if not isinstance(error,(ValueError,BudgetExceeded)):break
                    if truncated:
                        next_multiplier = 2 if attempt == 0 else 16
                        if attempt < 2 and multiplier < 16 and callable(output_budget) and output_budget(next_multiplier):
                            multiplier = next_multiplier
                            continue
                        exhausted_truncation = True
                        break
                    # Keep the original one schema retry for complete responses.
                    if attempt >= 1:break
            if self.status=='compressing':
                self.status='failed';self.last_step=step;self.after_tokens=before
            if exhausted_truncation:
                raise CompactionOutputUnavailable(
                    '记忆摘要在有限输出预算重试后仍被截断；已暂停，未执行本步动作。'
                    '需要检查摘要输出预算或模型接口，不能据此判定任务失败。')
        estimated=self.meter.estimate(current)
        self.estimated_input_tokens=estimated
        if estimated>=budget.hard:
            # Preserve the full projected input, including unsummarized evidence.
            # A target is not permission to silently hide newly read clues.
            self.status='over_budget'
            audit('MEMORY_COMPACTION_BUDGET_WARNING',dict(
                estimated_input_tokens=estimated,soft_ceiling_tokens=budget.hard,
                pending_records=len(self.pending),pending_preserved=True,
                target_reached=False,environment_action_executed=False))
        final_body,_=json.JSONDecoder().raw_decode(current[-1]['content'][-1]['text'])
        text_tokens,image_count=self.meter.parts(current)
        manifest=copy.deepcopy(manifest)
        manifest['messages']=current
        manifest['text_projection']=copy.deepcopy(self.last_projection)
        manifest['input_budget_breakdown']=dict(text_tokens=text_tokens,image_count=image_count,
            image_token_reserve=self.meter.image_reserve,estimator_calibrated=self.meter.calibrated,
            estimated_total_tokens=estimated,
            section_text_tokens={k:self.meter.count(dumps(v)) for k,v in final_body.items()},
            note='section values independently tokenized; not additive provider billing')
        manifest['memory_compaction']=dict(version=VERSION,revision=self.revision,budget=asdict(budget),
            budget_policy='soft_limits_preserve_pending_v2',pending_records=len(self.pending),
            output_recovery_policy='truncation_1x_2x_16x_then_pause_v2',
            json_envelope_policy='single_outer_markdown_fence_v1',
            estimated_input_tokens=estimated,target_reached=estimated<=budget.target,
            estimator=self.meter.method,status=self.status)
        manifest['character_count']=sum(len(p['text']) for m in current for p in
            ([{'type':'text','text':m['content']}] if isinstance(m['content'],str) else m['content']) if p['type']=='text')
        return current,manifest
