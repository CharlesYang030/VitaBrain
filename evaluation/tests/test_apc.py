from agentlab.agent.apc import APCMonitor


def test_apc_counts_active_time_and_excludes_pause():
    now = [10.0]
    monitor = APCMonitor(300, clock=lambda: now[0])
    monitor.set_focus("s1")
    now[0] += 200
    monitor.pause()
    now[0] += 500
    assert monitor.elapsed() == 200 and not monitor.should_intervene()
    monitor.resume()
    now[0] += 100
    assert monitor.should_intervene()

