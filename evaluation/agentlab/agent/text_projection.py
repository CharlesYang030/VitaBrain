"""Reversible public-data encoding; never selects facts or queries the world."""
import copy
import json

VERSION = 'public_text_projection_v1'
TABLE_RULE = {
    'version': 'rows_v1',
    'rule': 'format=rows_v1等价于对象列表：每行按columns对应rows各值，再合并defaults公共字段。保留全部行、ID、坐标与来源；P/N编号仍与地图一致，按id选点。',
}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def pack_rows(rows):
    if not isinstance(rows, list) or len(rows) < 2 or not all(isinstance(r, dict) for r in rows):
        return rows
    keys = list(rows[0])
    # Do not conflate absent fields with explicit nulls or bools with integers.
    if any(set(row) != set(keys) for row in rows):
        return rows
    common = {k: rows[0][k] for k in keys
              if all(dumps(row[k]) == dumps(rows[0][k]) for row in rows)}
    columns = [k for k in keys if k not in common]
    return dict(format='rows_v1', defaults=common, columns=columns,
                rows=[[row[k] for k in columns] for row in rows])


def unpack_rows(value):
    """For acceptance/analysis; actions continue to use the unchanged native state."""
    if isinstance(value, list):
        return value
    assert value['format'] == 'rows_v1'
    return [{**value['defaults'], **dict(zip(value['columns'], row))} for row in value['rows']]


def compact_public_text(body, count):
    result = copy.deepcopy(body)
    before = count(dumps(body))
    paths = []
    # Action handles and the current action receipt are deliberately untouched.
    for path in (('spatial_memory', 'nodes'), ('spatial_memory', 'recall_candidates'),
                 ('current_observation', 'safety', 'recent_events')):
        parent = result
        for key in path[:-1]:
            parent = parent.get(key, {}) if isinstance(parent, dict) else {}
        if not isinstance(parent, dict) or path[-1] not in parent:
            continue
        original = parent[path[-1]]
        packed = pack_rows(original)
        if isinstance(packed, dict) and count(dumps(packed)) < count(dumps(original)):
            parent[path[-1]] = packed
            paths.append('.'.join(path))
    if paths:
        result['table_encoding'] = TABLE_RULE
    after = count(dumps(result))
    # Include the decoding instruction cost. Tiny lists stay in ordinary JSON.
    if after >= before:
        result, after, paths = copy.deepcopy(body), before, []
    return result, dict(version=VERSION, table_paths=paths,
                        before_text_tokens=before, after_text_tokens=after,
                        saved_text_tokens=before-after, lossless_tables=True)
