#!/usr/bin/env python3
"""Usage: PYTHONPATH=. ../venv/bin/python scripts/compute_trajectory_metrics.py runs/run_ID"""
import argparse
import json
from pathlib import Path

from agentlab.trajectory_metrics import compute, render_html
from agentlab.web.replay import atomic_write


def main():
    parser = argparse.ArgumentParser(description='离线复算轨迹精简指标，不启动环境或模型')
    parser.add_argument('run_dirs', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, default=Path('runs/trajectory_metrics'))
    args = parser.parse_args()
    for run_dir in args.run_dirs:
        report = compute(run_dir)
        target = args.output / run_dir.name
        atomic_write(target / 'metrics.json', json.dumps(report, ensure_ascii=False, indent=2).encode())
        atomic_write(target / 'report.html', render_html(report).encode())
        print(json.dumps({'run_id': report['run_id'], 'report': str((target/'report.html').resolve()),
                          'success': report['task']['observed_success'], 'gc': report['task']['gc'],
                          'decisions': report['execution']['decision_steps'],
                          'failure_rate': report['execution']['decision_failure_rate']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
