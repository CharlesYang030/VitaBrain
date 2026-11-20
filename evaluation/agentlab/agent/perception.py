"""RGB-only floor estimation. No engine imports, requests, depth or scene assets.

The static Kinova camera calibration comes from InitializeBody (robot design):
90 degree vertical FOV, capsule half height .9m + camera offset .675m.
Projection assumes a level supporting floor; it is explicitly an estimate.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2] / 'models' / 'segformer-b2-ade20k'
DEPTH_ROOT = ROOT.parent / 'depth-anything-v2-metric-small'
CALIBRATION = dict(version='kinova_nominal_camera_v1', vertical_fov_deg=90.,
    camera_height_m=1.575, body_radius_m=.2, source='robot_design_InitializeBody',
    assumption='standing_on_level_support_plane; no scene height or depth queried')


class FloorPerception:
    _session = None
    _depth_session = None
    _lock = threading.Lock()

    @classmethod
    def ready(cls):
        if cls._session is None:
            with cls._lock:
                if cls._session is None:
                    import onnxruntime as ort
                    manifest = json.loads((ROOT / 'manifest.json').read_text())
                    for name, info in manifest['files'].items():
                        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != info['sha256']:
                            raise ValueError('Perception model fingerprint mismatch: ' + name)
                    opts = ort.SessionOptions()
                    opts.intra_op_num_threads = 2
                    opts.inter_op_num_threads = 1
                    cls._session = ort.InferenceSession(str(ROOT / 'model.onnx'), opts,
                        providers=['CPUExecutionProvider'])
                    depth_manifest=json.loads((DEPTH_ROOT/'manifest.json').read_text())
                    for name, info in depth_manifest['files'].items():
                        if hashlib.sha256((DEPTH_ROOT/name).read_bytes()).hexdigest()!=info['sha256']:
                            raise ValueError('Depth estimator fingerprint mismatch: '+name)
                    cls._depth_session=ort.InferenceSession(str(DEPTH_ROOT/'model.onnx'),opts,
                        providers=['CPUExecutionProvider'])
        return cls._session

    def __init__(self):
        self.ready()
        self.identity = dict(semantic=json.loads((ROOT / 'manifest.json').read_text()),
                            rgb_depth=json.loads((DEPTH_ROOT/'manifest.json').read_text()),
                            fusion='horizontal_plane_and_RGB_region_consensus_v1')
        labels = json.loads((ROOT / 'config.json').read_text())['id2label']
        self.floor_ids = [int(k) for k, v in labels.items() if v.strip() in ('floor', 'rug')]
        self.furniture_ids=[int(k) for k,v in labels.items() if v.strip() in
            ('bed','table','chair','sofa','cabinet','desk','counter','shelf','armchair','person','ceiling')]
        self.last_hash = None
        self.last = None

    def infer(self, data_url, pitch=0.):
        raw = base64.b64decode(data_url.split(',', 1)[1], validate=True)
        sha = hashlib.sha256(raw).hexdigest()
        if (sha,pitch) == self.last_hash:
            return {**self.last, 'inference_ms': 0., 'cached_rgb': True}
        started = time.perf_counter()
        image = Image.open(io.BytesIO(raw)).convert('RGB')
        arr = np.asarray(image.resize((512, 512), Image.Resampling.BILINEAR), np.float32) / 255
        arr = (arr - np.array([.485, .456, .406], np.float32)) / np.array([.229, .224, .225], np.float32)
        with self._lock:
            logits = self._session.run(None, {'pixel_values': arr.transpose(2, 0, 1)[None]})[0][0]
        exp = np.exp(logits - logits.max(0, keepdims=True))
        probabilities = exp / exp.sum(0, keepdims=True)
        probability = probabilities[self.floor_ids].sum(0)
        # The depth estimator sees the SAME RGB, never a simulator depth image.
        depth_input=np.asarray(image.resize((518,518),Image.Resampling.BICUBIC),np.float32)/255
        depth_input=(depth_input-np.array([.485,.456,.406],np.float32))/np.array([.229,.224,.225],np.float32)
        with self._lock:
            depth=self._depth_session.run(None,{'pixel_values':depth_input.transpose(2,0,1)[None]})[0].squeeze()
        depth=cv2.GaussianBlur(cv2.resize(depth,(128,128)),(5,5),0)
        v,u=np.mgrid[0:128,0:128];rx=((u+.5)/64-1)*(image.width/image.height);ry=1-(v+.5)/64
        points=np.stack((rx*depth,ry*depth,depth),-1)
        normals=np.cross(np.gradient(points,axis=0),np.gradient(points,axis=1))
        angle=math.radians(pitch)
        heights=-(points[:,:,1]*math.cos(angle)-points[:,:,2]*math.sin(angle))
        vertical=np.abs(normals[:,:,1]*math.cos(angle)-normals[:,:,2]*math.sin(angle))/(np.linalg.norm(normals,axis=-1)+1e-8)
        candidates=(vertical>.85)&(heights>.65)&(heights<3.5)&(v>64)
        floor_height=float(np.median(heights[candidates])) if candidates.sum()>=60 else None
        plane=(np.abs(heights-floor_height)<.16*floor_height)&(vertical>.70)&(heights>0) if floor_height else np.zeros((128,128),bool)
        furniture=probabilities[self.furniture_ids].sum(0)>.65
        seeds=((probability>.65)|plane)&~furniture
        # Expand only within image-supported colour regions containing positive
        # geometric/semantic seeds. No room geometry or authored colour constants.
        from skimage.segmentation import felzenszwalb
        small=np.asarray(image.resize((128,128),Image.Resampling.BILINEAR))
        regions=felzenszwalb(small,scale=80,sigma=.5,min_size=45,channel_axis=-1)
        mask=np.zeros((128,128),np.uint8)
        for region in np.unique(regions):
            pixels=regions==region
            if (seeds&pixels).sum() >= max(8,.08*pixels.sum()):
                mask[pixels&~furniture]=1
        # Every admitted pixel must project below the camera onto its support plane.
        ray_vertical=ry*math.cos(angle)-math.sin(angle)
        mask[ray_vertical>=-.08]=0
        # Remove uncertain pixel edges, not fill unseen space.
        interior = cv2.erode(mask, np.ones((3, 3), np.uint8))
        boundary = mask - interior
        boundary[:2] = boundary[-2:] = 0
        boundary[:, :2] = boundary[:, -2:] = 0
        self.last = dict(floor=interior, boundary=boundary, probability=probability,
            width=image.width, height=image.height, source_rgb_sha256=sha,
            inference_ms=round((time.perf_counter() - started) * 1000, 2), cached_rgb=False,
            estimated_plane_height=floor_height)
        self.last_hash = (sha,pitch)
        return self.last


def pixel_to_ground(x, y, pitch_deg, aspect=1.):
    if not all(type(v) in (float, int) and math.isfinite(v) for v in (x, y, pitch_deg)):
        return None
    if not (0 <= x <= 1 and 0 <= y <= 1):
        return None
    p = math.radians(pitch_deg)
    tangent = math.tan(math.radians(CALIBRATION['vertical_fov_deg']) / 2)
    rx, ry = (2 * x - 1) * tangent * aspect, (1 - 2 * y) * tangent
    vertical, forward = ry * math.cos(p) - math.sin(p), ry * math.sin(p) + math.cos(p)
    if vertical >= -.08 or forward <= 0:
        return None
    scale = -CALIBRATION['camera_height_m'] / vertical
    point = (rx * scale, forward * scale)
    return point if .25 <= math.hypot(*point) <= 8 else None


def world_to_image(dx, dz, heading_deg, pitch_deg, aspect=1.):
    h, p = math.radians(heading_deg), math.radians(pitch_deg)
    right = dx * math.cos(h) - dz * math.sin(h)
    forward = dx * math.sin(h) + dz * math.cos(h)
    height = CALIBRATION['camera_height_m']
    cy = -height * math.cos(p) + forward * math.sin(p)
    cz = height * math.sin(p) + forward * math.cos(p)
    focal = .5 / math.tan(math.radians(CALIBRATION['vertical_fov_deg']) / 2)
    safe = np.maximum(cz, .01)
    return .5 + focal * right / safe / aspect, .5 - focal * cy / safe, cz
