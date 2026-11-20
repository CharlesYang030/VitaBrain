"""Map anchors made only from revealed cells, public receipts and accepted memory.

A memory anchor is the agent's observation/recording position, never an inferred
object position. This module has no access to private geometry or task truth.
"""
from __future__ import annotations
import copy
import math

KINDS = {'start':'起点', 'portal':'门口', 'transit':'换层', 'event':'事件记录点', 'note':'记忆记录点'}
EVENTS = {'read':'读取点', 'ask_npc':'交流点', 'answer_npc':'交流点', 'answer_call':'通话点',
          'unlock':'解锁操作点', 'input_code':'密码操作点', 'open':'打开操作点', 'close':'关闭操作点',
          'pickup':'拾取操作点', 'put':'放置操作点', 'drop':'放置操作点',
          'handoff_npc':'交接操作点', 'screen_cycle':'屏幕观察点'}


def same_observed_area(entry, a, b):
    """Do not merge nearby stations across a revealed wall or unknown gap."""
    if math.dist(a,b)<.12:return True
    cells=entry.get('cells',{})
    for i in range(max(2,math.ceil(math.dist(a,b)/.04))+1):
        t=i/max(2,math.ceil(math.dist(a,b)/.04))
        x,z=(a[k]+(b[k]-a[k])*t for k in range(2))
        if cells.get((math.floor(x/.1),math.floor(z/.1)),(0,))[0] not in (1,5):return False
    return True


