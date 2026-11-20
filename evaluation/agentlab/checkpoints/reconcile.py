"""Operator-only recovery of a verified no-action auxiliary-observation failure.

No endpoint exposes this to the model. Never repairs an uncertain movement,
rewinds a world, replays a model call, or drops committed memory/usage.
"""
import json
from . import journal
from agentlab.domain import Stage, Visibility


def verify_readonly_tail(runner, proof):
    if (runner.auto_running or runner.model_waiting or runner.status!='running'
            or runner._checkpoint_dispatch or runner.navigation_reconciliation_required
            or runner.path_recall_reconciliation_required or not runner.checkpoint_save_pending):
        raise RuntimeError('only a paused, known no-action save failure can be reconciled')
    if (proof.get('run_id')!=runner.run_id or proof.get('step')!=runner.step_number
            or proof.get('source_observation_id')!=runner.observation['observation_id']
            or proof.get('source_rgb_equal') is not True
            or proof.get('native_metadata_equal') is not True):
        raise RuntimeError('reviewed native no-action proof missing or mismatched')
    valid,problem=runner.audit.verify()
    if not valid:raise RuntimeError('audit chain invalid: '+str(problem))
    events=list(runner.audit.events())
    tail=[e for e in events if e['step']==runner.step_number-1]
    if any(e['stage'] in ('ACTION_EXECUTED','PATH_RECALL_COMPLETED','HANDOFF_APPLIED') for e in tail):
        raise RuntimeError('physical or privileged action receipt exists; never replay or auto-reconcile')
    completed=[e['payload'] for e in tail if e['stage']=='NAVIGATION_COMPLETED']
    if len(completed)!=1:raise RuntimeError('exactly one committed failed navigation receipt required')
    result=completed[0]
    if (result.get('feedback_code')!='gt_navigation_query_changed_frame'
            or result.get('success') is not False or result.get('primitive_actions')!=0
            or result.get('travelled_m')!=0):
        raise RuntimeError('this is not the known no-action auxiliary capture failure')
    return result


def reconcile_readonly_observation(runner, proof):
    """Preserve the current live runner; append a fresh paired observation."""
    if not runner._step_lock.acquire(blocking=False):raise RuntimeError('decision still active')
    try:
        verify_readonly_tail(runner,proof)
        env=runner.environment
        before=env._json('GET','/api/inspect/runtime-details?limit=1')
        before_health=env._json('GET','/api/inspect/runtime-health')
        if not before_health.get('ready'):raise RuntimeError('native runtime unavailable')
        old_id=runner.observation['observation_id'];step=runner.step_number
        raw=env._observation('导航准备未产生移动；维护后重新同步当前观察。当前持物、任务进度和生命值保留，上一决策不重放。')
        after=env._json('GET','/api/inspect/runtime-details?limit=1')
        after_health=env._json('GET','/api/inspect/runtime-health')
        if (before.get('sim_time')!=after.get('sim_time') or before.get('safety')!=after.get('safety')
                or before_health.get('pid')!=after_health.get('pid') or not after_health.get('ready')):
            raise RuntimeError('world/time/safety changed during observation synchronization')
        current=runner._record_observation(raw)
        assert runner.step_number==step
        runner.audit.append(Stage.RUN_STATE,Visibility.EVALUATOR_PRIVATE,dict(
            maintenance='readonly_observation_cache_reconciled',proof=proof,
            previous_observation_id=old_id,observation_id=current['observation_id'],
            model_call_replayed=False,physical_action_replayed=False,unity_restarted=False),step=step)
        runner.audit.append(Stage.OBSERVED,Visibility.AGENT_VISIBLE,current,step=step)
        journal(runner,'readonly_observation_reconciled')
        runner.checkpoint_reconciliation_required=False;runner.checkpoint_save_pending=False
        runner.auto_error=None;runner.auto_stop_reason='maintenance_paused'
        return runner.save_checkpoint()
    except Exception:
        runner.checkpoint_reconciliation_required=True
        runner.checkpoint_save_pending=True
        raise
    finally:runner._step_lock.release()
