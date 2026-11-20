# VitaBrain — Evaluation Framework

## 论文信息

**VitaBrain: Benchmarking Long-Horizon Spatiotemporal Planning and Reasoning in Interactive Worlds**

**作者（按论文署名顺序）：**

Qihao Yang<sup>2</sup>, Pengfei Chen<sup>3</sup>, Lingxi Xie<sup>3</sup>,
Zhixu Zheng<sup>4</sup>, Qiyue Zhao<sup>2</sup>, Jiannan Ge<sup>3</sup>,
Xinyue Huo<sup>3</sup>, Kaiwen Duan<sup>3</sup>, Sida He<sup>3</sup>,
Yifan Yang<sup>2</sup>, Zhihang Zhong<sup>2,*</sup>, Xue Yang<sup>2,*</sup>,
Qi Tian<sup>1,3,*</sup>.

**单位：**

1. Guangdong Laboratory of Artificial Intelligence and Digital Economy (SZ), China
2. Shanghai Jiao Tong University
3. Huawei Technologies Ltd
4. University of Electronic Science and Technology of China

<sup>*</sup> Corresponding authors / 通讯作者：Zhihang Zhong、Xue Yang、Qi Tian。

**研究概述：** 本文面向交互世界中的长程时空规划与推理，研究模型如何结合
空间理解、持续记忆、线索利用和多步决策完成相互依赖的任务。本仓库提供
相关评测框架源码，具体发布范围和运行方式见下文。

## 代码概览

VitaBrain 长程时空智能体评测框架源码（8852 评测应用）。

本仓库提供评测与调试代码，包括模型接口、环境交互契约、结构化记忆、
执行与审计、轨迹指标以及 Web 界面。该版本是从研究工作区整理的源码发布，
可先使用内置 Mock 模型和 Mock 环境验证基本流程。

## 发布内容

- `evaluation/agentlab/agent/`：决策上下文、记忆管理及动作处理。
- `evaluation/agentlab/providers/`：模型适配、请求与响应处理。
- `evaluation/agentlab/environment/`：Mock、HTTP Runtime 及开发环境接口。
- `evaluation/agentlab/runner.py`：交互回合执行、状态与审计记录。
- `evaluation/agentlab/trajectory_metrics.py`：轨迹指标计算。
- `evaluation/agentlab/web/`：评测 Web 服务和前端。
- `evaluation/contracts/`、`evaluation/prompts/`：接口契约与提示词。
- `evaluation/tests/`：可独立运行的基础测试。

本次发布不含完整场景数据、Unity 播放器、模型权重或历史实验记录。
真实环境评测需要另行准备兼容的 Runtime 与场景资源；本仓库不声称
可单独复现论文的完整实验。该评测框架也不等同于 Online SkillOpt 的训练实现。

## 快速开始

要求 Python 3.11 或更高版本。建议在独立虚拟环境中安装。

```bash
git clone https://github.com/CharlesYang030/VitaBrain.git
cd VitaBrain/evaluation
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
agentlab-web
```

在浏览器打开 `http://127.0.0.1:8852`，使用 `mock` provider 和 `mock`
environment。Mock 流程不需要 API Key、Unity 或付费模型调用；其结果
用于检查代码流程，不是正式基准测试成绩。Windows 可使用
`.venv\Scripts\activate` 激活环境。

## 测试和轨迹检查

```bash
cd evaluation  # 如已位于 evaluation 目录，无需再次执行
pytest -q
python scripts/inspect_run.py runs/<run_id>
```

轨迹指标入口：`python scripts/compute_trajectory_metrics.py --help`。

## 接入真实模型与环境

模型配置模板见 `evaluation/config/providers.example.yaml`。
`openai_compatible` 项中的 URL 和模型名均为占位值，需要替换为自己的配置。
将密钥通过 `VLM_API_KEY` 环境变量提供，不要提交密钥或 `.env` 文件。

外部 HTTP Runtime 的消息格式见 `evaluation/contracts/`。可使用
`python scripts/run_mock_runtime.py` 启动契约模拟服务，再设置
`AGENTLAB_ENV_BACKEND=scenegen_http` 和 `AGENTLAB_SCENEGEN_URL` 进行接口调试。
具体代码说明见 [evaluation/README.md](evaluation/README.md)。

## 版本与许可证

首个源码版本：`v0.1.0`。项目源码采用 [MIT License](LICENSE)。
外部依赖及另行安装的第三方组件遵循各自许可证，见
[THIRD_PARTY.md](THIRD_PARTY.md)。
