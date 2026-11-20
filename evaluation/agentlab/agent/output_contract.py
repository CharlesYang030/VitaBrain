"""Feature output fields shared by the prompt example and runner validation."""
from __future__ import annotations

import json

VERSION = 'feature_output_contract_v1'


def required_feature_fields(*, task_support=False, rgb_mapping=False,
                            navigation=False, path_recall=False, gt_navigation=False):
    if navigation and path_recall:
        raise ValueError('pixel navigation and path recall are mutually exclusive')
    fields = []
    if task_support:
        fields.append('plan_update')
    if rgb_mapping:
        fields.extend(('place_update', 'visual_landmarks'))
    if navigation:
        fields.extend(('decision_type', 'navigation_request'))
    if path_recall:
        fields.extend(('decision_type', 'return_request'))
    if gt_navigation:
        fields.extend(('decision_type', 'gt_navigation_request'))
    return list(dict.fromkeys(fields))


def merge_output_example(prompt, *, task_support=None, rgb_mapping=False,
                         navigation=False, path_recall=False, gt_navigation=False):
    """Merge enabled fields into the existing main JSON, not a second example.

    This only describes model output; it never synthesizes a missing model plan.
    Feature-specific text still specifies alternate navigation/recall decisions.
    """
    required = required_feature_fields(
        task_support=task_support is not None, rgb_mapping=rgb_mapping,
        navigation=navigation, path_recall=path_recall, gt_navigation=gt_navigation)
    if not required:
        return prompt, required
    start = prompt.index('\n{') + 1
    example, length = json.JSONDecoder().raw_decode(prompt[start:])
    action = example.pop('action')
    if task_support is not None:
        example['plan_update'] = {
            'goal': '近期目标', 'source_observation_ids': ['已收到的 observation_id'],
            'expected_observable_change': '本步预期变化',
            'change_strategy_if': '改变策略的条件',
        }
        if (task_support.get('recovery') or {}).get('required'):
            example['recovery_update'] = {'changed_condition': '改变的条件或继续尝试的新证据'}
    if rgb_mapping:
        example.update(place_update=None, visual_landmarks=[])
    if navigation:
        example.update(decision_type='action', navigation_request=None)
    if path_recall:
        example.update(decision_type='action', return_request=None)
    if gt_navigation:
        example.update(decision_type='action', gt_navigation_request=None)
    example['action'] = action
    # One line per top-level field keeps the unified example compact.
    rendered = '{\n' + ',\n'.join(
        '  ' + json.dumps(k) + ': ' + json.dumps(v, ensure_ascii=False, separators=(',', ':'))
        for k, v in example.items()) + '\n}'
    note = '以下主示例已合并本轮开关要求；保留全部顶层字段。无新信息时按说明用 null 或 []，不要漏字段或编造内容。\n'
    return prompt[:start] + note + rendered + prompt[start + length:], required
