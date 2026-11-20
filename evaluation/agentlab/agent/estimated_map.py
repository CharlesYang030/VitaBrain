"""Online free-space belief map from public RGB, never the inspector GT map."""
from __future__ import annotations

import base64
import io
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from agentlab.agent.perception import CALIBRATION, FloorPerception, pixel_to_ground, world_to_image
from agentlab.agent.spatial import OnlineSpatialMemory
from agentlab.domain import public_observation

VERSION = 'public_rgb_bev_v3'
RESOLUTION = .10


class EstimatedSpatialMemory(OnlineSpatialMemory):
    version = VERSION

    def __init__(self, mode, *, perception=None):
        super().__init__(mode)
        self.version = VERSION
        self.perception = perception or FloorPerception()
        self.grids = {}
        self.places = []
        self.landmarks = []
        self.place_links = []
        self.current_place = None
        self.current_frame = None
        self.pitch = 0.
        self.last_inference_ms = 0.
        self.inference_calls = 0
        self.total_inference_ms = 0.
        self._rendered = None
        self.current_boundaries = set()

    def observe(self, raw, step, receipt=None):
        obs = public_observation(raw, self.mode)
        if obs['observation_id'] in self.seen:
            return
        old_chart = self.charts[-1]['id'] if self.charts else None
        super().observe(obs, step, receipt)
        self._rendered = None
        self.pitch = (obs.get('ego_state') or {}).get('camera_pitch_deg', 0.)
        if not self.last_pose:
            return
        chart = self.charts[-1]['id']
        if chart != old_chart:
            self.current_place = None
        data = (obs.get('rgb') or {}).get('data_url')
        if not data:
            return
        try:
            self.current_frame = self.perception.infer(data, pitch=self.pitch)
        except (ValueError, OSError, IndexError):
            self.current_frame = None
            return
        frame = self.current_frame
        self.last_inference_ms = frame['inference_ms']
        self.inference_calls += int(not frame.get('cached_rgb'))
        self.total_inference_ms += self.last_inference_ms
        grid = self.grids.setdefault(chart, {})
        self.current_boundaries = set()
        pose = self.last_pose
        cx, cz = round(pose['x'] / RESOLUTION), round(pose['z'] / RESOLUTION)
        gx, gz = np.meshgrid(np.arange(cx-80, cx+81), np.arange(cz-80, cz+81))
        dx, dz = gx * RESOLUTION - pose['x'], gz * RESOLUTION - pose['z']
        u, v, depth = world_to_image(dx, dz, pose['heading_deg'], self.pitch, frame['width']/frame['height'])
        sh, sw = frame['floor'].shape
        valid = (depth > .25) & (u > .02) & (u < .98) & (v > .02) & (v < .98) & (dx*dx+dz*dz <= 64)
        ix, iy = np.clip((u*sw).astype(int), 0, sw-1), np.clip((v*sh).astype(int), 0, sh-1)
        floor = valid & (frame['floor'][iy, ix] > 0)
        edge = valid & (frame['boundary'][iy, ix] > 0)
        for mask, value in ((floor, 1), (edge, -1)):
            for x, z in zip(gx[mask].tolist(), gz[mask].tolist()):
                old=grid.get((x,z),(0,0,None))[0]
                score=max(-6,min(6,old+(2 if value>0 else -1)))
                grid[(x, z)] = (score, step, obs['observation_id'])
                if value<0:
                    self.current_boundaries.add((x,z))
        # Never manufacture floor area by widening odometry tracks.
        self.cached = None

    def ground_point(self, x, y, *, require_floor=True):
        if not self.current_frame or not self.last_pose:
            return None
        frame = self.current_frame
        point = pixel_to_ground(x, y, self.pitch, frame['width']/frame['height'])
        if point is None:
            return None
        if require_floor:
            h, w = frame['floor'].shape
            if frame['floor'][min(h-1, int(y*h)), min(w-1, int(x*w))] == 0:
                return None
        a = math.radians(self.last_pose['heading_deg'])
        rx, rz = point
        return (self.last_pose['x'] + rx*math.cos(a)+rz*math.sin(a),
                self.last_pose['z'] - rx*math.sin(a)+rz*math.cos(a))

    def update_semantics(self, decision, oid, step):
        if not self.last_pose or oid != self.last_pose['source_observation_id']:
            return []
        accepted = []
        chart = self.charts[-1]['id']
        update = decision.get('place_update')
        if isinstance(update, dict) and update.get('source_observation_id') == oid:
            confidence = update.get('confidence', .5)
            label = str(update.get('label', '')).strip()[:100]
            if label and type(confidence) in (float, int) and 0 <= confidence <= 1:
                ident = update.get('id', 'new')
                node = next((p for p in self.places if p['id'] == ident and p['chart'] == chart), None)
                if node is None and ident in ('new', '', None):
                    node = dict(id=f'R{len(self.places)+1}', chart=chart,
                        x=self.last_pose['x'], z=self.last_pose['z'], first_step=step)
                    self.places.append(node)
                if node:
                    node.update(label=label, confidence=confidence, source_observation_id=oid,
                        origin='agent_visual_inference', last_step=step)
                    if self.current_place and self.current_place != node['id']:
                        link = dict(source=self.current_place, destination=node['id'], chart=chart,
                            source_observation_id=oid, origin='agent_place_transition_inference')
                        if not any((e['source'], e['destination']) == (link['source'], link['destination']) for e in self.place_links):
                            self.place_links.append(link)
                    self.current_place = node['id']; accepted.append(dict(node))
        for entry in (decision.get('visual_landmarks') or [])[:4]:
            if not isinstance(entry, dict) or entry.get('source_observation_id') != oid:
                continue
            point = self.ground_point(entry.get('x'), entry.get('y'))
            label = str(entry.get('label', ''))[:80]
            confidence = entry.get('confidence', .5)
            if not point or not label or entry.get('kind') not in ('portal', 'landmark') or type(confidence) not in (float, int) or not 0 <= confidence <= 1:
                continue
            existing = next((n for n in self.landmarks if n['chart'] == chart and n['label'] == label
                and math.hypot(n['x']-point[0], n['z']-point[1]) < .8), None)
            node = existing or dict(id=f'L{len(self.landmarks)+1}')
            node.update(chart=chart, label=label, kind=entry['kind'], x=round(point[0], 2), z=round(point[1], 2),
                confidence=confidence, source_observation_id=oid, origin='agent_visual_inference',
                uncertainty_m=max(.3, math.hypot(point[0]-self.last_pose['x'], point[1]-self.last_pose['z'])*.15))
            if existing is None:
                self.landmarks.append(node)
            accepted.append(dict(node))
        self._rendered = None
        return accepted

    def state(self):
        return dict(version=self.version, calibration=CALIBRATION,
            current_pose=self.last_pose, current_chart=self.charts[-1]['id'] if self.charts else None,
            current_place=self.current_place, charts=[dict(id=c['id'], floor=c['floor'],
                explored_floor_m2=round(sum(v[0] > 0 for v in self.grids.get(c['id'], {}).values())*RESOLUTION**2, 2)) for c in self.charts],
            places=self.places[-60:], landmarks=self.landmarks[-60:], place_links=self.place_links[-60:],
            transit_links=self.links[-20:], provenance='public_RGB_and_action_odometry_only',
            uncertainty='nominal_camera_flat_floor_projection; semantic_errors_and_odometry_drift_possible',
            inference=dict(model=self.perception.identity, calls=self.inference_calls,
                total_ms=round(self.total_inference_ms, 2), last_ms=self.last_inference_ms,
                current_floor_pixel_fraction=round(float(self.current_frame['floor'].mean()),4) if self.current_frame else 0.))

    def context(self):
        png = self.render()
        state=self.state()
        # Weight fingerprints stay in the audit/state; the model needs the map,
        # uncertainty and sources, not repeated binary-file metadata every step.
        state['inference']={k:v for k,v in state['inference'].items() if k!='model'}
        state['inference']['source']='fixed_RGB_semantic_and_monocular_depth_estimators'
        state['recent_collision_poses']=[dict(p,chart=c['id']) for c in self.charts for p in c['blocked']][-8:]
        return dict(state=state, image=dict(data_url='data:image/png;base64,'+base64.b64encode(png).decode(),
                    width=1200, height=800), version=self.version)

    def render(self):
        if self._rendered:
            return self._rendered
        image = Image.new('RGB', (1200, 800), '#f4f6fa')
        draw = ImageDraw.Draw(image)
        font_path = '/System/Library/Fonts/STHeiti Light.ttc'
        font = ImageFont.truetype(font_path, 17) if Path(font_path).exists() else ImageFont.load_default()
        big = ImageFont.truetype(font_path, 24) if Path(font_path).exists() else font
        draw.text((24, 14), '在线视觉地图 · RGB 估计 · 非 GT', fill='#152f44', font=big)
        draw.text((24, 48), '浅色：估计地面   橙色：边界估计   灰色：未知   青线：实际轨迹   ▲：当前位置', fill='#334b60', font=font)
        chart = self.charts[-1] if self.charts else None
        if chart and self.last_pose:
            grid = self.grids.get(chart['id'], {})
            points = chart['points']
            coords = [(x*RESOLUTION, z*RESOLUTION) for x, z in grid] + [(p['x'], p['z']) for p in points]
            xs, zs = list(zip(*coords)) if coords else ([0], [0])
            center = ((min(xs)+max(xs))/2, (min(zs)+max(zs))/2)
            span = max(8, max(xs)-min(xs)+2, max(zs)-min(zs)+2)
            self._panel(image, (24, 116, 714, 690), center, span, chart, grid, font)
            self._panel(image, (742, 116, 1176, 550), (self.last_pose['x'], self.last_pose['z']), 8, chart, grid, font)
            draw = ImageDraw.Draw(image)
            draw.text((24, 84), f"楼层概览  {chart['id']} · 楼层 {chart['floor'] or '?'}", fill='#17354b', font=font)
            draw.text((742, 84), '局部通道 · 固定 8 米范围', fill='#17354b', font=font)
            nodes = [n for n in self.places+self.landmarks if n['chart'] == chart['id']][-7:]
            for i, node in enumerate(nodes):
                draw.text((744, 568+i*24), f"{node['id']}  {node['label'][:20]}", fill='#273b50', font=font)
            draw.text((24, 713), '图上空白保留未知；未画完整房间。入口和地点名称由智能体观察判断。', fill='#334b60', font=font)
            draw.text((24, 744), '地面估计不保证身体可通过。换层局部图未经重定位不得直接拼成完整平面图。', fill='#334b60', font=font)
        else:
            draw.text((40, 150), '缺少公开里程计，地图不可用。', fill='#334b60', font=big)
        stream = io.BytesIO(); image.save(stream, format='PNG')
        self._rendered = stream.getvalue()
        return self._rendered

    def _panel(self, image, box, center, span, chart, grid, font):
        l, t, r, b = box
        size = min(r-l, b-t); scale=size/span
        x0, z1 = center[0]-span/2, center[1]+span/2
        canvas = Image.new('RGB', (r-l, b-t), '#d7dfe7')
        d = ImageDraw.Draw(canvas)
        def pixel(x, z): return ((x-x0)*scale+(r-l-size)/2, (z1-z)*scale+(b-t-size)/2)
        for (x, z), (value, step, oid) in grid.items():
            if value==0: continue
            px, py = pixel(x*RESOLUTION, z*RESOLUTION)
            if -5 <= px <= r-l+5 and -5 <= py <= b-t+5:
                radius=max(.6, RESOLUTION*scale/2)
                d.rectangle((px-radius, py-radius, px+radius, py+radius), fill='#ffffff' if value > 0 else '#d3914a')
        pts = [pixel(p['x'], p['z']) for p in chart['points']]
        if len(pts)>1: d.line(pts, fill='#2d8c98', width=2)
        for node in self.places+self.landmarks:
            if node['chart'] != chart['id']: continue
            px, py = pixel(node['x'], node['z'])
            if 0<=px<r-l and 0<=py<b-t:
                d.ellipse((px-4, py-4, px+4, py+4), fill='#7c4bbb')
                d.text((px+5, py-14), node['id'], font=font, fill='#643196')
        pose=self.last_pose; px, py=pixel(pose['x'],pose['z']); a=math.radians(pose['heading_deg'])
        d.polygon([(px+15*math.sin(a),py-15*math.cos(a)),(px+10*math.sin(a+2.5),py-10*math.cos(a+2.5)),
                   (px+10*math.sin(a-2.5),py-10*math.cos(a-2.5))],fill='#d33f39')
        d.line((15,b-t-20,15+scale,b-t-20),fill='#30495c',width=3)
        d.text((15,b-t-48),'1 m',font=font,fill='#30495c')
        image.paste(canvas,(l,t))
