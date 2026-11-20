# 评测代码说明

本目录为 VitaBrain 8852 评测应用的便携源码发布。

## 核心流程

1. `config.py` 加载运行参数及 provider 配置，默认使用本地 Mock 模式。
2. `environment/` 提供观察和动作执行接口。
3. `agent/` 构建模型上下文、管理记忆并解析模型决策。
4. `runner.py` 调度回合执行、续测状态和审计事件。
5. `audit/` 保存事件链；`trajectory_metrics.py` 汇总轨迹指标。
6. `web/app.py` 提供网页及运行管理 API。

## 安装与启动

```bash
pip install -e '.[dev]'
agentlab-web
```

默认服务地址为 `http://127.0.0.1:8852`。可通过 `AGENTLAB_HOST`、
`AGENTLAB_PORT`、`AGENTLAB_RUNS_DIR` 调整监听地址、端口和输出目录。
运行产生的记录保存在本地 `runs/`，不属于此次源码发布内容。

## 环境接口

- `mock`：内置合成演示环境，用于离线检查评测流程。
- `scenegen_http`：按 `contracts/` 接入外部 Runtime；需另行准备环境。
- `development_inspector`：研究工作区开发适配器；需外部开发服务，
  其运行不应作为正式评测成绩。

完整场景任务目录未随源码发布。代码在该目录缺失时保留环境返回的
任务进展；实际任务判定仍依赖外部环境，不能据此补造成功状态。

## 范围

该版本包含评测框架、提示词、接口契约、示例配置与基础测试。
场景数据、Unity 二进制、模型权重、私人配置、历史轨迹和研究报告不在本次发布中。
支持依赖说明见 `pyproject.toml`；可选空间处理依赖可通过 `.[spatial]` 安装。
