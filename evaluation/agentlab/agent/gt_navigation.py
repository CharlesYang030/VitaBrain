"""Second model response for GT-assisted visible-floor waypoint selection."""
import json
import math

VERSION='visible_gt_waypoint_v2_triplet'
SELECTOR_PROMPT='''你正在执行自己刚选择的导航子请求。绿色填充及亮绿边框是后端用当前左、正、右三视图RGB、配对深度与真值身体净空核验的可选地面；不是任务线索或正确方向。先选择view_id=left/front/right，再在该图绿色区域内部选一点，不选墙、家具、人物、图例或边界。坐标按所选视图的完整图片归一化，左上(0,0)，右下(1,1)，不是网页坐标。结合导航目的选有意义的较远站位；不要为缩短路径越过看不见的通道。后端做投影和A*规划，真实逐段行走，遇阻停止，不开门、不换层。半径上限以输入radius_m为准；远处未标绿时可先选沿途合适的绿色点，不能点击未标绿区域。无合适点可取消。
只输出JSON：{"view_id":"front","x":0.5,"y":0.8,"reason":"该点如何服务当前目的"}。取消时输出{"cancel":true,"reason":"原因"}。本次画面的绑定由框架负责，无需抄写内部票据。此窗口不输出普通action、计划或记忆，不执行交互。'''


def selection_messages(prepared, parsed, oid):
    info={k:prepared[k] for k in ('version','floor','selectable_pixels','radius_m')}
    info.update(observation_id=oid,navigation_intent=parsed['gt_navigation_request']['intent'],
                current_goal=parsed.get('current_goal',''))
    content=[{'type':'text','text':json.dumps(info,ensure_ascii=False)}]
    rendering=prepared.get('selection_rendering')
    if rendering and rendering.get('version')=='navigation_self_occlusion_removed_v1':
        content.append({'type':'text','text':'本选点窗口的左、正、右三图暂时隐藏了机械臂和当前手持物的渲染，深度图同步重采样；持物仍在手中、状态不变。墙、门和家具仍正常遮挡。可选择露出的绿色地面；普通操作观察恢复原样。'})
    views=prepared.get('views')
    if views:
        for view in views:
            content.extend([{'type':'text','text':json.dumps({k:view[k] for k in ('view_id','yaw_offset_degrees','selectable_pixels')},ensure_ascii=False)},
                {'type':'image_url','image_url':{'url':view['image']['data_url']}}])
    else:  # Legacy audit fixtures; live bridge requires the triplet protocol.
        content.append({'type':'image_url','image_url':{'url':prepared['image']['data_url']}})
    return [{'role':'system','content':SELECTOR_PROMPT},{'role':'user','content':content}]



def validate_selection(value,ticket,view_ids=None):
    if not isinstance(value,dict) or ('ticket' in value and value['ticket']!=ticket):
        raise ValueError('waypoint selection requires the current ticket')
    # The response belongs to this exact prepared request. Bind its private
    # nonce here; asking the model to transcribe a random UUID adds no useful
    # grounding evidence. Explicit stale legacy tickets are still rejected.
    value={'ticket':ticket,**value}
    if value.get('cancel') is True:
        if set(value)!={'ticket','cancel','reason'}:raise ValueError('invalid cancel schema')
    else:
        expected={'ticket','x','y','reason'} | ({'view_id'} if view_ids is not None else set())
        if set(value)!=expected:raise ValueError('waypoint requires ticket,view_id,x,y,reason for triplet navigation')
        if view_ids is not None and value.get('view_id') not in view_ids:raise ValueError('invalid view_id')
        if any(type(value[k]) not in (int,float) or not math.isfinite(value[k]) or not 0<=value[k]<1 for k in ('x','y')):
            raise ValueError('waypoint pixel outside normalized image')
    if not isinstance(value.get('reason'),str) or not 0<len(value['reason'])<=500:
        raise ValueError('waypoint requires a short reason')
    return value
