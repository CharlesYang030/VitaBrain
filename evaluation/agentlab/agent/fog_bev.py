"""Fixed Inspector projection with persistent, server-side masked RGB pixels.

Full source pixels/camera are private and transient. Only masked pixel memory
and public node references reach context(). Existing v1/v2 replays stay intact.
"""
from __future__ import annotations
import base64
import copy
import hashlib
import io
import math
import time
import numpy as np
from PIL import Image, ImageDraw
from agentlab.agent.fog_map import FogOfWarMap, RESOLUTION, RADIUS
from agentlab.agent.fog_render import agent_marker, cardinal_labels, font, ROUTE

VERSION='explored_gt_bev_v3'
CONTRACT=dict(version=VERSION,privileged=True,source='inspector_clean_topdown_and_exact_pose',
    radius_m=RADIUS,reveal='360_degree_current_floor',occlusion='walls_closed_doors_furniture_bounds',
    projection='fixed_inspector_camera_crop_and_native_size',persistence='last_seen_pixels_only',
    floor_policy='visited_floors_only',excluded='unexplored_pixels_task_answers_private_ids_hidden_floor_count',
    limitations='privileged overhead appearance, not RGB autonomous mapping; 2D occlusion; no body clearance guarantee')


def source_image(geometry):
    s=geometry.get('bev_source') or {}
    if s.get('version')!='inspector_clean_bev_v1' or s.get('floor')!=geometry['floor']:
        raise ValueError('fixed BEV requires paired current-floor Inspector source')
    encoded=s.get('data_url','')
    if not encoded.startswith('data:image/png;base64,'):
        raise ValueError('clean BEV must be lossless PNG')
    raw=base64.b64decode(encoded.split(',',1)[1],validate=True)
    if hashlib.sha256(raw).hexdigest()!=s.get('sha256'):
        raise ValueError('clean BEV fingerprint mismatch')
    im=Image.open(io.BytesIO(raw)).convert('RGB')
    if im.size!=(s.get('width'),s.get('height')) or not all(64<=v<=4096 for v in im.size):
        raise ValueError('invalid clean BEV dimensions')
    c=s.get('camera') or {};p=c.get('position') or {};r=c.get('rotation') or {}
    if abs(float(r.get('x',0))-90)>.01 or abs(float(r.get('y',0))%360)>.01 or abs(float(r.get('z',0))%360)>.01:
        raise ValueError('BEV source must use north-up vertical orthographic camera')
    projection=(float(p['x']),float(p['z']),float(c.get('orthographicSize') or c.get('orthographic_size') or 0),*im.size)
    if not all(math.isfinite(v) for v in projection) or projection[2]<=0 or c.get('orthographic') is False:
        raise ValueError('invalid Inspector projection')
    return np.asarray(im),projection


