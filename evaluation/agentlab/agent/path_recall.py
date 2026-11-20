"""Public historical anchors; opaque backend keys never enter context."""
from __future__ import annotations
import copy
import math
from agentlab.agent.map_nodes import EVENTS, same_observed_area

VERSION='explored_path_recall_v1'


class RecallNodes:
    def __init__(self):
        self.nodes={}
        self._anchors={}
        self._centres={}
        self._current=None

    def observe(self, geometry, entry, oid, step, result=None):
        anchor=geometry.get('_recall_anchor') or {}
        if anchor.get('version')!=VERSION or not anchor.get('key'):
            raise ValueError('enabled path recall requires a server historical-pose receipt')
        floor=int(geometry['floor']);pos=entry['pose'][:2]
        self._current=(floor,list(pos))
        result=result or {};verb=(result.get('action') or {}).get('verb')
        # Backend keeps all physical edges/anchors. Public P nodes are stations,
        # not every turn and 75 cm controller primitive inside one decision.
        if result.get('decision_type')=='gt_navigate' and result.get('navigation_substep') is not None:return
        event=verb in EVENTS and result.get('success') is True
        arrival=verb=='gt_navigate' and result.get('primitive_actions',0)>0
        transition=verb in ('use_stairs','use_elevator') and result.get('success') is True
        rows=[n for n in self.nodes.values() if n['floor']==floor]
        near=[n for n in rows if math.dist(self._centres[n['id']],pos)<.85
              and same_observed_area(entry,n['position_local_m'],pos)]
        if near:
            n=min(near,key=lambda n:math.dist(n['position_local_m'],pos))
            # Keep IDs stable; update to an actual observed pose AND its matching
            # private anchor, never an averaged coordinate or stale heading.
            n.update(position_local_m=list(pos),observation_id=oid,step=step,heading_deg=entry['pose'][2])
            self._anchors[n['id']]=copy.deepcopy(anchor)
            n['last_seen_step']=step
            if event and math.dist(n['position_local_m'],pos)<.12:
                n['public_event']=dict(verb=verb,message=str(result.get('message',''))[:240],
                    success=result.get('success') is True,observation_id=oid,step=step)
                n.update(label=EVENTS[verb],kind='event')
            return
        if rows and not (event or arrival or transition):
            if min(math.dist(n['position_local_m'],pos) for n in rows)<2.5:return
        nid=f'P{len(self.nodes)+1:03d}'
        kind='start' if not rows else 'event' if event else 'transit' if transition else 'arrival' if arrival else 'route'
        n=dict(id=nid,floor=floor,position_local_m=list(pos),step=step,
            observation_id=oid,heading_deg=entry['pose'][2],kind=kind,last_seen_step=step,
            label=EVENTS[verb] if event else '换层到达点' if transition else ('导航落点' if result.get('success') else '导航受阻停点') if arrival else '首次到达点' if not rows else '已走路线关键点',
            returnable_hint='每次请求重新检查，未预先保证可回溯')
        if event:n['public_event']=dict(verb=verb,message=str(result.get('message',''))[:240],
            success=result.get('success') is True,observation_id=oid,step=step)
        self.nodes[nid]=n;self._anchors[nid]=copy.deepcopy(anchor);self._centres[nid]=list(pos)

    def candidates(self, current_floor):
        rows=list(self.nodes.values())
        chosen=sorted(rows,key=lambda n:(n['floor']==current_floor,n['kind'] in ('start','transit','event'),n.get('last_seen_step',n['step'])),reverse=True)[:16]
        unique={n['id']:n for n in chosen}
        result=copy.deepcopy(sorted(unique.values(), key=lambda n:(n['floor']!=current_floor,n['step'])))
        for n in result:
            if self._current and self._current[0]==n['floor']:
                n['distance_from_current_m']=round(math.dist(n['position_local_m'],self._current[1]),2)
        return result

    def target(self, node_id, floor):
        if node_id not in {n['id'] for n in self.candidates(floor)}:return None
        return dict(node=copy.deepcopy(self.nodes[node_id]),anchor_key=self._anchors[node_id]['key'])
