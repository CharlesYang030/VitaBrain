"""An online, agent-owned map. No engine, depth, object truth or observer views.

Solid tracks come from public odometry. Semantic positions are explicitly
model estimates from FPV, not sensor measurements. Transit starts a new local
chart because a floor macro does not report its displacement/rotation.
"""
from __future__ import annotations

import base64
import hashlib
import io
import math
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from agentlab.domain import InterfaceMode, public_observation

VERSION = 'public_online_bev_v2'
PROMPT = """
在线空间记忆已开启。图片依次是当前第一视角 FPV、你自己的在线 BEV。
BEV 实线仅表示成功走过的轨迹，不表示整片区域可通行；空白是未知。
BEV 的紫色 V 编号与右侧的历史第一视角关键帧对应，这些画面是你已经看过的真实
观察，不是新观察。地图上的紫色短箭头记录当时的朝向。把位置、朝向和画面结合，
判断哪个门口已经去过、从哪里转向、怎样回到之前的视角。右侧历史画面不可用于
当前动作的像素指代；当前目标必须仍以第一张当前 FPV 或本轮 handle 为准。
红叉仅表示曾在该姿态遇阻，不是障碍物精确位置。虚线圆及编号是你
先前从图像估计的地标，可能错误或过时，不能替代当前 FPV 的碰撞判断。
地图向上为该局部图的 +z，向右为 +x；角度 0 朝 +z，90 朝 +x。
每次换层另开局部图，通过公开换层回执连接。不同图的坐标不能直接相减，
同一楼层再次进入也不自动合并；里程计可能有漂移。
换层后新图的 (0,0) 是到达换层出口时的位置；transit_links 保存出发/到达点。
先结合图上走过的路线与地标决定探索方向，再用当前 FPV 接近、读字、操作。
地图编号不是动作目标；对象操作仍严格使用当前帧的 handle 或像素。
可在输出 JSON 增加 spatial_updates 数组（最多 4 项，无新信息时 []）：
{"id":"新地标留空，修正已有地标填写其编号", "label":"简短名称或视觉特征",
 "kind":"landmark|obstacle|portal|region", "bearing_deg":0,
 "distance_m":2.0, "uncertainty_m":1.0, "confidence":0.6,
 "source_observation_id":"当前 observation_id"}
bearing_deg 是你从当前 FPV 判断的水平相对朝向，正为右、负为左，范围 -90 到 90；
distance_m 是你估计的水平距离（0.2 到 12 米），不是环境提供的深度。
只记录当前画面确实看见的东西；看不出距离就不填该项，不要把文字提示的位置
画成已看见的地标。uncertainty_m 表示位置误差半径，至少 0.5 米；辨认不确定时增大它。
同一个物体再次看见可用 id 修正估计；已移动的东西需要重新观察，不能沿用旧坐标。
优先记录门口、电视、桌子等稳定地标。若当前观察证明已有地标位置失效，
可提交 {"id":"已有编号","remove":true,"source_observation_id":"当前 observation_id"} 撤销它。
地图内容全是记忆和推断，不是新的任务提示，也不能证明任务已经完成。
"""


def _number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


