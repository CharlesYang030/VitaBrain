explored_path_recall_v1 已探索路径回溯：每步普通动作 decision_type="action",return_request=null；或独立回溯：
{"decision_type":"return_to_visited","return_request":{"node_id":"P003","map_observation_id":"当前 observation_id","reason":"回去的原因与近期目标"},"action":null}
仍须其他已开功能要求的字段。P编号原样选自 spatial_memory.recall_candidates /地图青色标记，仅实际到过的站位，非物体坐标/未来路线。
后台检查同层、观察有效、旧路线门/障碍和终点净空；失败不移动且不生成新图，返回recall_door_closed/recall_route_blocked/recall_map_stale等原因。条件未变不重复；旧路失败不证明不存在其他路。成功才落位、刷新三视图/地图并给前后观察、历史事件、原因、距离、目标回执，不代表任务完成。每次请求计1决策，内部定位非普通原子动作；省略途中时间/风险，是辅助导航成绩。跨层用真实楼梯/电梯。

可携带当前手持物回溯；恢复历史位置、朝向与视线，保留当前物品、内容和任务状态，不是世界状态回退。

导航选择优先级：眼前可完成任务操作就先操作；需返回已探索位置时，优先直接选相关P点回溯，不要为旧路反复转身或逐步行走。支线结束、送回物品、绕路后返回、困在角落或重复微调时，主动检查P点与distance_from_current_m；无需自己证明一定可达，后台会检查。探索新区域再用点选导航（若已开启）。不要回溯到几乎重合的当前点；同条件失败不重复。近邻P点会保留ID并更新为最近一次真实站位及朝向，按本轮候选表为准。
