"""Allowlisted, front-RGB-bound optional depth sensor contract."""
import base64
import hashlib
import io
from PIL import Image

VERSION = 'front_metric_depth_v1'
FIELDS = ('version','view_id','units','scale_min_m','scale_max_m','measurement',
          'viewport_width','viewport_height','width','height','source_rgb_sha256','sha256','data_url')


def public_depth(raw, front, *, verify=False):
    if not isinstance(raw,dict) or not isinstance(front,dict):
        raise ValueError('missing current front depth contract')
    if (raw.get('version')!=VERSION or raw.get('view_id')!='front' or raw.get('units')!='m'
            or raw.get('scale_min_m')!=0 or raw.get('scale_max_m')!=6
            or raw.get('measurement')!='native_camera_depth_not_walkable_distance'
            or not front.get('sha256') or raw.get('source_rgb_sha256')!=front['sha256']
            or raw.get('viewport_width')!=front['width'] or raw.get('viewport_height')!=front['height']
            or raw.get('width')!=front['width'] or raw.get('height')!=front['height']+112
            or not str(raw.get('data_url','')).startswith('data:image/png;base64,')):
        raise ValueError('depth scale, viewport or source RGB fingerprint mismatch')
    if verify:
        data=base64.b64decode(raw['data_url'].split(',',1)[1],validate=True)
        if hashlib.sha256(data).hexdigest()!=raw.get('sha256'):
            raise ValueError('depth image fingerprint mismatch')
        with Image.open(io.BytesIO(data)) as im:
            if im.size!=(raw['width'],raw['height']):raise ValueError('depth dimensions mismatch')
            im.verify()
    return {k:raw[k] for k in FIELDS}


def depth_parts(observation):
    import json
    depth=observation.get('front_depth')
    if not depth:return []
    caption={k:v for k,v in depth.items() if k!='data_url'}
    caption.update(observation_id=observation['observation_id'],
        rule='当前正视图同帧、同像素视口；下方追加固定米制图例。6+表示至少6米，灰色未知。相机深度不是可通行距离；对象仍在RGB正视图选取。')
    return [dict(type='text',text='CURRENT_FRONT_DEPTH\n'+json.dumps(caption,ensure_ascii=False)),
            dict(type='image_url',image_url={'url':depth['data_url']})]
