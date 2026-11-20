"""Bounded local navigation over the RGB belief map, using original actions."""
from __future__ import annotations

import heapq
import math

from agentlab.agent.estimated_map import RESOLUTION

VERSION = 'public_map_controller_v1'


class NavigationController:
    def __init__(self, spatial):
        self.spatial = spatial
        self.goal = None
        self.remaining = 0
        self.state = dict(version=VERSION, status='idle')

    def stop(self, reason):
        self.goal = None
        self.remaining = 0
        self.state.update(status='paused', reason=reason)

    def request(self, request, observation):
        if not isinstance(request, dict):
            return False
        if request.get('observation_id') != observation['observation_id']:
            self.stop('stale_navigation_observation'); return False
        point = self.spatial.ground_point(request.get('x'), request.get('y'))
        if point is None:
            self.stop('destination_not_confirmed_as_visible_floor'); return False
        if (observation.get('ego_state') or {}).get('held'):
            self.stop('held_object_requires_model_control'); return False
        self.goal = point
        self.remaining = 4
        self.state = dict(version=VERSION, status='active', destination=list(point),
            chart=self.spatial.charts[-1]['id'], source_observation_id=observation['observation_id'],
            source='model_selected_current_FPV_pixel', max_primitive_actions=4)
        return True

    def next_action(self, observation, receipt=None):
        if self.goal is None:
            return None
        if self.remaining <= 0:
            self.stop('bounded_horizon_completed'); return None
        if receipt and receipt.get('collision'):
            self.stop('collision_requires_replanning'); return None
        pose = self.spatial.last_pose
        if not pose or self.spatial.charts[-1]['id'] != self.state['chart']:
            self.stop('chart_changed'); return None
        if math.hypot(self.goal[0]-pose['x'], self.goal[1]-pose['z']) < .3:
            self.stop('waypoint_reached_not_task_completion'); return None
        grid = self.spatial.grids.get(self.state['chart'], {})
        start = (round(pose['x']/RESOLUTION), round(pose['z']/RESOLUTION))
        target = (round(self.goal[0]/RESOLUTION), round(self.goal[1]/RESOLUTION))
        # Collision clearance is conservative and applies only to observed floor.
        def free(node):
            if any((node[0]+dx,node[1]+dz) in self.spatial.current_boundaries
                   for dx,dz in ((0,0),(2,0),(-2,0),(0,2),(0,-2))):
                return False
            # Apply the blind-zone allowance to each footprint cell, not to the
            # candidate center: its rear footprint can still be under the body.
            for dx,dz in ((0,0),(2,0),(-2,0),(0,2),(0,-2)):
                cell=(node[0]+dx,node[1]+dz)
                value=grid.get(cell,(0,))[0]
                if value>0: continue
                if value<0 or self.spatial.pitch<25 or math.hypot(cell[0]-start[0],cell[1]-start[1])>5:
                    return False
            return True
        frontier = [(0, start)]; prev={start:None}; cost={start:0}; found=None
        while frontier and len(cost) < 12000:
            _, node=heapq.heappop(frontier)
            if math.hypot(node[0]-target[0],node[1]-target[1]) <= 2:
                found=node; break
            for dx,dz in ((1,0),(-1,0),(0,1),(0,-1)):
                nxt=(node[0]+dx,node[1]+dz)
                if not free(nxt): continue
                value=cost[node]+1
                if value < cost.get(nxt, float('inf')):
                    cost[nxt]=value; prev[nxt]=node
                    heapq.heappush(frontier,(value+math.hypot(nxt[0]-target[0],nxt[1]-target[1]),nxt))
        if found is None:
            if self.spatial.pitch < 55:
                self.remaining-=1
                self.state.update(reason='inspect_near_ground_before_moving',remaining=self.remaining)
                return dict(verb='look_down',target=None,parameters={'degrees':max(1,min(30,55-self.spatial.pitch))})
            self.stop('no_route_in_observed_floor; inspect_ground_or_choose_another_waypoint'); return None
        path=[]
        while found != start:
            path.append(found); found=prev[found]
        path.reverse()
        if not path:
            self.stop('waypoint_reached_not_task_completion'); return None
        waypoint=path[min(2,len(path)-1)]
        dx,dz=waypoint[0]*RESOLUTION-pose['x'],waypoint[1]*RESOLUTION-pose['z']
        angle=(math.degrees(math.atan2(dx,dz))-pose['heading_deg']+180)%360-180
        if abs(angle)>8:
            action=dict(verb='rotate_right' if angle>0 else 'rotate_left',target=None,
                        parameters={'degrees':round(min(35,abs(angle)),1)})
        else:
            blind = any(grid.get(cell,(0,))[0] == 0 for cell in path[:3])
            action=dict(verb='move_forward',target=None,parameters={'distance_m':round(min(.1 if blind else .3,max(.05,math.hypot(dx,dz))),2)})
            self.state['near_body_blindzone_probe']=blind
        self.remaining-=1
        self.state.update(remaining=self.remaining, path_cells=len(path), action=action)
        return action
