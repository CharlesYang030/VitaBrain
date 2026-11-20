"""Opt-in, privileged geometry + exact pose with a persistent occlusion mask.

Only masked cell snapshots are exposed. Private geometry, source IDs and global
house extents never enter state()/context(). No learned perception is imported.
"""
from __future__ import annotations
import base64
import io
import math
import time
from PIL import Image, ImageDraw
from agentlab.agent.map_nodes import heading_label

VERSION = 'explored_gt_fog_v2'
PATH_RECALL_VERSION = 'explored_path_recall_v1'
RESOLUTION = .1
RADIUS = 4.0
CONTRACT = dict(version=VERSION, privileged=True, source='bound_geometry_and_exact_pose',
    radius_m=RADIUS, reveal='360_degree_current_floor', occlusion='walls_closed_doors_furniture_bounds',
    persistence='last_seen_cells_only', floor_policy='visited_floors_only',
    excluded='task_answers_object_ids_NPCs_small_items_hidden_floor_count',
    limitations='2D structural visibility; open door leaf omitted; furniture bounds conservative; no body clearance guarantee')


def _line(x0,y0,x1,y1):
    """Supercover line: both cells at a diagonal crossing must be clear."""
    dx, dy = x1-x0,y1-y0
    nx, ny = abs(dx),abs(dy)
    sx, sy = (1 if dx>0 else -1),(1 if dy>0 else -1)
    x,y,ix,iy=x0,y0,0,0
    while ix<nx or iy<ny:
        a,b=(1+2*ix)*ny,(1+2*iy)*nx
        if a==b:
            yield x+sx,y
            yield x,y+sy
            x+=sx; y+=sy; ix+=1; iy+=1
        elif a<b:
            x+=sx; ix+=1
        else:
            y+=sy; iy+=1
        yield x,y


