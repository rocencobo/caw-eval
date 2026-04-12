# CAW Eval

端到端评测 CAW (Cobo Agentic Wallet) Agent 质量，产出评分数据和分析报告。

## 两种评测方式

| 方式 | 文档 | 适用场景 |
|------|------|---------|
| **Claude Code 评测**（主要） | [SKILL.md](./SKILL.md) | 日常迭代、修复验证 |
| **Openclaw 弱模型验证** | [run-eval-openclaw.md](./references/run-eval-openclaw.md) | 上线前兼容性验证 |

## 目录结构

```
caw-eval/
├── SKILL.md                     # 评测 skill（入口）
├── references/
│   ├── run-eval-cc.md           # Claude Code 执行详细说明
│   ├── run-eval-openclaw.md     # Openclaw 执行详细说明
│   ├── scoring.md               # 评分体系（维度/权重/公式/解读）
│   └── dataset-management.md    # 数据集管理
├── reports/                     # 评测报告（按日期归档）
│   └── eval-report-YYYYMMDD-model-dataset.md
└── scripts/
    ├── run_eval_cc.py           # Claude Code 评测编排
    ├── run_eval_openclaw.py     # Openclaw 评测编排
    ├── eval_utils.py            # 公共工具（Langfuse 客户端/数据集/上传）
    ├── judge_cc.py              # LLM-as-Judge
    ├── assertions.py            # 结构化断言
    ├── score_traces.py          # 评分管线
    ├── upload_session.py        # session → Langfuse trace
    └── generate_dataset.py      # 数据集生成
```

## 评分体系

```
综合分 = task_completion × 0.3 + process_quality × 0.7
process_quality = S1(意图) × 0.15 + S2(Pact) × 0.45 + S3(执行) × 0.4
```

详见 [scoring.md](./references/scoring.md)。

## 数据集

默认：`caw-agent-eval-seth-v2`（14 case，Ethereum Sepolia 测试链）。可通过 `--dataset-name` 指定其他数据集。

## 快速开始

参考 [SKILL.md](./SKILL.md) 的 Phase 1-7。
