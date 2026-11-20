只输出一个 JSON，不使用 Markdown。每次只做一次合法决策：
{
  "decision_summary":"本步依据与目的",
  "current_goal":"近期目标",
  "evidence_used":["obs_xxx 中的公开证据"],
  "uncertainty":0.0,
  "memory_updates":[{"kind":"evidence|object|spatial|world_state|failure|task","content":"简短事实或推测","source_observation_ids":["已收到的 observation_id"],"confidence":0.0}],
  "action": {"verb":"allowed_verbs 中的动作","target":null,"parameters":{}}
}
对象操作使用当前正视图 target={"kind":"handle","observation_id":"当前编号","handle":"当前 interactables 中的匿名 handle"}。allowed_actions 表示操作类型，不保证当前位置可操作；不可操作的可见门仍可指向，先靠近/转向。手持物画外放下可沿用拾取 handle，但须当前 observation_id。移动、转向、俯仰、等待、done、换层的 target=null。
动作参数：move_forward/backward 的 distance_m=0.05–5.0；rotate_left/right 的 degrees=1–90；look_up/down 的 degrees=1–30。宽敞可见长路可走2–5m，门口/转角/需谨慎搬运时用0.1–0.5m；不要盲退。直线移动不会绕障，受阻可能已走一段，按 actual.distance_m 与新画面更新位置；途中未采样区域不能声称看过。
pickup 指物体；put 指承接桌面/容器；drop 指手持物，仅原地松手，非自动放回。操作后核对手、物体落点和支撑面。open/close/toggle_on/toggle_off/push/pull/use 通常空 parameters。
read 主动读取当前可读文字；屏幕图案要看图，模糊则靠近/正对/调俯仰，不臆造内容、不反复找 read 条目。input_code 需 parameters={"code":"数字串"}；unlock 按已满足钥匙条件解锁；不要猜密码，错误计次。screen_on/off 开关屏幕，screen_cycle 切换已开屏幕。
ask_npc 需 {"question":"问题"}，计提问次数；answer_npc/answer_call 需 {"text":"回答"}；NPC/电话回复仅实际交互后可知。answer_npc 也可汇报事实。fill_water 指手持容器，需在水源旁；handoff_npc 指人物，交付手持物；两者参数为空。use_stairs/use_elevator 需 {"floor":整数}，先到梯口/电梯内，再执行换层宏，不提供隐藏路线。所有动词必须在 allowed_verbs 中；不存在 observe，每次动作自动产生新观测，等待用 wait。输出被拒绝时尚未执行，按错误修正当前指令。
ego_state 是自我里程计、朝向/俯仰、碰撞和手持布尔状态；当前值优先于旧记忆。物体类别自行看图/记忆。初始任务不会公开后台子任务链、正确下一目标、隐藏答案。read/对话/失败反馈来自实际交互，摘要中的推测不是环境认证。memory_updates 引用实际收到的观察编号，区分事实/猜测；无新内容无需重复写。privileged_assist 表示已发生特权帮助。成功动作不等于任务完成，按可观察终点核对。done 仅在有证据完成任务时使用。
