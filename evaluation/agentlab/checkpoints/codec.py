"""Data-only local checkpoint codec. No pickle, code, credentials or GT prompt injection."""
import base64
import importlib
from collections import deque
from enum import Enum
from pathlib import Path

import numpy as np


# An explicit schema allowlist: checkpoint files cannot select arbitrary imports.
CLASSES = {
    'agentlab.runner:RunConfig',
    'agentlab.domain:InterfaceMode', 'agentlab.domain:RunMode',
    'agentlab.agent.memory:StructuredMemory', 'agentlab.agent.memory:MemoryFact',
    'agentlab.agent.apc:APCMonitor', 'agentlab.agent.apc:FocusState',
    'agentlab.agent.compaction:SelfCompactor', 'agentlab.agent.compaction:TokenMeter',
    'agentlab.agent.compaction:Budget',
    'agentlab.agent.temporal:VisualHistory', 'agentlab.agent.support:TaskSupport',
    'agentlab.agent.fpv_fog_bev:ThreeViewFogBev',
    'agentlab.agent.fpv_fog_bev:FirstPersonFogBev',
    'agentlab.agent.fog_bev:FixedFogBev', 'agentlab.agent.fog_map:FogOfWarMap',
    'agentlab.agent.estimated_map:EstimatedSpatialMemory',
    'agentlab.agent.perception:FloorPerception',
    'agentlab.agent.pixel_navigation:PixelNavigationController',
    'agentlab.agent.pixel_navigation:NavigationView',
    'agentlab.agent.map_nodes:MapNodes',
    'agentlab.agent.path_recall:RecallNodes',
}
EXCLUDE = {'APCMonitor': {'clock'}, 'TokenMeter': {'encoding'}}


def encode(value):
    if isinstance(value, Enum):
        return {'t': 'enum', 'class': class_key(type(value)), 'v': value.value}
    if value is None or type(value) in (str, bool, int, float):
        return value
    if isinstance(value, np.generic):
        return encode(value.item())
    if isinstance(value, np.ndarray):
        if value.dtype.hasobject:
            raise TypeError('object arrays are not checkpoint data')
        return {'t': 'array', 'dtype': value.dtype.str, 'shape': value.shape,
                'v': base64.b64encode(value.tobytes()).decode()}
    if isinstance(value, Path):
        return {'t': 'path', 'v': str(value)}
    if isinstance(value, dict):
        return {'t': 'dict', 'v': [[encode(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, (list, tuple, set, deque)):
        return {'t': type(value).__name__, 'v': [encode(v) for v in value],
                **({'maxlen': value.maxlen} if isinstance(value, deque) else {})}
    key = class_key(type(value))
    skip = EXCLUDE.get(type(value).__name__, set())
    return {'t': 'object', 'class': key,
            'v': encode({k: v for k, v in vars(value).items() if k not in skip})}


def class_key(cls):
    key = cls.__module__ + ':' + cls.__name__
    if key not in CLASSES:
        raise TypeError('unsupported checkpoint type: ' + key)
    return key


def decode(value):
    if not isinstance(value, dict):
        return value
    kind = value['t']
    if kind == 'dict':
        return {decode(k): decode(v) for k, v in value['v']}
    if kind in ('list', 'tuple', 'set', 'deque'):
        data = [decode(v) for v in value['v']]
        return deque(data, maxlen=value['maxlen']) if kind == 'deque' else {
            'list': list, 'tuple': tuple, 'set': set}[kind](data)
    if kind == 'array':
        dtype = np.dtype(value['dtype'])
        if dtype.hasobject:
            raise ValueError('object arrays forbidden')
        return np.frombuffer(base64.b64decode(value['v'], validate=True), dtype=dtype).reshape(value['shape']).copy()
    if kind == 'path':
        return Path(value['v'])
    if kind not in ('object', 'enum') or value['class'] not in CLASSES:
        raise ValueError('unsupported checkpoint schema')
    module, name = value['class'].split(':')
    cls = getattr(importlib.import_module(module), name)
    if kind == 'enum':
        return cls(value['v'])
    obj = cls.__new__(cls)
    obj.__dict__.update(decode(value['v']))
    if name == 'APCMonitor':
        import time
        obj.clock = time.monotonic
    if name == 'TokenMeter':
        obj.encoding = cls().encoding
    if name == 'FloorPerception':
        obj.ready()
    return obj
