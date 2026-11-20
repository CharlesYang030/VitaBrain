"""Short RGB-only floor-point navigation; no persistent map or engine access.

The controller accepts only a sealed view of front RGB, nominal camera
calibration and action-derived odometry. Estimated depth inside FloorPerception
is inferred from RGB, never a simulator depth sensor.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from agentlab.agent.perception import CALIBRATION, FloorPerception, pixel_to_ground, world_to_image

VERSION = 'front_rgb_navigation_v2'
MAX_ACTIONS = 4
MAX_DISTANCE_M = 5.0
ARRIVAL_RADIUS_M = .40


@dataclass(frozen=True)
class NavigationView:
    observation_id: str
    rgb: str
    x: float
    z: float
    heading: float
    pitch: float
    held: bool
    annotation: tuple | None = None
    width: int = 560
    height: int = 560

    @classmethod
    def from_public(cls, observation):
        ego = observation.get('ego_state') or {}
        pos = ego.get('position_relative_spawn_m') or {}
        values = [pos.get('x'), pos.get('z'), ego.get('heading_relative_spawn_deg'), ego.get('camera_pitch_deg')]
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise ValueError('public_odometry_unavailable')
        rgb = observation.get('rgb') or {}
        front = next((v for v in (observation.get('current_views') or {}).get('views', [])
                      if v.get('view_id') == 'front'), {})
        return cls(observation['observation_id'], rgb.get('data_url', ''), *values,
                   bool(ego.get('held')), tuple(front['annotation_rect_px']) if front.get('annotation_rect_px') else None,
                   rgb.get('width', 560), rgb.get('height', 560))


class PixelNavigationController:
    def __init__(self, perception=None):
        self.perception = perception or FloorPerception()
        self.goal = None
        self.frame = None
        self.recent_floor_views = []
        self.total_inference_ms = 0.
        self.reset_request()

    def reset_request(self):
        # A refusal can occur before inference or projection. Never associate
        # the previous destination, remaining distance or RGB with a new click.
        # Keep the separate recent-floor cache subject to its existing policy.
        self.goal = None
        self.frame = None
        self.state = dict(version=VERSION, status='idle', max_primitive_actions=MAX_ACTIONS,
                          max_distance_m=MAX_DISTANCE_M, arrival_radius_m=ARRIVAL_RADIUS_M, source='current_front_RGB_and_public_odometry_only',
                          calibration=CALIBRATION, persistent_mapping_required=False)

    def stop(self, reason):
        self.goal = None
        self.state.update(status='stopped', reason=reason)

    def _infer(self, view):
        self.frame = self.perception.infer(view.rgb, pitch=view.pitch)
        self.total_inference_ms += self.frame.get('inference_ms', 0.)
        self.state['source_rgb_sha256'] = self.frame.get('source_rgb_sha256')
        self.state['perception_ms'] = self.frame.get('inference_ms', 0.)
        if not self.recent_floor_views or self.recent_floor_views[-1][0].observation_id != view.observation_id:
            self.recent_floor_views.append((view, dict(self.frame)))
            self.recent_floor_views = self.recent_floor_views[-4:]

    def clear_recent_views(self):
        # An ordinary model action may manipulate an object or change floors.
        # Never carry a local navigation corridor through such a transition.
        self.recent_floor_views.clear()

    def _floor(self, u, v, view):
        u, v = np.asarray(u), np.asarray(v)
        h, w = self.frame['floor'].shape
        valid = np.isfinite(u) & np.isfinite(v) & (u > .01) & (u < .99) & (v > .01) & (v < .99)
        ix = np.clip(np.nan_to_num(u*w).astype(int), 0, w-1)
        iy = np.clip(np.nan_to_num(v*h).astype(int), 0, h-1)
        valid &= self.frame['floor'][iy, ix] > 0
        if view.annotation:
            x0, y0, x1, y1 = view.annotation
            valid &= ~((u*view.width >= x0) & (u*view.width < x1) &
                       (v*view.height >= y0) & (v*view.height < y1))
        return valid

    def request(self, request, view: NavigationView):
        self.reset_request()
        self.state.update(status='checking', reason=None, primitive_actions=0, travelled_m=0.,
                          source_observation_id=view.observation_id, requested_pixel=dict(request))
        if request.get('view_id', 'front') != 'front':
            self.stop('navigation_requires_current_front_view'); return False
        if request.get('observation_id') != view.observation_id:
            self.stop('stale_navigation_observation'); return False
        if view.held:
            self.stop('held_object_requires_model_control'); return False
        x, y = request.get('x'), request.get('y')
        if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 < v < 1 for v in (x, y)):
            self.stop('invalid_normalized_floor_pixel'); return False
        self._infer(view)
        if not self._floor(x, y, view):
            self.stop('destination_not_confirmed_as_visible_floor'); return False
        point = pixel_to_ground(x, y, view.pitch, view.width/view.height)
        if point is None or math.hypot(*point) > MAX_DISTANCE_M:
            self.stop('floor_projection_outside_5m_range'); return False
        a = math.radians(view.heading)
        rx, rz = point
        self.goal = (view.x+rx*math.cos(a)+rz*math.sin(a), view.z-rx*math.sin(a)+rz*math.cos(a))
        self.state.update(status='active', estimated_destination_m=list(self.goal),
                          uncertainty='nominal_flat_floor_projection_and_RGB_estimation')
        return True

    def accept_receipt(self, receipt):
        self.state['primitive_actions'] += 1
        actual = receipt.get('actual') or {}
        distance = actual.get('distance_m', 0.)
        if type(distance) in (int, float) and math.isfinite(distance):
            self.state['travelled_m'] += abs(distance)
        if receipt.get('success') is not True or receipt.get('collision'):
            self.stop('blocked_or_failed_action_requires_model_replanning')

    def next_action(self, view: NavigationView):
        if self.goal is None:
            return None
        if self.state['primitive_actions'] >= MAX_ACTIONS or self.state['travelled_m'] >= MAX_DISTANCE_M:
            self.stop('bounded_horizon_completed'); return None
        if view.held:
            self.stop('held_object_requires_model_control'); return None
        dx, dz = self.goal[0]-view.x, self.goal[1]-view.z
        distance = math.hypot(dx, dz)
        self.state['estimated_remaining_m'] = round(distance, 3)
        if distance <= ARRIVAL_RADIUS_M:
            self.stop('waypoint_reached_not_task_completion'); return None
        angle = (math.degrees(math.atan2(dx, dz))-view.heading+180)%360-180
        if abs(angle) > 6:
            return dict(verb='rotate_right' if angle > 0 else 'rotate_left', target=None,
                        parameters={'degrees': round(min(60., abs(angle)), 1)})
        self._infer(view)
        limit = min(1.5, distance-.20, MAX_DISTANCE_M-self.state['travelled_m'])
        # Check the swept width. The robot body occludes roughly the first
        # 0.5 m in this calibrated view. That region is NOT marked free: use
        # floor actually seen in the last four navigation frames, or a bounded
        # 0.1 m collision-checked probe. Beyond it, require current visible floor.
        safe = 0.
        blind_near_body = False
        # A goal just outside the .4 m arrival radius still needs a small
        # closing move. Always verify two near-floor samples (.25/.35 m);
        # otherwise limit=distance-.20 can end the scan at .25, fail safe<.30,
        # and falsely reject perfectly observed floor at distances .4-.55 m.
        # This extends verification, not the commanded travel distance.
        check_distance = max(.35, limit)
        for forward in np.arange(.25, check_distance+.001, .10):
            side = np.array([-.22, 0., .22])
            a = math.radians(view.heading)
            wx = side*math.cos(a)+forward*math.sin(a)
            wz = -side*math.sin(a)+forward*math.cos(a)
            u, v, depth = world_to_image(wx, wz, view.heading, view.pitch, view.width/view.height)
            free = (depth > 0) & self._floor(u, v, view)
            if forward <= .50 and not np.all(free):
                current_frame = self.frame
                try:
                    for old_view, old_frame in self.recent_floor_views[:-1]:
                        ou, ov, od = world_to_image(wx+view.x-old_view.x, wz+view.z-old_view.z,
                            old_view.heading, old_view.pitch, old_view.width/old_view.height)
                        self.frame = old_frame
                        free |= (od > 0) & self._floor(ou, ov, old_view)
                finally:
                    self.frame = current_frame
                blind_near_body |= not np.all(free)
            elif not np.all(free):
                break
            safe = float(forward)
        self.state['near_body_blindzone_probe'] = bool(blind_near_body)
        if blind_near_body:
            if view.pitch < 55:
                return dict(verb='look_down', target=None,
                            parameters={'degrees': round(min(30., 55-view.pitch), 1)})
            if safe >= .65:
                return dict(verb='move_forward', target=None, parameters={'distance_m': .10})
            self.stop('near_route_not_confirmed_as_visible_floor'); return None
        if safe < .30:
            if view.pitch < 55:
                return dict(verb='look_down', target=None,
                            parameters={'degrees': round(min(30., 55-view.pitch), 1)})
            self.stop('near_route_not_confirmed_as_visible_floor'); return None
        return dict(verb='move_forward', target=None, parameters={'distance_m': round(min(safe-.10, limit), 2)})
