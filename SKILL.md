---
name: caw-eval
metadata:
  version: "2026.04.13.1"
description: |
  在本地 Claude Code 中评测 CAW (Cobo Agentic Wallet) Agent 质量，产出评分数据和分析报告。
  Use when: 用户想运行 CAW 评测、跑评测、测试 Skill、评估 Agent 质量、
  生成评测报告，或说 "跑评测", "测评 CAW", "eval", "评分"。
  弱模型 / openclaw 评测请使用 caw-eval-openclaw（仅安装在 openclaw 服务器）。
---

# CAW Eval

端到端评测 CAW Agent 质量，产出评分数据和分析报告。

## Step 0: 环境识别（必做）

执行任何后续步骤前，先确认当前环境：

```bash
[[ "$(hostname)" == *openclaw* ]] && echo "env=openclaw" || echo "env=local"
```

- `env=openclaw`：**本 SKILL（caw-eval）是本地 CC 版本，不能在 openclaw 服务器跑**。告诉用户：
  > "当前在 openclaw 服务器，本地 CC 评测 SKILL 不适用。请使用 `caw-eval-openclaw`（说'弱模型评测'或'openclaw 评测'触发）。"
  然后停止。
- `env=local`：继续下面的流程路由。

## 流程路由

根据用户意图选择评测方式，然后**读取对应的 reference 文件并按步骤执行**：

| 用户说 | 评测方式 | 读取并执行 |
|--------|---------|-----------|
| "跑评测"、"测评 CAW"、"eval"、"评分"、"claude code 评测" | **Claude Code 评测**（默认 dataset: `caw-agent-eval-seth-v2`） | → 读 [run-eval-cc.md](./references/run-eval-cc.md) 按步骤执行 |
| "recipe 评测"、"跑 recipe"、"recipe eval" | **Claude Code 评测**（dataset: `caw-recipe-eval-seth-v1`） | → 读 [run-eval-cc.md](./references/run-eval-cc.md) 按步骤执行，`--dataset-name caw-recipe-eval-seth-v1` |
| "弱模型验证"、"openclaw 评测"、"模型兼容性" | **Openclaw 弱模型验证** | 本 SKILL 不直接执行。告诉用户按 [run-eval-openclaw.md](./references/run-eval-openclaw.md) 操作：在服务器 openclaw 中说"跑评测"→ 下载 session 到本地 → 在本地 Claude Code 说"导入 session 并评分" |

**默认走 Claude Code 评测**（如果用户没有明确说"弱模型"或"openclaw"）。

---

## 概览

### Claude Code 评测（主要方式）

在本地 Claude Code 中用 Sonnet subagent 并行执行 + 评分，最后用 Opus subagent 生成分析报告。

```
检查环境 → 获取 case 列表 → Sonnet subagent 并行执行 14 case
→ 收集 session → 上传 Langfuse → LLM Judge 评分 → 应用评分
→ Opus subagent 生成报告
```

- 时间：约 40 分钟（14 case 并行 4 个）
- 模型分工：
  - 主会话 / Step 1-8：**Sonnet**（编排与脚本调度，不消耗 Opus 额度）
  - Step 3 评测 subagent：**Sonnet**（独立周额度）
  - Step 7 judge subagent：**Sonnet**（或走 API 直调节省 CC 额度）
  - Step 9 报告 subagent：**Opus**（深度分析，隔离 context 省 token）
- 详细步骤：[run-eval-cc.md](./references/run-eval-cc.md)

### Openclaw 弱模型验证

在 Openclaw 服务器上用弱模型执行，本地 Claude Code 评分。三层分离架构。

```
服务器: 脚本生成 prompt → 弱模型执行 task → 脚本收集 session → 打包
  ↓ gcp scp
本地: 导入 session → LLM Judge 评分 → 上传 Langfuse → 生成报告
```

- 适用：上线前验证 Skill 对弱模型的兼容性
- 详细步骤：[run-eval-openclaw.md](./references/run-eval-openclaw.md)

---

## 评分体系

```
综合分 = task_completion × 0.3 + process_quality × 0.7
process_quality = S1(意图) × 0.15 + S2(Pact) × 0.45 + S3(执行) × 0.4
```

所有分数 0-1。详见 [scoring.md](./references/scoring.md)。

## 数据集

| 数据集 | Case 数 | 场景类型 | 说明 |
|--------|---------|---------|------|
| `caw-agent-eval-seth-v2` | 14 | transfer/swap/lend/dca/... | 默认，Ethereum Sepolia 测试链 |
| `caw-recipe-eval-seth-v1` | - | recipe | Recipe 多步骤场景，Sepolia 测试链 |

- 默认使用 `caw-agent-eval-seth-v2`，用户明确说"recipe 评测"时改用 `caw-recipe-eval-seth-v1`
- 可通过 `--dataset-name` 指定其他数据集
- 已有数据集和创建新数据集参见 [dataset-management.md](./references/dataset-management.md)

## Scripts

| 脚本 | 用途 |
|------|------|
| `run_eval_cc.py` | Claude Code 评测编排（prepare/collect/upload/score/import-sessions） |
| `run_eval_openclaw.py` | Openclaw 评测编排（prepare/collect/upload/pack） |
| `eval_utils.py` | 公共工具（Langfuse 客户端/数据集/上传） |
| `judge_cc.py` | LLM-as-Judge（prompt 构建 + API 调用） |
| `assertions.py` | 结构化断言 + 门槛检查 |
| `score_traces.py` | 评分管线（断言 + judge → 综合分 → Langfuse） |
| `upload_session.py` | session → Langfuse trace |
| `generate_dataset.py` | 数据集生成 |