class FixedFogBev(FogOfWarMap):
    version=VERSION
    contract=CONTRACT

    def filter_pixels(self, geometry, projection, mask):
        return mask

    def observe_geometry(self,geometry,observation_id,step,recent_result=None):
        start=time.perf_counter()
        if not geometry:raise ValueError('fixed BEV source missing')
        pixels,projection=source_image(geometry)
        existing=self.floors.get(int(geometry['floor']))
        if existing and existing.get('projection')!=projection:
            raise ValueError('Inspector crop/size changed during episode; fixed map cannot drift')
        super().observe_geometry(geometry,observation_id,step,recent_result)
        e=self.floors[self.current_floor]
        if 'projection' not in e:
            e['projection']=projection;e['pixel_memory']=np.zeros_like(pixels)
            e['pixel_seen']=np.zeros(pixels.shape[:2],dtype=bool)
        # Every pixel corner must fall in a revealed cell and inside the 4 m
        # circle. No raster resize/filter can blend hidden pixels into output.
        camx,camz,half_h,w,h=projection;half_w=half_h*w/h
        ox,oz=e['origin'];ax,az=geometry['pose'][:2]
        visible=e['visible'];mask=np.ones((h,w),dtype=bool)
        if visible:
            lx=min(x for x,z in visible);lz=min(z for x,z in visible)
            hx=max(x for x,z in visible);hz=max(z for x,z in visible)
            grid=np.zeros((hz-lz+1,hx-lx+1),dtype=bool)
            for x,z in visible:grid[z-lz,x-lx]=True
            for dx,dz in ((0,0),(1,0),(0,1),(1,1)):
                wx=camx-half_w+(np.arange(w)+dx)*2*half_w/w
                wz=camz+half_h-(np.arange(h)+dz)*2*half_h/h
                gx=np.floor((wx-ox)/RESOLUTION).astype(int)-lx
                gz=np.floor((wz-oz)/RESOLUTION).astype(int)-lz
                valid=(gz[:,None]>=0)&(gz[:,None]<grid.shape[0])&(gx[None,:]>=0)&(gx[None,:]<grid.shape[1])
                values=grid[np.clip(gz,0,grid.shape[0]-1)[:,None],np.clip(gx,0,grid.shape[1]-1)[None,:]]
                mask &= valid & values & ((wz[:,None]-az)**2+(wx[None,:]-ax)**2<=RADIUS**2)
        else:mask[:]=False
        mask=self.filter_pixels(geometry,projection,mask)
        e['pixel_memory'][mask]=pixels[mask]
        e['pixel_seen'] |= mask;e['pixel_visible']=mask
        self._cache=None
        elapsed=(time.perf_counter()-start)*1000
        self.total_inference_ms+=elapsed-self.last_inference_ms;self.last_inference_ms=elapsed

    def pixel(self,floor,local):
        e=self.floors[floor];camx,camz,half_h,w,h=e['projection'];half_w=half_h*w/h
        x,z=local[0]+e['origin'][0],local[1]+e['origin'][1]
        return ((x-camx+half_w)/(2*half_w)*w,(camz-z+half_h)/(2*half_h)*h)

    def base_image(self,floor):
        e=self.floors[floor];pixels=(e['pixel_memory']*.55).astype('uint8')
        if floor==self.current_floor:
            pixels[e['pixel_visible']]=e['pixel_memory'][e['pixel_visible']]
        return Image.fromarray(pixels)

    def render(self,floor):
        e=self.floors[floor];im=self.base_image(floor);d=ImageDraw.Draw(im);w,h=im.size
        # Route strokes and selectable station labels are independent. Keep
        # the actual walked polyline even when internal P points are suppressed.
        for path in e['path_segments']:
            if len(path)>1:d.line([self.pixel(floor,p) for p in path],fill=ROUTE,width=3,joint='curve')
        # Reuse the Inspector's red circle/arrow and directional-label style.
        current=floor==self.current_floor
        point=self.pixel(floor,e['pose'][:2]) if current else None
        boxes=[]
        state=self.state()
        for n in (n for n in state['nodes'] if n['floor']==floor):
            # A nearby P label is selectable and links to the semantic N card;
            # do not stack two labels at the same station.
            if self.recall_enabled and any(n['id'] in p.get('related_node_ids',[]) for p in state.get('recall_candidates',[])):continue
            px,py=self.pixel(floor,n['position_local_m'])
            if not (0<=px<w and 0<=py<h):continue
            colour={'start':'#f6d373','portal':'#61d9a7','transit':'#c5a0ff','event':'#ffb55a','note':'#e8a8e9'}[n['kind']]
            d.ellipse((px-3,py-3,px+3,py+3),fill=colour,outline='black')
            tw=d.textbbox((0,0),n['id'],font=font(12))[2]+8
            for dx,dy in ((7,-22),(7,8),(-tw-7,-22),(-tw-7,8),(7,-43),(7,29)):
                x=max(2,min(px+dx,w-tw-2));y=max(48,min(py+dy,h-58));b=(x,y,x+tw,y+19)
                if any(not(b[2]+2<c[0] or c[2]+2<b[0] or b[3]+2<c[1] or c[3]+2<b[1]) for c in boxes):continue
                if point and b[0]-14<point[0]<b[2]+14 and b[1]-14<point[1]<b[3]+14:continue
                boxes.append(b);d.line((px,py,x+tw/2,y+9),fill=colour,width=1)
                d.rounded_rectangle(b,radius=3,fill='#112033',outline=colour)
                d.text((x+4,y+2),n['id'],font=font(12),fill=colour);break
        if self.recall_enabled:
            for n in state.get('recall_candidates',[]):
                if n['floor'] != floor: continue
                px,py=self.pixel(floor,n['position_local_m'])
                d.ellipse((px-4,py-4,px+4,py+4),fill='#22d3ee',outline='black')
                tw=d.textbbox((0,0),n['id'],font=font(14))[2]+10
                for dx,dy in ((12,-23),(12,10),(-tw-12,-23),(-tw-12,10),(12,-47),(12,34),(-tw-12,34)):
                    x=max(2,min(px+dx,w-tw-2));y=max(48,min(py+dy,h-58));b=(x,y,x+tw,y+22)
                    if any(not(b[2]+3<c[0] or c[2]+3<b[0] or b[3]+3<c[1] or c[3]+3<b[1]) for c in boxes):continue
                    if point and b[0]-14<point[0]<b[2]+14 and b[1]-14<point[1]<b[3]+14:continue
                    boxes.append(b);d.line((px,py,x+tw/2,y+11),fill='#22d3ee',width=1)
                    d.rounded_rectangle(b,radius=3,fill='#04232b',outline='#22d3ee')
                    d.text((x+5,y+2),n['id'],font=font(14),fill='#67e8f9');break
        if current:agent_marker(d,*point,e['pose'][2],length=min(w,h)*.07,radius=min(w,h)*.02)
        cardinal_labels(d,(0,0,w,h),size=max(16,w//26),arrow=max(14,min(w,h)//22))
        label=f'F{floor} '+('当前层' if current else '历史层')
        if current:label+=f" · {e['pose'][2]%360:.0f}°"
        d.text((9,9),label,font=font(14),fill='white',stroke_width=2,stroke_fill='#10151d')
        return im

    def state(self):
        s=super().state();s.update(version=self.version,assistance=copy.deepcopy(self.contract),
            legend='black unknown; original colours currently revealed; dimmed last-seen pixels; red lines are actually walked routes, sparse selectable keypoints; no straight teleport connectors; red position/body heading and N/E/S/W; node meanings in text',
            layout='fixed Inspector camera, crop, scale and native dimensions; no auto recenter or zoom; node details outside image')
        for f in s['floors']:
            e=self.floors[f['floor']]
            if 'projection' in e:f['image_size']=list(e['projection'][-2:])
        for n in s['nodes']:
            if 'projection' in self.floors[n['floor']]:
                n['image_pixel']=[round(v,1) for v in self.pixel(n['floor'],n['position_local_m'])]
        for n in s.get('recall_candidates', []):
            from agentlab.agent.map_nodes import same_observed_area
            n['related_node_ids']=[v['id'] for v in s['nodes'] if v['floor']==n['floor']
                and math.dist(v['position_local_m'],n['position_local_m'])<.85
                and same_observed_area(self.floors[n['floor']],v['position_local_m'],n['position_local_m'])]
            if 'projection' in self.floors[n['floor']]:
                n['image_pixel']=[round(v,1) for v in self.pixel(n['floor'],n['position_local_m'])]
        return s

    def context(self):
        if self._cache is None:
            images=[]
            for floor in sorted(self.floors,key=lambda f:(f!=self.current_floor,f)):
                im=self.render(floor);buf=io.BytesIO();im.save(buf,format='PNG')
                images.append(dict(floor=floor,current=floor==self.current_floor,width=im.width,height=im.height,
                    data_url='data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode()))
            self._cache=dict(version=self.version,state=self.state(),images=images,image=images[0] if images else None)
        return self._cache
