from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agentlab.domain import ALLOWED_VERBS, InterfaceMode, public_observation
from agentlab.information import contract
from agentlab.agent.behavior import public_history_warnings
from agentlab.agent.output_contract import VERSION as OUTPUT_CONTRACT_VERSION, merge_output_example


class ContextBuilder:
    def __init__(self, prompt_dir: Path, character_budget: int = 40000):
        self.prompt_dir = prompt_dir
        self.character_budget = character_budget
        self.system_prompt = (prompt_dir / "system.md").read_text(encoding="utf-8")
        self.decision_prompt = (prompt_dir / "decision.md").read_text(encoding="utf-8")
        self.e2e_decision_prompt = (prompt_dir / 'decision_e2e.md').read_text(encoding='utf-8')

    def build(
        self,
        observation: dict[str, Any],
        memory: list[dict[str, Any]],
        mode: InterfaceMode,
        recent_results: list[dict[str, Any]],
        spatial_memory: dict[str, Any] | None = None,
        history_frames: list[dict] | None = None,
        visual_frame_count: int = 1,
        task_support: dict | None = None,
        navigation_state: dict | None = None,
        path_recall_state: dict | None = None,
        model_compaction_enabled: bool = False,
        gt_navigation_enabled: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if visual_frame_count not in (1, 5, 10):
            raise ValueError('visual_frame_count must be 1, 5 or 10')
        modules = {}
        def feature(name):
            path=self.prompt_dir / 'features' / (name + '.md')
            compact=self.prompt_dir / 'compact' / 'features' / (name + '.md')
            if model_compaction_enabled and compact.is_file():path=compact
            text = path.read_text(encoding='utf-8')
            modules[name] = hashlib.sha256(text.encode()).hexdigest()
            return '\n' + text
        observation = public_observation(observation, mode)
        triplet = observation.get('current_views')
        decision_prompt = self.e2e_decision_prompt if mode == InterfaceMode.E2E else self.decision_prompt
        if model_compaction_enabled:
            decision_prompt=(self.prompt_dir/'compact'/('decision_e2e.md' if mode==InterfaceMode.E2E else 'decision.md')).read_text(encoding='utf-8')
            modules['model_self_compaction_v1']=hashlib.sha256(decision_prompt.encode()).hexdigest()
        decision_prompt, required_fields = merge_output_example(
            decision_prompt, task_support=task_support,
            rgb_mapping=spatial_memory is not None and spatial_memory.get('version') == 'public_rgb_bev_v3',
            navigation=navigation_state is not None, path_recall=path_recall_state is not None,
            gt_navigation=gt_navigation_enabled)
        if observation.get('safety') is not None:
            decision_prompt += feature('safety')
        # The action schema permits 5m, but the model needs a short safe-use rule.
        if gt_navigation_enabled:
            decision_prompt += feature('gt_navigation')
        else:
            decision_prompt += feature('movement_efficiency_v1')
        if observation.get('front_depth'):
            decision_prompt += feature('metric_depth_gt_v1' if gt_navigation_enabled else 'metric_depth_v1')
        if triplet:
            decision_prompt += feature('current_views')
        fog_gt = spatial_memory is not None and spatial_memory.get('version') in ('explored_gt_fog_v1', 'explored_gt_fog_v2', 'explored_gt_bev_v3', 'explored_gt_bev_v4', 'explored_gt_bev_v5')
        map_images = ((spatial_memory.get('images') or [spatial_memory['image']]) if spatial_memory else [])
        if spatial_memory is not None:
            if fog_gt:
                decision_prompt += feature({'explored_gt_fog_v2':'fog_gt_v2', 'explored_gt_bev_v3':'fog_bev_v3', 'explored_gt_bev_v4':'fog_bev_v4', 'explored_gt_bev_v5':'fog_bev_v5'}.get(spatial_memory.get('version'),'fog_gt'))
            elif spatial_memory.get('version') == 'public_rgb_bev_v3':
                decision_prompt += feature('map')
            else:
                from agentlab.agent.spatial import PROMPT
                decision_prompt += '\n' + PROMPT
        if visual_frame_count > 1:
            decision_prompt += feature('history') + (f'\n本轮历史正视图最多 {visual_frame_count-1} 张，当前始终提供左正右 3 张，第一视角图片合计最多 {visual_frame_count+2} 张，BEV 另计。'
                if triplet else f'\n本轮第一视角上限为 {visual_frame_count} 张（包含当前帧）。')
        if task_support is not None:
            decision_prompt += feature('task_support')
            decision_prompt += feature('room_exploration_v1')
        if navigation_state is not None:
            if navigation_state.get('version') == 'front_rgb_navigation_v2':
                decision_prompt += feature('navigation_v2')
            else:
                decision_prompt += feature('navigation')
        if path_recall_state is not None:
            if navigation_state is not None or not fog_gt:
                raise ValueError('path recall requires explored GT and excludes legacy pixel navigation')
            recall_prompt = feature('path_recall_v1')
            if gt_navigation_enabled:
                recall_prompt = recall_prompt.replace('每次决策只能二选一：', '使用原有动作/回溯时：')
            decision_prompt += recall_prompt
        obs_for_text = {key: value for key, value in observation.items() if key not in ('rgb', 'current_views', 'front_depth', 'available_actions')}
        if triplet:
            obs_for_text['view_contract'] = dict(version=triplet['version'],target_view_id='front',
                views=[{k:v[k] for k in ('view_id','label','yaw_offset_degrees','width','height','sha256','annotation_rect_px')} for v in triplet['views']])
        body = {
            "allowed_verbs": sorted(ALLOWED_VERBS.intersection(observation.get('available_actions', ALLOWED_VERBS))),
            "interface_mode": mode.value,
            'information_contract': contract(mode),
            "interface_rule": (
                "操作对象只能用当前 observation_id 与归一化像素点指代。"
                if mode == InterfaceMode.E2E else
                "操作对象只能用当前 observation_id 与公开匿名 handle 指代。"
            ),
            "current_observation": obs_for_text,
            "structured_memory": memory,
            "recent_public_results": recent_results[-6:],
            "behavior_diagnostics": public_history_warnings(recent_results),
        }
        if spatial_memory is not None:
            body['spatial_memory'] = spatial_memory['state']
            if fog_gt:
                body['information_contract']['map_assistance'] = spatial_memory['state']['assistance']
        if task_support is not None:
            body['task_support'] = task_support
        if navigation_state is not None:
            body['navigation_state'] = navigation_state
        if path_recall_state is not None:
            body['path_recall_state'] = path_recall_state
        body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        if not model_compaction_enabled and len(body_text) > self.character_budget:
            body["structured_memory"] = memory[:10]
            body["recent_public_results"] = recent_results[-3:]
            body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            # Never cut a JSON document (or the current task) midway. Drop
            # older memory/results first; an oversized observation is explicit.
            while len(body_text) > self.character_budget and (body['structured_memory'] or body['recent_public_results']):
                if body['structured_memory']:
                    body['structured_memory'] = body['structured_memory'][:-1]
                else:
                    body['recent_public_results'] = body['recent_public_results'][:-1]
                body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))

        content: list[dict[str, Any]] = [{"type": "text", "text": body_text + "\n\n" + decision_prompt}]
        rgb = observation.get("rgb")
        if isinstance(rgb, dict) and isinstance(rgb.get("data_url"), str):
            content.insert(0, {"type": "image_url", "image_url": {"url": rgb["data_url"]}})
        if spatial_memory is not None:
            content.insert(len(content) - 1, {'type': 'image_url', 'image_url': {
                'url': spatial_memory['image']['data_url']}})
        history_metadata = []
        if modules:
            from agentlab.agent.temporal import frame_caption
            # Independent image parts retain resolution. Every picture has its own caption.
            content = []
            for frame in (history_frames or [])[-(visual_frame_count-1):] if visual_frame_count > 1 else []:
                if frame['observation_id'] == observation['observation_id']:
                    raise ValueError('current frame duplicated as history')
                caption = frame_caption(frame)
                history_metadata.append(caption)
                content.extend([{'type':'text', 'text':'HISTORICAL_FPV\n'+json.dumps(caption,ensure_ascii=False)},
                    {'type':'image_url','image_url':{'url':frame['rgb']}}])
            if triplet:
                from agentlab.agent.views import current_image_parts
                content.extend(current_image_parts(observation))
            elif isinstance(rgb, dict) and isinstance(rgb.get('data_url'), str):
                content.extend([{'type':'text','text':'CURRENT_FPV\n'+json.dumps({
                    'observation_id':observation['observation_id'], 'step':observation.get('step'),
                    'rule':'所有当前对象操作只引用此帧。'},ensure_ascii=False)},
                    {'type':'image_url','image_url':{'url':rgb['data_url']}}])
            from agentlab.agent.depth import depth_parts
            content.extend(depth_parts(observation))
            if spatial_memory is not None:
                for map_image in map_images:
                    caption = (f"EXPLORED_GT_MAP F{map_image['floor']} · " +
                        ('当前楼层' if map_image['current'] else '已访问楼层的旧记忆') +
                        (' · 固定画布；当前左正右可见表面并集揭雾（最远4m，分别检查遮挡）；黑色未知；显式特权视觉辅助。'
                         if spatial_memory.get('version') == 'explored_gt_bev_v5' else
                         ' · 固定画布；当前第一视角可见表面揭雾（最远4m）；黑色未知；显式特权视觉辅助。'
                         if spatial_memory.get('version') == 'explored_gt_bev_v4' else
                         ' · 固定画布同源俯视图；4m 遮挡揭雾；黑色未知；显式特权视觉辅助。'
                         if spatial_memory.get('version') == 'explored_gt_bev_v3' else
                         ' · 4m 遮挡揭雾；黑色未知；显式特权几何辅助。') if fog_gt else
                        'MAP_IMAGE · 本轮公开观察形成的估计地图，不是另一个相机。')
                    content.extend([{'type':'text','text':caption},
                        {'type':'image_url','image_url':{'url':map_image['data_url']}}])
            content.append({'type':'text','text':body_text+'\n\n'+decision_prompt})
        system_prompt = self.system_prompt
        if fog_gt:
            system_prompt += '\n本轮明确授权 EXPLORED_GT_MAP：可使用已揭开的真实几何与精确局部位姿。这是特权地图辅助；黑色未知区域、未访问楼层和任务隐藏答案仍不可推定。对象操作仍只引用当前第一视角。'
            if spatial_memory.get('version') in ('explored_gt_bev_v3', 'explored_gt_bev_v4', 'explored_gt_bev_v5'):
                system_prompt += '\n本版本还明确提供已揭开区域的真实俯视外观，图片沿用 Inspector 的固定相机与原生尺寸。'

        if modules:
            system_prompt = system_prompt.replace('所有事实性记忆必须给出当前 observation_id 作为来源',
                '所有事实性记忆必须给出当前或此前确实收到的 observation_id 作为来源')
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ]
        manifest = {
            'output_contract': {'version': OUTPUT_CONTRACT_VERSION, 'required_feature_fields': required_fields},
            "included_sections": ["system", "interface", "observation", "memory", "recent_results", "output_schema"],
            "character_count": len(self.system_prompt) + len(body_text) + len(decision_prompt),
            "memory_fact_count": len(body["structured_memory"]),
            "observation_id": observation.get("observation_id"),
            "prompt_sha256": hashlib.sha256((system_prompt + decision_prompt).encode()).hexdigest(),
            "messages": messages,
        }
        if spatial_memory is not None:
            manifest['included_sections'].append('online_spatial_memory')
            manifest['spatial_memory'] = spatial_memory
        if modules:
            manifest['prompt_modules'] = modules
            manifest['visual_input'] = dict(requested_fpv_count=visual_frame_count+(2 if triplet else 0),
                actual_fpv_count=len(history_metadata)+(3 if triplet else int(isinstance(rgb,dict) and bool(rgb.get('data_url')))),
                map_image_count=len(map_images), depth_image_count=int(bool(observation.get('front_depth'))),
                sensor_condition='RGB-D' if observation.get('front_depth') else 'RGB', history=history_metadata,
                order='historical_text_image_pairs_then_current_then_map_then_task',
                version='interleaved_recent_fpv_v1')
            if triplet:
                manifest['visual_input'].update(version='interleaved_history_front_current_triplet_v2',
                    temporal_window=visual_frame_count,current_fpv_count=3,history_fpv_count=len(history_metadata),
                    current_view_ids=['left','front','right'],history_view_id='front',
                    order='historical_front_text_image_pairs_then_current_left_front_right_then_optional_depth_then_maps_then_task')
            manifest['character_count'] = len(self.system_prompt)+sum(len(c['text']) for c in content if c['type']=='text')
        return messages, manifest
