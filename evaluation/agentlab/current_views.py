"""Portable image-label helper extracted from the project's shared view module.

This helper labels supplied images; it does not capture or synthesize scene views.
"""
import base64
import hashlib
import io

import numpy as np
from PIL import Image, ImageDraw, ImageFont

VERSION = 'left_front_right_v1'
VIEWS = (('left', -90, '左 LEFT'), ('front', 0, '正 FRONT'), ('right', 90, '右 RIGHT'))


def _font(size):
    for path in ('/System/Library/Fonts/STHeiti Light.ttc',
                 '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def labelled_view(pixels, view_id):
    offset, label = next((d, l) for k, d, l in VIEWS if k == view_id)
    im = Image.fromarray(np.asarray(pixels)).convert('RGB')
    font = _font(max(18, round(im.width * .046)))
    text = f'{label}  {offset:+d}°' if offset else label + '  0°'
    draw = ImageDraw.Draw(im)
    box = draw.textbbox((0, 0), text, font=font, stroke_width=1)
    rect = [8, 8, min(im.width - 1, box[2] - box[0] + 24), min(im.height - 1, box[3] - box[1] + 24)]
    draw.rectangle(rect, fill='#101318')
    draw.text((16, 15 - box[1]), text, font=font, fill='#ff3030', stroke_width=1, stroke_fill='#ffe4e4')
    buffer = io.BytesIO()
    im.save(buffer, format='PNG')
    raw = buffer.getvalue()
    return dict(view_id=view_id, label=label, yaw_offset_degrees=offset,
                width=im.width, height=im.height, annotation_rect_px=rect,
                sha256=hashlib.sha256(raw).hexdigest(),
                data_url='data:image/png;base64,' + base64.b64encode(raw).decode())