class OnlineSpatialMemory:
    def __init__(self, mode: InterfaceMode):
        self.version = VERSION
        self.mode = mode
        self.charts: list[dict] = []
        self.links: list[dict] = []
        self.seen: set[str] = set()
        self.counter = 0
        self.visual_counter = 0
        self.last_pose: dict | None = None
        self.cached: dict | None = None

    def new_chart(self, floor=None):
        self.charts.append({'id': f'C{len(self.charts) + 1}', 'floor': floor,
                            'origin': None, 'points': [], 'blocked': [], 'landmarks': [],
                            'keyframes': []})
        self.cached = None

    def observe(self, raw: dict, step: int, receipt: dict | None = None):
        obs = public_observation(raw, self.mode)
        oid = obs.get('observation_id')
        if not oid or oid in self.seen:
            return
        self.seen.add(oid)
        if not self.charts:
            self.new_chart()
        receipt = receipt or {}
        action = receipt.get('action') or {}
        verb = action.get('verb')
        floor = (receipt.get('actual') or {}).get('floor')
        if (verb in ('use_stairs', 'use_elevator') and receipt.get('success') is True
                and receipt.get('feedback_code') == 'floor_changed' and type(floor) is int):
            previous = self.charts[-1]
            self.new_chart(floor)
            self.links.append({'from': previous['id'], 'to': self.charts[-1]['id'],
                               'via': verb, 'arrived_floor': floor,
                               'departure_pose': dict(self.last_pose) if self.last_pose else None,
                               'arrival_pose': {'x': 0, 'z': 0, 'heading_deg': 0},
                               'source_observation_id': oid, 'step': step})
        elif receipt.get('feedback_code') == 'already_at_requested_floor':
            confirmed = (receipt.get('requested') or {}).get('floor')
            if type(confirmed) is int:
                self.charts[-1]['floor'] = confirmed
        ego = obs.get('ego_state') or {}
        p = ego.get('position_relative_spawn_m') or {}
        x, z, heading = (_number(p.get('x')), _number(p.get('z')),
                         _number(ego.get('heading_relative_spawn_deg')))
        if x is None or z is None or heading is None:
            self.last_pose = None
            self.cached = None
            return
        chart = self.charts[-1]
        if chart['origin'] is None:
            chart['origin'] = [x, z, heading]
        ox, oz, oh = chart['origin']
        # Reset orientation at macro boundaries, not a hidden world transform.
        angle = math.radians(oh)
        local_x = (x - ox) * math.cos(angle) - (z - oz) * math.sin(angle)
        local_z = (x - ox) * math.sin(angle) + (z - oz) * math.cos(angle)
        self.last_pose = {'x': round(local_x, 3), 'z': round(local_z, 3),
                          'heading_deg': round((heading - oh) % 360, 2),
                          'step': step, 'source_observation_id': oid}
        # Preserve turns, revisits and observation provenance; bounded per run.
        chart['points'].append(dict(self.last_pose))
        chart['points'] = chart['points'][-2001:]
        if receipt.get('success') is False and receipt.get('feedback_code') == 'collision':
            chart['blocked'].append({**self.last_pose, 'verb': verb})
            chart['blocked'] = chart['blocked'][-100:]
        self._capture_keyframe(obs, chart, receipt)
        self.cached = None

    @staticmethod
    def _angle(a, b):
        return abs((a - b + 180) % 360 - 180)

    def _capture_keyframe(self, obs, chart, receipt):
        """Deterministic visual memory of delivered FPV, not semantic inference."""
        pose = self.last_pose
        frames = chart['keyframes']
        previous = frames[-1] if frames else None
        if previous:
            distance = math.hypot(pose['x']-previous['x'], pose['z']-previous['z'])
            turned = self._angle(pose['heading_deg'], previous['heading_deg'])
            # Keep sparse, diverse views. Pure waiting does not add duplicates.
            if distance < 1.2 and turned < 55 and receipt.get('feedback_code') != 'collision':
                return
            if pose['step'] - previous['step'] < 2:
                return
        url = (obs.get('rgb') or {}).get('data_url')
        if not isinstance(url, str) or not url.startswith(('data:image/jpeg;base64,', 'data:image/png;base64,')) or len(url) > 3_000_000:
            return
        try:
            encoded = base64.b64decode(url.split(',', 1)[1], validate=True)
            with Image.open(io.BytesIO(encoded)) as original:
                if original.width > 4096 or original.height > 4096:
                    return
                thumb = original.convert('RGB')
                thumb.thumbnail((200, 200))
                buffer = io.BytesIO()
                thumb.save(buffer, format='JPEG', quality=85)
        except (ValueError, OSError, Image.DecompressionBombError):
            return
        self.visual_counter += 1
        frames.append({**pose, 'id': f'V{self.visual_counter}', 'chart': chart['id'],
                       'origin': 'public_fpv_keyframe',
                       'thumbnail': base64.b64encode(buffer.getvalue()).decode(),
                       'source_rgb_sha256': hashlib.sha256(encoded).hexdigest()})
        if len(frames) > 96:
            del frames[1]  # retain the chart entrance and recent visual memory

    def visual_context(self):
        if not self.charts or not self.last_pose:
            return []
        candidates = [f for f in self.charts[-1]['keyframes']
                      if f['source_observation_id'] != self.last_pose['source_observation_id']]
        if not candidates:
            return []
        chosen = [candidates[-1]]
        # On revisits, show an older nearby viewpoint in addition to the latest.
        revisit = [f for f in candidates if self.last_pose['step'] - f['step'] >= 6
                   and math.hypot(f['x']-self.last_pose['x'], f['z']-self.last_pose['z']) <= 2]
        if revisit:
            match = min(revisit, key=lambda f: math.hypot(f['x']-self.last_pose['x'], f['z']-self.last_pose['z']))
            if match not in chosen:
                chosen.append(match)
        if candidates[0] not in chosen:
            chosen.append(candidates[0])
        # Prefer another location/orientation over a nearly duplicate view.
        while len(chosen) < 3 and len(chosen) < len(candidates):
            rest = [f for f in candidates if f not in chosen]
            chosen.append(max(rest, key=lambda f: min(math.hypot(f['x']-c['x'], f['z']-c['z'])
                + self._angle(f['heading_deg'], c['heading_deg'])/90 for c in chosen)))
        return sorted(chosen[:3], key=lambda f: f['step'])

    def apply_updates(self, updates: Any, observation_id: str, step: int) -> list[dict]:
        accepted = []
        if (not isinstance(updates, list) or not self.last_pose
                or self.last_pose['source_observation_id'] != observation_id):
            return accepted
        chart = self.charts[-1]
        for update in updates[:4]:
            if not isinstance(update, dict) or update.get('source_observation_id') != observation_id:
                continue
            if update.get('remove') is True:
                old = next((v for v in chart['landmarks'] if v['id'] == update.get('id')), None)
                if old is not None:
                    chart['landmarks'].remove(old)
                    accepted.append({'id': old['id'], 'removed': True, 'origin': 'agent_inference',
                                     'source_observation_id': observation_id, 'step': step})
                continue
            label = update.get('label')
            kind = update.get('kind', 'landmark')
            bearing, distance, error, confidence = [_number(update.get(k)) for k in
                ('bearing_deg', 'distance_m', 'uncertainty_m', 'confidence')]
            if (not isinstance(label, str) or not label.strip()
                    or kind not in ('landmark', 'obstacle', 'portal', 'region')
                    or None in (bearing, distance, error, confidence)
                    or not -90 <= bearing <= 90 or not .2 <= distance <= 12
                    or not .5 <= error <= 12 or not 0 < confidence <= 1):
                continue
            requested_id = update.get('id')
            existing = next((v for v in chart['landmarks'] if v['id'] == requested_id), None)
            if requested_id and existing is None:
                continue  # never join unrelated local frames by a claimed id
            if existing is None:
                self.counter += 1
            angle = math.radians(self.last_pose['heading_deg'] + bearing)
            item = {'id': existing['id'] if existing else f'L{self.counter}',
                    'label': label.strip()[:60], 'kind': kind,
                    'x': round(self.last_pose['x'] + distance * math.sin(angle), 2),
                    'z': round(self.last_pose['z'] + distance * math.cos(angle), 2),
                    'uncertainty_m': max(error, round(distance * .15, 2)),
                    'confidence': confidence, 'origin': 'agent_inference',
                    'source_observation_id': observation_id, 'step': step}
            if existing:
                chart['landmarks'].remove(existing)
            chart['landmarks'].append(item)
            chart['landmarks'] = chart['landmarks'][-32:]
            accepted.append(item)
        self.cached = None
        return accepted

    def state(self):
        return {'version': VERSION, 'charts': self.charts, 'links': self.links,
                'current_pose': self.last_pose, 'source': 'public_odometry_and_agent_fpv_inference'}

    def context(self):
        if self.cached is not None:
            return self.cached
        chart = self.charts[-1] if self.charts else None
        state = {'version': VERSION, 'current_chart': chart['id'] if chart else None,
                 'current_floor': chart['floor'] if chart else None,
                 'current_pose': self.last_pose,
                 'landmarks': chart['landmarks'] if chart else [],
                 'visual_keyframes': [{k:v for k,v in f.items() if k != 'thumbnail'}
                                      for f in self.visual_context()],
                 'charts': [{'id': c['id'], 'floor': c['floor'],
                             'observations': len(c['points']),
                             'visual_keyframe_count': len(c['keyframes']),
                             'landmarks': [{'id': v['id'], 'label': v['label']} for v in c['landmarks']]}
                            for c in self.charts[-16:]],
                 'transit_links': self.links[-24:],
                 'limits': 'Unknown is blank; tracks are not free-space polygons; dashed landmarks are uncertain model estimates; local charts are not globally aligned.'}
        frame = self.render()
        self.cached = {'state': state, 'image': {'data_url': 'data:image/png;base64,' +
            base64.b64encode(frame).decode(), 'sha256': hashlib.sha256(frame).hexdigest()}}
        return self.cached

    def render(self) -> bytes:
        im = Image.new('RGB', (880, 640), '#101b2a')
        d = ImageDraw.Draw(im)
        font = ImageFont.load_default(size=15)
        small = ImageFont.load_default(size=12)
        chart = self.charts[-1] if self.charts else None
        title = f"ONLINE BEV | {chart['id']} | floor {chart['floor'] or '?'}" if chart else 'ONLINE BEV'
        d.text((18, 12), title, font=font, fill='white')
        d.text((18, 36), 'PUBLIC ODOMETRY + YOUR VISUAL ESTIMATES | UNKNOWN IS BLANK', font=small, fill='#b2c7df')
        if chart and self.last_pose:
            points = chart['points']
            entries = points + chart['landmarks'] + self.visual_context()
            xs, zs = [v['x'] for v in entries], [v['z'] for v in entries]
            span = max(8., max(xs) - min(xs) + 3, max(zs) - min(zs) + 3)
            cx, cz = (max(xs) + min(xs)) / 2, (max(zs) + min(zs)) / 2
            scale = 490 / span
            def p(x, z):
                return (320 + (x - cx) * scale, 310 - (z - cz) * scale)
            grid = 1 if span <= 24 else 5
            for n in range(math.floor((cx-span/2)/grid), math.ceil((cx+span/2)/grid)+1):
                x, _ = p(n*grid, cz)
                if 75 <= x <= 565:
                    d.line([(x, 65), (x, 555)], fill='#223047')
            for n in range(math.floor((cz-span/2)/grid), math.ceil((cz+span/2)/grid)+1):
                _, y = p(cx, n*grid)
                if 65 <= y <= 555:
                    d.line([(75, y), (565, y)], fill='#223047')
            if len(points) > 1:
                d.line([p(v['x'], v['z']) for v in points], fill='#50c9a0', width=3)
            for i, v in enumerate(points):
                if i % 10 == 0:
                    xy = p(v['x'], v['z'])
                    d.ellipse((xy[0]-2, xy[1]-2, xy[0]+2, xy[1]+2), fill='#b4ffdf')
                    d.text(xy, str(v['step']), font=small, fill='#b4ffdf')
            for v in chart['landmarks']:
                x, y = p(v['x'], v['z'])
                radius = min(120, max(6, v['uncertainty_m']*scale))
                for a in range(0, 360, 30):
                    d.arc((x-radius, y-radius, x+radius, y+radius), a, a+15, fill='#e6bb68', width=1)
                d.text((x+3, y+3), v['id']+'?', font=font, fill='#ffe1a0')
            for v in chart['blocked']:
                x, y = p(v['x'], v['z'])
                d.line((x-5, y-5, x+5, y+5), fill='#ff7b79', width=2)
                d.line((x-5, y+5, x+5, y-5), fill='#ff7b79', width=2)
            for j, v in enumerate(self.visual_context()):
                vx, vy = p(v['x'], v['z'])
                a = math.radians(v['heading_deg'])
                d.ellipse((vx-5, vy-5, vx+5, vy+5), fill='#d3a3ff')
                d.line((vx, vy, vx+18*math.sin(a), vy-18*math.cos(a)), fill='#d3a3ff', width=2)
                d.text((vx+7, vy-16-14*j), v['id'], font=font, fill='#e7c8ff')
            x, y = p(self.last_pose['x'], self.last_pose['z'])
            a = math.radians(self.last_pose['heading_deg'])
            d.ellipse((x-6, y-6, x+6, y+6), fill='#6abaff')
            d.line((x, y, x+25*math.sin(a), y-25*math.cos(a)), fill='white', width=4)
            d.text((18, 566), f'Grid {grid}m | +z UP / +x RIGHT | YOU: blue arrow | steps: green', font=small, fill='#c4d8ef')
        else:
            d.text((80, 290), 'No public odometry available. Map unavailable.', font=font, fill='white')
        d.text((18, 592), 'Green: walked path   Red X: blocked pose   L?: uncertain landmark', font=small, fill='#c4d8ef')
        d.text((18, 614), 'Landmark names, sources and uncertainty are in spatial_memory text.', font=small, fill='#c4d8ef')
        d.line((642, 8, 642, 632), fill='#415773')
        d.text((656, 12), 'PAST FPV / VISUAL MEMORY', font=small, fill='#e7c8ff')
        d.text((656, 32), 'Purple V = map pose + facing', font=small, fill='#c4d8ef')
        frames = self.visual_context()
        for i, v in enumerate(frames):
            top = 56 + i * 188
            d.text((656, top), f"{v['id']} | step {v['step']} | past view", font=font, fill='#e7c8ff')
            thumb = Image.open(io.BytesIO(base64.b64decode(v['thumbnail'])))
            thumb.thumbnail((156, 156))
            im.paste(thumb, (680, top+22))
        if not frames:
            d.text((656, 95), 'No earlier keyframe yet.', font=small, fill='#c4d8ef')
        d.text((656, 624), 'Use current FPV for actions.', font=small, fill='#c4d8ef')
        stream = io.BytesIO()
        im.save(stream, format='PNG')
        return stream.getvalue()
