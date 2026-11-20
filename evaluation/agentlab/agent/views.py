"""Public camera contract. No world poses, segmentation or observer cameras."""
import base64
import hashlib

VERSION='left_front_right_v1'
SPECS=(('left',-90,'左 LEFT'),('front',0,'正 FRONT'),('right',90,'右 RIGHT'))


def public_views(raw, *, verify=False):
    if not isinstance(raw,dict) or raw.get('version')!=VERSION:
        raise ValueError('missing left/front/right camera contract')
    rows=raw.get('views')
    if (not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows)
            or [r.get('view_id') for r in rows]!=[s[0] for s in SPECS]):
        raise ValueError('current views must be exactly left, front, right in order')
    views=[]
    for row,(view_id,yaw,label) in zip(rows,SPECS):
        if row.get('yaw_offset_degrees')!=yaw:
            raise ValueError('current camera direction mismatch')
        w,h=row.get('width'),row.get('height');rect=row.get('annotation_rect_px');url=row.get('data_url','')
        if (type(w) is not int or type(h) is not int or min(w,h)<16
                or not isinstance(url,str) or not url.startswith(('data:image/png;base64,','data:image/jpeg;base64,'))
                or not isinstance(rect,list) or len(rect)!=4
                or any(type(v) is not int for v in rect)
                or not 0<=rect[0]<rect[2]<w or not 0<=rect[1]<rect[3]<h):
            raise ValueError('invalid camera image or annotation rectangle')
        if verify:
            import io
            from PIL import Image
            raw_image=base64.b64decode(url.split(',',1)[1],validate=True)
            actual=hashlib.sha256(raw_image).hexdigest()
            if actual!=row.get('sha256'):raise ValueError('current view fingerprint mismatch')
            try:
                with Image.open(io.BytesIO(raw_image)) as im:
                    if im.size!=(w,h):raise ValueError('actual image dimensions mismatch')
                    im.verify()
            except OSError as error:
                raise ValueError('current view is not a valid image') from error
        views.append(dict(view_id=view_id,yaw_offset_degrees=yaw,label=label,width=w,height=h,
            data_url=url,sha256=row.get('sha256'),annotation_rect_px=list(rect)))
    if len({(v['width'],v['height']) for v in views})!=1:
        raise ValueError('three camera sizes must match')
    return dict(version=VERSION,views=views)


def current_image_parts(observation):
    triplet=observation.get('current_views')
    if not triplet:return []
    import json
    parts=[]
    for view in triplet['views']:
        caption=dict(view_id=view['view_id'],label=view['label'],yaw_offset_degrees=view['yaw_offset_degrees'],
            observation_id=observation['observation_id'],step=observation.get('step'),
            image_sha256=view['sha256'],annotation_rect_px=view['annotation_rect_px'],
            role='current_view',rule=('当前对象操作使用此正视图的像素/handle；红色标签区域不可作为对象目标。'
                if view['view_id']=='front' else '当前侧视图用于观察与定位；操作其中的物体前，先转向使它进入新的正视图。'))
        parts.extend([dict(type='text',text='CURRENT_'+view['view_id'].upper()+'_FPV\n'+json.dumps(caption,ensure_ascii=False)),
            dict(type='image_url',image_url={'url':view['data_url']})])
    return parts
