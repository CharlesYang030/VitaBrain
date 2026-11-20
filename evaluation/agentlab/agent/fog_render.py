"""North-up BEV: cardinal directions, body heading, red route and memory index."""
import math
from functools import lru_cache
from PIL import Image, ImageDraw, ImageFont
from agentlab.agent.map_nodes import heading_label, KINDS

WIDTH,HEIGHT=1600,1050
ROUTE='#f34f59'
# Match scenegen.topdown's Inspector annotations (geometry stays fog-masked).
AGENT='#ff2828'
POSITION='#ff3232'
CARDINAL='#dc3232'


@lru_cache(maxsize=24)
def font(size):
    for path in ('/System/Library/Fonts/STHeiti Light.ttc','/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        try:return ImageFont.truetype(path,size)
        except OSError:pass
    return ImageFont.load_default()


def heading_triangle(px,py,heading):
    a=math.radians(heading);forward=(math.sin(a),-math.cos(a));right=(math.cos(a),math.sin(a))
    return [(px+forward[0]*24,py+forward[1]*24),
            (px+forward[0]*14.4+right[0]*7.2,py+forward[1]*14.4+right[1]*7.2),
            (px+forward[0]*14.4-right[0]*7.2,py+forward[1]*14.4-right[1]*7.2)]


def agent_marker(d,px,py,heading,length=56,radius=16):
    d.ellipse((px-radius-2,py-radius-2,px+radius+2,py+radius+2),fill='black')
    d.ellipse((px-radius,py-radius,px+radius,py+radius),fill=POSITION)
    tri=[(px+(x-px)*length/24,py+(y-py)*length/24) for x,y in heading_triangle(px,py,heading)]
    d.line(((px,py),tri[0]),fill=AGENT,width=3)
    d.polygon(tri,fill=AGENT)


def cardinal_labels(d,bounds,size=24,arrow=22,stroke=2):
    """Inspector-style red edge triangles and white-outlined bilingual labels."""
    x0,y0,x1,y1=bounds;cx=(x0+x1)/2;cy=(y0+y1)/2
    for where,x,y,zh,en in (('n',cx,y0+8,'北','N'),('s',cx,y1-8,'南','S'),
                            ('e',x1-8,cy,'东','E'),('w',x0+8,cy,'西','W')):
        dx,dy={'n':(0,-1),'s':(0,1),'e':(1,0),'w':(-1,0)}[where]
        d.polygon(((x,y),(x-dx*arrow-dy*arrow,y-dy*arrow+dx*arrow),
                   (x-dx*arrow+dy*arrow,y-dy*arrow-dx*arrow)),fill=CARDINAL)
        label=f'{zh} {en}';bbox=d.textbbox((0,0),label,font=font(size));tw=bbox[2]-bbox[0];th=bbox[3]-bbox[1]
        if where=='n':tx,ty=x-tw/2,y+arrow+3
        elif where=='s':tx,ty=x-tw/2,y-arrow-th-8
        elif where=='e':tx,ty=x-arrow-tw-5,y-th/2-2
        else:tx,ty=x+arrow+5,y-th/2-2
        d.text((tx,ty),label,font=font(size),fill=CARDINAL,stroke_width=stroke,stroke_fill='white')


def wrap(text, width, size=18, lines=2):
    out=[];row='';f=font(size)
    for ch in str(text).replace('\n',' '):
        if f.getlength(row+ch)>width:
            out.append(row);row=ch
            if len(out)==lines:return out[:-1]+[out[-1][:-1]+'…']
        else:row+=ch
    if row:out.append(row)
    return out[:lines]


def render_map(mapper,floor,show_recall=False):
    e=mapper.floors[floor];current=floor==mapper.current_floor;res=.1
    im=Image.new('RGB',(WIDTH,HEIGHT),'#0b1321');d=ImageDraw.Draw(im)
    def text(x,y,s,size=20,color='#e9f1fa'):d.text((x,y),s,font=font(size),fill=color)
    text(32,23,f'F{floor}  ·  '+('当前楼层 / CURRENT FLOOR' if current else '已访问楼层 / MEMORY'),32)
    text(32,72,'探索式 GT · 4m 遮挡揭雾 · 北向固定 · 特权地图辅助',20,'#9fb8cf')
    d.rounded_rectangle((1102,24,1570,267),radius=14,fill='#162438',outline='#344962',width=1)
    text(1123,42,'方位与朝向',23)
    cx,cy=1205,155
    cardinal_labels(d,(1112,75,1298,233),size=16,arrow=7,stroke=1)
    if current:
        h=e['pose'][2]%360
        agent_marker(d,cx,cy,h,length=30,radius=8)
        text(1340,116,heading_label(h),23,AGENT);text(1340,153,f'{h:.0f}°',29,AGENT)
        text(1123,239,'红点：当前位置；红色箭头：智能体身体朝向',15,'#a8becf')
    else:text(1328,127,'历史地图',23,'#acb8c7');text(1123,239,'智能体目前在其他楼层；此图不显示当前位姿',16,'#a8becf')
    d.rounded_rectangle((28,117,1076,910),radius=14,outline='#314256',width=1)
    keys=list(e['cells']) or [(0,0)]
    minx,maxx=min(k[0] for k in keys)-8,max(k[0] for k in keys)+8
    minz,maxz=min(k[1] for k in keys)-8,max(k[1] for k in keys)+8
    scale=min(927/(maxx-minx+1),670/(maxz-minz+1),14)
    left=552-(maxx-minx+1)*scale/2;top=514-(maxz-minz+1)*scale/2
    def screen(x,z):return left+(x/res-minx)*scale,top+(maxz+1-z/res)*scale
    bright={1:'#d0dee8',2:'#f2f4f8',3:'#788ca4',4:'#ffac50',5:'#55c69a',6:'#ad9bc7'}
    muted={1:'#4c6276',2:'#8f9ba9',3:'#344357',4:'#996337',5:'#33715f',6:'#584e70'}
    for (gx,gz),(kind,_) in e['cells'].items():
        px,py=left+(gx-minx)*scale,top+(maxz-gz)*scale
        color=(bright if current and (gx,gz) in e['visible'] else muted)[kind]
        d.rectangle((px,py,px+scale+.1,py+scale+.1),fill=color)
        if kind==6 and (gx+gz)%4==0:d.line((px,py+scale,px+scale,py),fill='#3b2d59',width=1)
    for path in e.get('path_segments',[e['path']]):
        if len(path)>1:
            line=[screen(*p) for p in path]
            d.line(line,fill='#6f222e',width=5,joint='curve');d.line(line,fill=ROUTE,width=3,joint='curve')
    cardinal_labels(d,(28,117,1076,910))
    selected=[n for n in mapper.node_book.state(mapper.current_floor)['nodes'] if n['floor']==floor]
    colors={'start':'#f6d373','portal':'#61d9a7','transit':'#c5a0ff','event':'#ffb55a','note':'#e8a8e9'}
    label_boxes=[]
    agent_point=screen(*e['pose'][:2]) if current else None
    for n in selected:
        px,py=screen(*n['position_local_m']);color=colors[n['kind']]
        d.ellipse((px-6,py-6,px+6,py+6),fill=color,outline='#0d1824',width=2)
        label=n['id'];tw=d.textbbox((0,0),label,font=font(17))[2]+14
        for dx,dy in ((12,-29),(12,12),(-tw-12,-29),(-tw-12,12),(12,-57),(12,39)):
            tx=max(103,min(px+dx,984-tw));ty=max(174,min(py+dy,844))
            box=(tx,ty,tx+tw,ty+26)
            if any(not(box[2]+3<b[0] or b[2]+3<box[0] or box[3]+3<b[1] or b[3]+3<box[1]) for b in label_boxes):continue
            if agent_point and box[0]-20<agent_point[0]<box[2]+20 and box[1]-24<agent_point[1]<box[3]+24:continue
            label_boxes.append(box);d.line((px,py,tx+tw/2,ty+13),fill=color,width=1)
            d.rounded_rectangle(box,radius=4,fill='#15253b',outline=color)
            text(tx+7,ty+2,label,17,color);break
    if current:
        px,py=agent_point
        agent_marker(d,px,py,e['pose'][2])
    if show_recall:
        for n in mapper.recall_candidates():
            if n['floor'] != floor: continue
            px,py=screen(*n['position_local_m'])
            d.ellipse((px-9,py-9,px+9,py+9),fill='#22d3ee',outline='#06131d',width=2)
            text(px+11,py-10,n['id'],16,'#67e8f9')
    text(1115,284,'关键节点 · 对应地图编号',24)
    text(1115,321,f'本层 {len(selected)} 个索引；下列展示优先 8 个',16,'#a8becf')
    for i,n in enumerate(selected[:8]):
        yy=353+i*73
        d.rounded_rectangle((1109,yy,1571,yy+66),radius=7,fill='#152236')
        color=colors[n['kind']]
        title=f"{n['id']}  {n['label']}"
        if n['kind']=='portal':title+=' · '+n.get('last_observed_state','')
        text(1121,yy+6,title,19,color)
        refs=[f for f in n['memory_refs'] if f['status']=='active']
        if refs:
            f=refs[-1];detail=('推测' if f['origin']=='agent_inference' else '公开回执')+': '+f['content']
        elif n['public_events']:
            event=n['public_events'][-1]
            detail=('成功：' if event['success'] else '未成功：')+event['message']
        elif n['memory_refs']:detail='旧记忆已撤回/被替代；不再视为有效结论'
        else:detail=f"最近观察：步 {n['last_seen_step']} · {n['source_observation_ids'][-1]}"
        for j,line in enumerate(wrap(detail,435,15,2)):text(1121,yy+30+j*16,line,15,'#c6d4e3')
    d.line((56,931,56+scale/res,931),fill='white',width=3);text(56,945,'1 米',17)
    d.line((237,940,300,940),fill=ROUTE,width=4);text(314,928,'已走路线',20)
    text(475,928,'亮色：当前可见   暗色：旧记忆   黑色：未知',19,'#c7d7e5')
    text(34,986,'编号不是操作 handle。笔记/事件节点标示当时站位，不是物体坐标；推测不等于已确认。',20,'#bccbd9')
    text(34,1017,f"观察 {e['observation_id']} · 橙色关闭门 / 绿色门槛 / 紫纹高差 · 地图不保证身体净空",16,'#89a8c2')
    return im
