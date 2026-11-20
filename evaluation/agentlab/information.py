"""The model-facing information contract; independent of game truth storage."""
from __future__ import annotations

from agentlab.domain import InterfaceMode

VERSION = 'information_boundary_v2'
FEEDBACK_VERSION = 'interaction_feedback_v1'

# Closed vocabulary: author-written references/names are not category labels.
LABELS = {
    'Apple': '苹果', 'Bowl': '碗', 'Box': '储物箱', 'Safe': '保险箱',
    'Door': '门', 'Doorway': '门', 'Drawer': '抽屉', 'Cabinet': '柜子',
    'Fridge': '冰箱', 'Television': '电视', 'CellPhone': '手机',
    'DiningTable': '餐桌', 'CoffeeTable': '茶几', 'Desk': '桌子',
    'CounterTop': '台面', 'Shelf': '搁板', 'ShelvingUnit': '置物架',
    'Sofa': '沙发', 'Chair': '椅子', 'Bed': '床', 'Lamp': '灯',
    'DeskLamp': '台灯', 'FloorLamp': '落地灯', 'LightSwitch': '开关',
    'Book': '书', 'Newspaper': '报纸', 'Paper': '纸张', 'Pen': '笔',
    'Pencil': '铅笔', 'KeyChain': '钥匙扣', 'Key': '钥匙', 'Mug': '杯子',
    'Cup': '杯子', 'Plate': '盘子', 'Fork': '叉子', 'Spoon': '勺子',
    'Knife': '刀', 'ButterKnife': '餐刀', 'Pot': '锅', 'Pan': '平底锅',
    'Bottle': '瓶子', 'Vase': '花瓶', 'Tomato': '番茄', 'Potato': '土豆',
    'Lettuce': '生菜', 'Egg': '鸡蛋', 'Bread': '面包', 'SaltShaker': '盐罐',
    'PepperShaker': '胡椒罐', 'Kettle': '水壶', 'Microwave': '微波炉',
    'Toaster': '烤面包机', 'CoffeeMachine': '咖啡机', 'Sink': '水槽',
    'SinkBasin': '水槽', 'Faucet': '水龙头', 'GarbageCan': '垃圾桶',
    'RemoteControl': '遥控器', 'Laptop': '笔记本电脑', 'Computer': '电脑',
    'Desktop': '电脑', 'Painting': '画', 'Window': '窗户', 'Mirror': '镜子',
    'HousePlant': '盆栽', 'SprayBottle': '喷壶', 'SoapBottle': '皂液瓶',
    'SoapBar': '肥皂', 'Towel': '毛巾', 'ToiletPaper': '卫生纸',
    'AlarmClock': '闹钟', 'Watch': '手表', 'CreditCard': '卡片',
}