class FogOfWarMap:
    version = VERSION
    def filter_visible(self, geometry, entry, visible):
        return visible

    def __init__(self):
        self.floors = {}
        self.current_floor = None
        self.transitions = []
        from agentlab.agent.map_nodes import MapNodes
        self.node_book = MapNodes()
        self._last_oid = None
        self._relocated = False
        self.last_inference_ms = self.total_inference_ms = 0.
        self._cache = None
        self.recall_enabled = False
        from agentlab.agent.path_recall import RecallNodes
        self.recall_book = RecallNodes()
        self.keypoints_only = True

    def observe_geometry(self, geometry, observation_id, step, recent_result=None):
        start = time.perf_counter()
        if not geometry or geometry.get('version') != 'explored_gt_geometry_v1':
            raise ValueError('opted-in GT map requires atomic private geometry')
        x,z,heading = map(float,geometry['pose'])
        if not all(math.isfinite(v) for v in (x,z,heading)):
            raise ValueError('non-finite GT pose')
        floor = int(geometry['floor'])
        previous_floor = self.current_floor
        self.current_floor = floor
        entry = self.floors.setdefault(floor, dict(origin=[x,z], cells={}, path=[], path_segments=[], visible=set()))
        ox,oz=entry['origin']
        def cell(p):
            return (math.floor((p[0]-ox)/RESOLUTION),math.floor((p[1]-oz)/RESOLUTION))
        cx,cz=cell((x,z))
        pad=int(RADIUS/RESOLUTION)+2
        lx,lz=cx-pad,cz-pad
        side=2*pad+1
        raster=Image.new('L',(side,side),0)
        draw=ImageDraw.Draw(raster)
        def pts(poly):
            return [(cell(p)[0]-lx,cell(p)[1]-lz) for p in poly]
        for p in geometry['floor_polygons']:
            draw.polygon(pts(p),fill=1)
        for p in geometry.get('level_changes',[]):
            draw.polygon(pts(p),fill=6)
        for p in geometry.get('obstacles',[]):
            draw.polygon(pts(p),fill=3)
        for seg in geometry['walls']:
            draw.line(pts(seg),fill=2,width=2)
        for door in geometry['doors']:
            if not door['open']:
                draw.line(pts(door['segment']),fill=4,width=2)
            else:
                # Mark the sill without erasing other intersecting walls.
                for a,b in zip(pts(door['segment'])[:1],pts(door['segment'])[1:]):
                    sill=Image.new('L',raster.size); ImageDraw.Draw(sill).line([a,b],fill=1,width=1)
                    for py in range(side):
                        for px in range(side):
                            if sill.getpixel((px,py)) and raster.getpixel((px,py))==1:
                                raster.putpixel((px,py),5)
        pixels=raster.load()
        visible=set()
        for j in range(side):
            for i in range(side):
                gx,gz=i+lx,j+lz
                # Radius bound applies to cell centers, not just raster indices.
                if math.hypot(ox+(gx+.5)*RESOLUTION-x,oz+(gz+.5)*RESOLUTION-z)>RADIUS:
                    continue
                for tx,tz in _line(cx,cz,gx,gz):
                    val=pixels[tx-lx,tz-lz]
                    if val not in (1,5,6):
                        if (tx,tz)==(gx,gz) and val:
                            visible.add((gx,gz))
                        break
                else:
                    if pixels[i,j]: visible.add((gx,gz))
        visible = self.filter_visible(geometry, entry, visible)
        for key in visible:
            entry['cells'][key]=(pixels[key[0]-lx,key[1]-lz],step)
        entry['visible']=visible
        transition = None
        verb = (recent_result or {}).get('action', {}).get('verb')
        if previous_floor is not None and previous_floor != floor and (recent_result or {}).get('success') and verb in ('use_stairs','use_elevator'):
            transition = dict(from_floor=previous_floor,to_floor=floor,kind=verb,
                from_local_m=self.floors[previous_floor]['pose'][:2],to_local_m=[x-ox,z-oz],
                source_observation_id=observation_id,step=step)
            self.transitions.append(transition)
        entry['pose']=[x-ox,z-oz,heading]
        entry['observation_id']=observation_id
        entry['step']=step
        if not entry['path'] or math.dist(entry['path'][-1],(x-ox,z-oz))>.08:
            entry['path'].append((x-ox,z-oz))
            if not entry['path_segments'] or previous_floor != floor or self._relocated:
                entry['path_segments'].append([])
            entry['path_segments'][-1].append((x-ox,z-oz))
        self._relocated = False
        self.node_book.observe(floor, entry, observation_id, step, transition, recent_result, self._last_oid)
        if self.recall_enabled:
            self.recall_book.observe(geometry, entry, observation_id, step, recent_result)
        self._last_oid = observation_id
        self._cache=None
        self.last_inference_ms=(time.perf_counter()-start)*1000
        self.total_inference_ms+=self.last_inference_ms

    def update_semantics(self,*args):
        return []

    def record_navigation_endpoint(self, geometry, oid, step, summary):
        """Publish the actual terminal pose, never an unachieved selected target."""
        if not summary.get('primitive_actions') or summary.get('travelled_m',0)<=.01:return
        entry=self.floors[self.current_floor]
        if oid in self.node_book.poses:self.node_book.poses[oid]['keypoint_eligible']=True
        if self.recall_enabled:self.recall_book.observe(geometry,entry,oid,step,summary)
        else:
            n=self.node_book._near(self.current_floor,entry['pose'][:2],('event','note'),.8)
            if n is None:
                self.node_book._make(self.current_floor,entry['pose'][:2],'event',
                    '导航落点' if summary['success'] else '导航受阻停点',oid,step,'public_action_result')
        self._cache=None

    def new_chart(self):
        # Do not draw a walked route across a privileged relocation.
        self._relocated = True

    def sync_memory(self, grouped, links=()):
        accepted = self.node_book.sync_memory(grouped, links)
        self._cache = None
        return accepted

    def state(self):
        return dict(version=VERSION, assistance=CONTRACT, current_floor=self.current_floor,
            keypoint_policy='sparse_stations_v1: navigation terminal poses only; nearby observed stations merged with exact latest pose/heading; full walked edges remain server-side',
            floors=[dict(floor=floor,current=floor==self.current_floor,
                explored_area_m2=round(len(e['cells'])*RESOLUTION**2,2),
                last_observation_id=e['observation_id'], last_seen_step=e['step'],
                current_pose_local_m_deg=e['pose'] if floor==self.current_floor else None)
                for floor,e in sorted(self.floors.items())],
            **self.node_book.state(self.current_floor),
            agent_heading=({'degrees':round(self.floors[self.current_floor]['pose'][2]%360,1),
                'direction':heading_label(self.floors[self.current_floor]['pose'][2])}
                if self.current_floor is not None else None),
            confirmed_transitions=self.transitions[-30:],
            coordinates='each floor origin = first arrival; north = Unity +z; clockwise heading',
            **({'recall_candidates': self.recall_candidates()} if self.recall_enabled else {}),
            legend='black unknown; light current; muted remembered; pale walls; orange closed door; green open sill; red walked route lines with sparse keypoints, no straight teleport connectors; red position/heading/cardinal labels; purple stair/level change')

    def recall_candidates(self):
        return self.recall_book.candidates(self.current_floor) if self.recall_enabled else []

    def recall_target(self, node_id):
        return self.recall_book.target(node_id, self.current_floor) if self.recall_enabled else None

    def render(self, floor):
        from agentlab.agent.fog_render import render_map
        return render_map(self, floor, show_recall=self.recall_enabled)

    def context(self):
        if self._cache is None:
            images=[]
            for floor in sorted(self.floors,key=lambda f:(f!=self.current_floor,f)):
                buf=io.BytesIO(); self.render(floor).save(buf,format='PNG')
                images.append(dict(floor=floor,current=floor==self.current_floor,
                    data_url='data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode(),width=1600,height=1050))
            self._cache=dict(version=VERSION,state=self.state(),images=images,image=images[0] if images else None)
        return self._cache
