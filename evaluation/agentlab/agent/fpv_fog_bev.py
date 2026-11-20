"""v4 keeps v3's fixed canvas but only accumulates first-person visible cells."""
import base64
import hashlib
import io

import numpy as np
from PIL import Image

from agentlab.agent.fog_bev import FixedFogBev, CONTRACT
from agentlab.agent.fog_map import RESOLUTION

VERSION = 'explored_gt_bev_v4'


class FirstPersonFogBev(FixedFogBev):
    version = VERSION
    contract = dict(CONTRACT, version=VERSION,
        reveal='current_fpv_visible_surfaces_current_floor',
        occlusion='paired_fpv_metric_depth_and_conservative_structural_mask',
        visibility_resolution_m=.05,
        limitations='privileged overhead appearance of visible footprints, not RGB autonomous mapping; conservative surface mask; no body clearance guarantee')

    def observe_geometry(self, geometry, observation_id, step, recent_result=None):
        # Validate before any memory mutation. No silent fallback to radial fog.
        s=(geometry or {}).get('fpv_visibility') or {}
        if (s.get('version')!='fpv_surface_visibility_v1'
                or s.get('floor')!=geometry.get('floor')
                or not s.get('frame_rgb_sha256')
                or s['frame_rgb_sha256']!=geometry.get('frame_rgb_sha256')):
            raise ValueError('first-person visibility missing or not paired with current FPV')
        encoded=s.get('data_url','')
        if not encoded.startswith('data:image/png;base64,'):
            raise ValueError('visibility must be lossless PNG')
        raw=base64.b64decode(encoded.split(',',1)[1],validate=True)
        if hashlib.sha256(raw).hexdigest()!=s.get('sha256'):
            raise ValueError('visibility fingerprint mismatch')
        im=Image.open(io.BytesIO(raw)).convert('L')
        origin=np.asarray(s.get('origin_xz_m'),dtype=float)
        if (im.size!=(s.get('width'),s.get('height')) or not all(1<=n<=512 for n in im.size)
                or origin.shape!=(2,) or not np.isfinite(origin).all()
                or s.get('resolution_m')!=.05):
            raise ValueError('invalid visibility raster')
        self._fpv_mask=np.asarray(im)==255
        self._fpv_origin=origin
        try:
            super().observe_geometry(geometry,observation_id,step,recent_result)
        finally:
            self._fpv_mask=None
            self._fpv_origin=None

    def _seen(self,x,z):
        gx=np.floor((np.asarray(x)-self._fpv_origin[0])/.05).astype(int)
        gz=np.floor((np.asarray(z)-self._fpv_origin[1])/.05).astype(int)
        h,w=self._fpv_mask.shape
        valid=(gx>=0)&(gx<w)&(gz>=0)&(gz<h)
        return valid & self._fpv_mask[np.clip(gz,0,h-1),np.clip(gx,0,w-1)]

    def filter_visible(self,geometry,entry,visible):
        ox,oz=entry['origin']
        # Cells/nodes are filtered BEFORE they enter persistent memory.
        return {(x,z) for x,z in visible if self._seen(
            ox+(x+.5)*RESOLUTION,oz+(z+.5)*RESOLUTION)}

    def filter_pixels(self,geometry,projection,mask):
        camx,camz,half_h,w,h=projection;half_w=half_h*w/h
        for dx,dz in ((0,0),(1,0),(0,1),(1,1)):
            wx=camx-half_w+(np.arange(w)+dx)*2*half_w/w
            wz=camz+half_h-(np.arange(h)+dz)*2*half_h/h
            mask &= self._seen(wx[None,:],wz[:,None])
        return mask


class ThreeViewFogBev(FirstPersonFogBev):
    version='explored_gt_bev_v5'
    contract=dict(FirstPersonFogBev.contract,version=version,
        reveal='union_of_current_left_front_right_visible_surfaces_current_floor',
        view_protocol='left_front_right_v1',view_yaw_offsets_degrees=[-90,0,90],
        occlusion='independent_paired_depth_per_delivered_view_and_conservative_structural_mask')

    def observe_geometry(self,geometry,observation_id,step,recent_result=None):
        s=(geometry or {}).get('fpv_visibility') or {};sources=(geometry or {}).get('view_image_sha256') or {}
        if (s.get('view_protocol')!='left_front_right_v1' or set(sources)!={'left','front','right'}
                or s.get('source_view_sha256')!=sources):
            raise ValueError('three-view fog requires visibility paired to all delivered camera images')
        super().observe_geometry(geometry,observation_id,step,recent_result)