FEEDBACK = {
    'succeeded': '操作已完成。',
    'movement_stopped': '移动提前停止；实际位移见 actual.distance_m，不能按请求距离记账。请观察障碍与当前位置，再调整路径。',
    'dynamic_obstacle_blocked': '移动被当前动态实体阻挡，已停止；实际位移见 actual.distance_m。可观察或等待其离开，也可改道；不要原样重复前进。',
    'released_at_hand': '已松开手中物体，手已空；物体从手的位置自由释放。这不是指定桌面放置，落点和支撑面需用当前画面确认。',
    'floor_changed': '换层已完成；实际到达楼层见 actual.floor。请根据到达后的画面确认朝向和出口。',
    'already_at_requested_floor': '当前已经位于你请求的楼层，无需再次换层。',
    'stairs_entry_required': '尚未进入楼梯使用区域。请先从楼梯间门口走入，再请求换层。',
    'elevator_entry_required': '尚未进入电梯使用区域。请先从电梯间门口走入，再请求换层。',
    'stairs_adjacent_only': '楼梯一次只能前往相邻楼层，请检查请求的楼层。',
    'floor_unavailable': '请求的楼层不可用，请根据现场楼层标识选择。',
    'floor_transition_failed': '此次换层未完成，请重新观察现场状态后再决定操作。',
    'action_rejected': '动作未完成，请根据当前画面检查目标状态。',
    'grasp_failed': '夹爪未能抓住所指物体；请检查抓取路径的遮挡，并调整接近方向或位置后再试。',
    'grasped_transport_blocked': '物体已抓住，但机械臂收拢受到阻挡，暂不保证可以携带移动。若退离或转向仍受阻，可先放下，再从不同位置接近抓取。',
    'push_blocked': '所指物体沿推动方向受到障碍阻挡，未能移动；请改变接近方向或选择其他通路。',
    'out_of_reach': '未能触及所指目标，请先靠近。',
    'not_facing': '当前朝向无法完成操作，请调整朝向。',
    'not_visible': '目标当前被遮挡或不在视野中，请重新观察。',
    'unsupported_action': '所指目标不支持这个动作。',
    'locked_code': '锁扣未打开，需要输入正确密码。',
    'locked_key': '锁扣未打开，需要相应的解锁条件。',
    'wrong_code': '输入的密码被拒绝，锁仍然关闭。',
    'unlock_condition': '解锁未成功，所需条件尚未满足。',
    'already_open': '目标已经打开。', 'already_closed': '目标已经关闭。',
    'closed_container': '容器仍然关闭，无法读取或操作其中的内容。',
    'not_readable': '此处当前没有可读取的内容。',
    'screen_off': '屏幕尚未开启，请先开启屏幕再切换画面。',
    'screen_on': '屏幕已开启，请观察画面。',
    'screen_already_on': '屏幕已经开启。',
    'screen_already_off': '屏幕已经关闭。',
    'screen_turned_off': '屏幕已关闭。',
    'screen_cycled': '屏幕画面已切换，请观察当前图像。',
    'screen_rejected': '屏幕未响应这次操作，请检查当前状态并靠近。',
    'hand_empty': '手中没有物体。', 'hand_occupied': '手中已有物体，请先放下。',
    'no_space': '目标位置没有可容纳手持物的空位。',
    'collision': '运动受到阻挡，请观察周围并调整路径。',
    'arm_clearance_blocked': '机械臂姿态或夹爪受到阻挡，动作未完成；此类碰撞不扣血、不计入主躯干连续碰撞。请调整接近方向或路线。',
    'arm_dynamic_obstacle_blocked': '机械臂或当前持物与动态实体的通行空间冲突，已停止；不计入主躯干连续碰撞、不因此扣血。请先改变方向或退让，原地等待未必能让出通道。',
    'look_limit': '相机已到俯仰限位，请调整观察方向。',
    'no_ready_question': '当前没有等待回答的问题。',
    'answer_rejected': '回答未被接受，请根据已获得的信息重新判断。',
    'answer_accepted': '回答已被接受。',
    'callback_not_ready': '当前没有可回应的来电请求。',
    'read_content': '读取到的文字：', 'npc_utterance': '对方回复：',
    'target_miss': '该像素未选中物体，请依据当前图像重新指定目标。',
    'view_label_region': '所指像素位于红色视图标识区域；请指向当前正视图中的实际物体像素。',
    'water_unavailable': '请手持可装水容器，并靠近正在出水的水源。',
    'already_filled_with_water': '当前手持容器已装有水，无需重复接水。',
    'water_filled': '当前手持容器已成功装水。',
    'invalid_target': '目标引用无效，请使用当前观察中的目标。',
}


def contract(mode: InterfaceMode) -> dict:
    return {'version': VERSION, 'interface_mode': mode.value,
            'object_assistance': 'visible_doors_and_current_operables' if mode == InterfaceMode.ASSISTED else 'none',
            'target_reference': 'handle' if mode == InterfaceMode.ASSISTED else 'pixel',
            'feedback': FEEDBACK_VERSION, 'read_policy': 'active_text',
            'task_guidance': 'evaluator_only', 'proprioception': 'odometry_and_grip_occupancy',
            'memory': 'public_events_durable_diverse_retrieval_v2',
            'behavior_support': 'public_history_warnings_v1',
            'wayfinding': 'physical_floor_and_residential_signs_v2',
            'offscreen_handles': 'held_object_only', 'grounding_ids': 'server_private'}


def render_feedback(payload: dict, verb: str) -> str:
    if not isinstance(payload, dict) or payload.get('version') != FEEDBACK_VERSION:
        raise ValueError('missing or incompatible public feedback contract')
    code = payload.get('code')
    if code not in FEEDBACK:
        raise ValueError('unknown public feedback code')
    text = FEEDBACK[code]
    if code in ('read_content', 'npc_utterance'):
        if (code == 'read_content' and verb != 'read') or (
                code == 'npc_utterance' and verb not in ('ask_npc', 'answer_npc')):
            raise ValueError('content disclosure does not match the performed action')
        content = payload.get('content')
        if not isinstance(content, str) or not content:
            raise ValueError('public content missing')
        text += content
    elif 'content' in payload:
        raise ValueError('unexpected free text in public feedback')
    return text
