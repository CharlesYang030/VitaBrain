"""Public state validation shared by interactive and batch adapters.

Only consumes returned public state. Never infers a damage cause, invents a
health value, accesses a private task predicate, or submits an action.
"""
import math


class InvalidPublicState(RuntimeError):
    pass


def health_value(value):
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise InvalidPublicState('health must be a finite nonnegative number or null')
    return value


def health_report_value(value):
    # SceneEnv exposes status.health as its public safety report; native
    # facades expose a number. Only unwrap that one documented health field.
    if isinstance(value, dict):
        if 'health' not in value:
            raise InvalidPublicState('public health report lacks health')
        value = value['health']
    return health_value(value)


def public_health(raw):
    if 'health' in raw:
        return health_report_value(raw['health'])
    status = raw.get('status')
    if status is None:
        return None
    if not isinstance(status, dict):
        raise InvalidPublicState('public status must be an object or null')
    return health_report_value(status.get('health'))


def public_instruction(raw):
    value = raw.get('instruction')
    if not value:
        task = raw.get('task')
        value = task.get('instruction') if isinstance(task, dict) else task
    if not isinstance(value, str) or not value.strip():
        raise InvalidPublicState('initial public task instruction missing')
    return value


def terminal_observation(observation):
    """A verified zero-health observation ends an episode immediately."""
    value = health_value(observation.get('health'))
    if value == 0:
        observation['episode_done'] = True
    return observation