def heading_label(degrees):
    return ('北 N','东北 NE','东 E','东南 SE','南 S','西南 SW','西 W','西北 NW')[int((degrees%360+22.5)//45)%8]


class MapNodes:
    def __init__(self):
        self.nodes = {}
        self.poses = {}
        self.portal_cells = {}
        self.memory_nodes = {}
        self.associations = {}
        self.entries = {}

    def _make(self, floor, pos, kind, label, oid, step, origin):
        nid=f'N{len(self.nodes)+1:02d}'
        n=dict(id=nid,floor=floor,position_local_m=[round(float(v),3) for v in pos],
               kind=kind,label=label,origin=origin,first_seen_step=step,last_seen_step=step,
               source_observation_ids=[oid],memory_refs=[],public_events=[])
        self.nodes[nid]=n
        return n

    def _touch(self, n, oid, step):
        n['source_observation_ids']=list(dict.fromkeys(n['source_observation_ids']+[oid]))[-6:]
        n['last_seen_step']=max(step,n['last_seen_step'])

    def _near(self, floor, pos, kinds, distance):
        rows=[n for n in self.nodes.values() if n['floor']==floor and n['kind'] in kinds
              and math.dist(n['position_local_m'],pos)<=distance
              and same_observed_area(self.entries.get(floor,{}),n['position_local_m'],pos)]
        return min(rows,key=lambda n:math.dist(n['position_local_m'],pos)) if rows else None

    def observe(self, floor, entry, oid, step, transition=None, result=None, previous_oid=None, resolution=.1):
        internal=(result or {}).get('decision_type')=='gt_navigate' and (result or {}).get('navigation_substep') is not None
        self.entries[floor]=entry
        self.poses[oid]=dict(floor=floor,position=entry['pose'][:2],step=step,keypoint_eligible=not internal)
        if not any(n['floor']==floor for n in self.nodes.values()):
            self._make(floor,entry['pose'][:2],'start',f'F{floor} 首次到达',oid,step,'visited_pose')
        # Connected components of ALREADY REVEALED portal cells. Private door
        # endpoints, identities and unseen portions of openings are not read.
        remaining={p for p,v in entry['cells'].items() if v[0] in (4,5)}
        while remaining:
            start=min(remaining);component={start};stack=[start];remaining.remove(start)
            while stack:
                x,z=stack.pop()
                for dx,dz in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(-1,-1),(1,-1),(-1,1)):
                    q=(x+dx,z+dz)
                    if q in remaining:remaining.remove(q);component.add(q);stack.append(q)
            seen=component.intersection(entry['visible'])
            if len(component)<2 or not seen:continue
            matches=[n for n in self.nodes.values() if n['floor']==floor and n['kind']=='portal'
                     and component.intersection(self.portal_cells.get(n['id'],set()))]
            cx=sum(x for x,z in component)/len(component);cz=sum(z for x,z in component)/len(component)
            x,z=min(component,key=lambda p:((p[0]-cx)**2+(p[1]-cz)**2,p))
            pos=[(x+.5)*resolution,(z+.5)*resolution]  # always a revealed cell
            n=min(matches,key=lambda n:int(n['id'][1:])) if matches else self._make(floor,pos,'portal','门口',oid,step,'revealed_geometry')
            self.portal_cells[n['id']]=component
            n['position_local_m']=[round(v,3) for v in pos]
            latest=max(entry['cells'][p][1] for p in seen)
            states={entry['cells'][p][0] for p in seen if entry['cells'][p][1]==latest}
            n['last_observed_state']='关闭' if states=={4} else '开放' if states=={5} else '部分可见'
            self._touch(n,oid,step)
            for old in matches:
                if old['id']!=n['id']:old['merged_into']=n['id']
        if transition:
            for side,other in (('from','to'),('to','from')):
                f=transition[side+'_floor'];pos=transition[side+'_local_m']
                label='楼梯换层点' if transition['kind']=='use_stairs' else '电梯换层点'
                n=self._near(f,pos,('transit',),.8)
                if n is None:n=self._make(f,pos,'transit',label,oid,step,'public_action_result')
                self._touch(n,oid,step)
                event=dict(step=step,observation_id=oid,message=f"已实际从 F{transition['from_floor']} 到达 F{transition['to_floor']}",
                           kind='floor_transition',arrived_floor=transition['to_floor'],success=True)
                n['public_events']=(n['public_events']+[event])[-5:]
        verb=((result or {}).get('action') or {}).get('verb')
        if previous_oid in self.poses and result and (verb in EVENTS or result.get('collision')):
            pose=self.poses[previous_oid];pos=pose['position'];f=pose['floor']
            n=self._near(f,pos,('note','event'),.8)
            label='受阻记录点' if result.get('collision') else EVENTS[verb]
            if n is None:n=self._make(f,pos,'event',label,previous_oid,step,'public_action_result')
            self._touch(n,oid,step)
            event=dict(step=step,observation_id=oid,recorded_from_observation_id=previous_oid,
                       message=str(result.get('message',''))[:240],kind=verb,success=result.get('success') is True)
            # Repeated identical failed actions at the same place do not flood
            # the legend. Each retained event remains linked to its real source.
            if not n['public_events'] or any(n['public_events'][-1].get(k)!=event[k] for k in ('message','kind','success')):
                n['public_events']=(n['public_events']+[event])[-5:]

    def _canonical_node(self, nid):
        seen=set()
        while nid in self.nodes and self.nodes[nid].get('merged_into') and nid not in seen:
            seen.add(nid);nid=self.nodes[nid]['merged_into']
        return self.nodes.get(nid)

    def sync_memory(self, grouped, links=()):
        facts={f['fact_id']:f for rows in grouped.values() for f in rows if isinstance(f,dict)
               and f.get('fact_id') and f.get('origin') in ('agent_inference','action_result')
               and (f.get('origin')!='action_result' or f.get('source_event_ids'))
               and f.get('source_observation_ids') and all(o in self.poses for o in f['source_observation_ids'])}
        accepted=[]
        # Only a validated, already existing fact can be explicitly linked.
        for link in links[:12] if isinstance(links,list) else []:
            if not isinstance(link,dict):continue
            nid,fid=link.get('node_id'),link.get('fact_id')
            if not isinstance(nid,str) or not isinstance(fid,str):continue
            n=self._canonical_node(nid);f=facts.get(fid)
            if n and f and f.get('status')=='active' and not n.get('merged_into'):
                self.memory_nodes[fid]=n['id'];self.associations[fid]='agent_selected_node';accepted.append(dict(node_id=n['id'],fact_id=fid))
        for fid,f in facts.items():
            if fid not in self.memory_nodes:
                if f.get('state_key')=='confirmed_floor' and f.get('origin')=='action_result':
                    matches=[n for n in self.nodes.values() if any(ev.get('kind')=='floor_transition'
                        and ev.get('arrived_floor')==n['floor'] and ev['step']==f.get('last_confirmed_step',-2)+1
                        for ev in n['public_events'])]
                    if matches:
                        self.memory_nodes[fid]=matches[0]['id'];self.associations[fid]='confirmed_transit_receipt'
                        continue
                if (f.get('status')!='active' or f.get('kind') not in ('evidence','spatial','failure','object')
                        or (f['origin']=='agent_inference' and f.get('confidence',0)<.5)):
                    continue
                oid=f['source_observation_ids'][-1];pose=self.poses[oid]
                if not pose.get('keypoint_eligible',True):continue
                n=self._near(pose['floor'],pose['position'],('note','event'),.8)
                if n is None:n=self._make(pose['floor'],pose['position'],'note','记忆记录点',oid,pose['step'],'recording_pose')
                self.memory_nodes[fid]=n['id'];self.associations[fid]='recording_pose'
        # Rebuild references from the actual store, including retractions and
        # supersession. The map never promotes a model inference to a fact.
        for n in self.nodes.values():n['memory_refs']=[]
        for fid,nid in self.memory_nodes.items():
            if fid not in facts:continue
            f=facts[fid];n=self._canonical_node(nid)
            n['memory_refs'].append({k:copy.deepcopy(f.get(k)) for k in (
                'fact_id','kind','origin','confidence','status','last_confirmed_step','source_observation_ids','source_event_ids')}
                | {'content':str(f.get('content',''))[:240],'association_origin':self.associations.get(fid,'recording_pose')})
        return accepted

    def selected(self, floor, limit=16):
        rows=[n for n in self.nodes.values() if n['floor']==floor and not n.get('merged_into')]
        def rank(n):
            active=any(f['status']=='active' for f in n['memory_refs'])
            return (int(n['kind']=='transit'),int(bool(n['public_events'])),int(active),n['last_seen_step'])
        keep=sorted(rows,key=rank,reverse=True)[:limit]
        result=[]
        for original in keep:
            n=copy.deepcopy(original)
            n['source_observation_ids']=n['source_observation_ids'][-2:]
            n['memory_ref_count']=len(n['memory_refs'])
            n['memory_refs']=sorted(n['memory_refs'],key=lambda f:(f['status']=='active',f.get('last_confirmed_step') or 0),reverse=True)[:2]
            for f in n['memory_refs']:
                f['content']=f['content'][:160]
                f['source_observation_ids']=f['source_observation_ids'][-2:]
                f['source_event_ids']=(f['source_event_ids'] or [])[-1:]
            n['public_events']=n['public_events'][-2:]
            for ev in n['public_events']:ev['message']=ev['message'][:160]
            result.append(n)
        return result

    def state(self, current_floor=None):
        floors=sorted({n['floor'] for n in self.nodes.values()})
        nodes=self.selected(current_floor) if current_floor in floors else []
        others=[self.selected(f) for f in floors if f!=current_floor]
        # A bounded global index, spread across visited floors. Full facts
        # remain in StructuredMemory; the map carries short references only.
        for i in range(16):
            for rows in others:
                if i<len(rows) and len(nodes)<24:nodes.append(rows[i])
        return dict(nodes=nodes,
            node_rule='portal = observed map boundary; note/event = agent recording position, not target position; model inference is unverified; IDs are memory anchors, not handles',
            selection='global 24 nodes; up to 16 on current floor; remaining slots shared across visited floors; 2 brief memory refs per node; 8 legend cards per map')
