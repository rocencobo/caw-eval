# Claude Code 评测：执行步骤

**本文件是 Agent 的执行指南。按 Step 1-9 依次执行即可。**

---

## Step 1: 检查环境

```bash
export PATH="$HOME/.cobo-agentic-wallet/bin:$PATH"
caw status          # 确认 healthy=true, signing_ready=true
caw wallet balance  # 确认 SETH 有余额（建议 >= 0.2）
```

如果环境不就绪，提示用户先完成 onboarding（参考 cobo-agentic-wallet skill）。

---

## Step 2: 获取 test case 列表

```bash
cd <repo>/cobo-agent-wallet
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval_cc.py prepare \
  --dataset-name caw-agent-eval-seth-v2
```

> 默认数据集为 `caw-agent-eval-seth-v2`（14 case）。
> Recipe 场景评测使用 `--dataset-name caw-recipe-eval-seth-v1`。
> 如需同时评测两个数据集，按以下流程各跑一遍，使用不同的 run-name 区分（如 `eval-cc-sonnet-20260414-1200-v2` 和 `eval-cc-sonnet-20260414-1400-recipe`）。

输出每个 case 的 item_id 和 user_message。记下这些信息用于 Step 3。

---

## Step 3: 执行评测（Sonnet subagent 并行）

对每个 case，启动一个后台 Sonnet subagent。**始终保持 4-5 个并行**，一个完成就补一个新的。

**为什么用 Sonnet**：Sonnet 有独立的周额度（Weekly Sonnet），不消耗主额度。

**subagent 调用方式**：

```python
Agent(
    model="sonnet",
    run_in_background=True,
    description="Eval {item_id}",
    prompt="""[EVAL:{item_id}]

你是 CAW (Cobo Agentic Wallet) Agent。请先读取 Skill 指令，然后执行用户操作。

## 读取 Skill
1. {repo}/sdk/skills/cobo-agentic-wallet-dev/SKILL.md
2. {repo}/sdk/skills/cobo-agentic-wallet-dev/references/pact.md
3. {repo}/sdk/skills/cobo-agentic-wallet-dev/references/error-handling.md

## 评测约束
- 不得复用已有的 active pact，必须为本次任务创建新的 pact（评测需要评估 pact 协商能力）
- 提交 pact 时跳过预览和确认，直接 `caw pact submit`
- `pending_approval` 且 `owner_linked=false` 时自动 `caw pending approve`
- 无法自动化的阻塞记录原因并跳过
- 自动化评测，须完整执行至结束

## 环境
- caw: `export PATH="$HOME/.cobo-agentic-wallet/bin:$PATH"`
- 环境: sandbox, signing_ready=true
- 工作目录: {repo}/cobo-agent-wallet

## 用户指令
{user_message}"""
)
```

**时间预估**：
- 简单操作（transfer/error/edge）：1-2 分钟
- DeFi 操作（swap/lend/dca）：5-15 分钟
- 14 case 并行 4 个：约 40 分钟总计

等待所有 subagent 完成。

---

## Step 4: 收集 session

```bash
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval_cc.py collect \
  --dataset-name {dataset_name} \
  --run-name eval-cc-sonnet-$(date +%Y%m%d-%H%M)
```

`{dataset_name}` 替换为实际数据集名称（`caw-agent-eval-seth-v2` 或 `caw-recipe-eval-seth-v1`）。

确认所有 session 都收集到。脚本搜索 `~/.claude/projects/` 下包含 `[EVAL:{item_id}]` 标记的 subagent session 文件。

---

## Step 4.5: 提取运行指标

```bash
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval_cc.py metrics \
  --run-name {run_name}
```

从各 session 文件中提取运行指标，生成 `~/.caw-eval/runs/{run_name}/session_metrics.json`。

指标包括：时长（秒）、output tokens、工具调用数、caw 命令数、pact submit 次数、tx 命令数、错误数。该文件供 Step 9 Opus 写报告时直接读取 Section 3（运行指标）。

---

## Step 5: 上传 session 到 Langfuse

```bash
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval_cc.py upload \
  --run-name {run_name} \
  --dataset-name {dataset_name}
```

脚本为每个 session 生成独立的 Langfuse trace（UUID），并关联到 dataset run。同时在 run 目录下生成 `trace_map.json`，记录 item_id → trace UUID 的映射，供后续评分使用。

确认输出中每个 item 都显示 `[LINKED]`。

---

## Step 6: 生成精细版 judge prompt

```bash
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py session \
  --session ~/.caw-eval/runs/{run_name}/ \
  --dataset-name {dataset_name} \
  --dump-judge-requests ~/.caw-eval/runs/{run_name}/judge_req.json
```

生成精细版 judge request（含断言结果 + pact 参数 + expected output）。judge request 保存在 run 目录下，避免不同 run 之间互相覆盖。

---

## Step 7: LLM Judge 评分（Sonnet subagent 并行）

读取 `~/.caw-eval/runs/{run_name}/judge_req.json`，对每个 request 启动一个后台 Sonnet subagent。**始终保持 4-5 个并行**，一个完成就补一个新的。

每个 subagent 通过 Read 工具读取完整 session 文件后评分，结果写入 run 目录下的 `judge_{item_id}.json`。

```python
# 读取 judge_req.json，对每个 request 启动一个 subagent：
run_dir = "~/.caw-eval/runs/{run_name}"

Agent(
    model="sonnet",
    run_in_background=True,
    description="Judge {item_id}",
    prompt="""你是 CAW Agent 评估专家。请对以下 session 进行评分。

{prompt}  # judge_req.json 中该 item 的 prompt 字段（含 session_path 和评分维度）

将结果写入 {run_dir}/judge_{item_id}.json，格式：
{{
  "item_id": "{item_id}",
  "intent_understanding": {{"score": 0.0, "reasoning": "..."}},
  "policies_correctness": {{"score": 0.0, "reasoning": "..."}},
  "completion_conditions_correctness": {{"score": 0.0, "reasoning": "..."}},
  "execution_correctness": {{"score": 0.0, "reasoning": "..."}},
  "result_reporting": {{"score": 0.0, "reasoning": "..."}},
  "task_completion": {{"score": 0.0, "reasoning": "..."}}
}}

should_refuse case 只需输出 refusal_quality 和 task_completion 两个维度。"""
)
```

所有 judge 完成后，合并结果：

```python
import json, glob
run_dir = "~/.caw-eval/runs/{run_name}"
results = []
for f in sorted(glob.glob(f"{run_dir}/judge_E2E-*.json")):
    results.append(json.loads(open(f).read()))
open(f"{run_dir}/judge_results.json", "w").write(json.dumps(results, indent=2, ensure_ascii=False))
```

---

## Step 8: 应用评分到 Langfuse

```bash
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py session \
  --session ~/.caw-eval/runs/{run_name}/ \
  --dataset-name {dataset_name} \
  --judge-results ~/.caw-eval/runs/{run_name}/judge_results.json \
  --report
```

评分写入 Step 5 上传的各 trace（通过 `trace_map.json` 定位），同时在 Langfuse dataset run 页面可按维度查看分数。

---

## Step 9: 生成报告（Opus subagent）

**必须**通过 Agent 工具启动 **Opus subagent** 写报告，主会话（Sonnet）不要直接写。

**为什么用 Opus**：报告阶段需要跨 case 根因归纳、P0/P1 权衡判断、上线决策这类深度推理，Sonnet 可行但质量明显弱。Opus subagent 在隔离 context 中按需读产物，成本比主 Opus 直接写低约 40%。

### 9.1 主会话先整理"运行观察"

在启动 Opus 之前，主会话自己写一段 briefing（Opus subagent 看不到主会话历史，必须显式传入）：

- 哪些 case 失败/超时/需重试
- 环境异常（余额、faucet、API 超时、pending 卡住等）
- 跑了几轮，每轮有没有差异
- 任何影响分析结论的元信息

保存为临时变量或文件，注入到下面 prompt 的 `## 主会话观察` 段落。

### 9.2 启动 Opus subagent

```python
Agent(
    subagent_type="general-purpose",
    model="opus",
    description="生成 CAW 评测报告",
    prompt=f"""基于以下产物写 EVAL_REPORT.md。主会话已跑完评测，你负责深度分析。

## 产物路径
- Judge 结果: ~/.caw-eval/runs/{{run_name}}/judge_results.json（14 case × 6 维度 score + reasoning）
- 运行指标: ~/.caw-eval/runs/{{run_name}}/session_metrics.json（各 case 时长/tokens/caw命令/错误数等）
- Session 原文: ~/.caw-eval/runs/{{run_name}}/E2E-*.jsonl
- Skill 源文件: {{repo}}/cobo-agent-wallet/sdk/skills/cobo-agentic-wallet-dev/
  - SKILL.md, references/pact.md, references/error-handling.md, references/security.md
- 报告模板参考: cobo-agent-wallet/sdk/skills/caw-eval/reports/eval-report-eval-cc-sonnet-20260416-l2-v1.md
- 输出路径: cobo-agent-wallet/sdk/skills/caw-eval/reports/eval-report-{{run_name}}.md

## 主会话观察（重要，你看不到主会话历史但需要这些信息）
- 运行轮次: {{n_runs}}
- 异常: {{observations}}

## 分析要求- L2 数据集最新 baseline: E2E=0.867, TC=0.770（eval-cc-sonnet-20260416-0524）

1. 先 Read judge_results.json 全文，按 e2e_composite 从低到高排序
2. 先 Read session_metrics.json 全文，用于 Section 3 运行指标（不需要自己从 session 中统计）
3. 低分 case（<0.6）必须 Read 对应 session 追根因；高分 case 不需读 session
4. 遇到疑似 skill 指令缺陷时，Read 对应 skill 文件验证（不要猜）
5. P0/P1/P2 按"风险严重度 × 发生频率 × 修复成本"排序，每条附依据
6. 上线建议三选一：可上 / 有条件上 / 建议延期，附理由
7. 报告末尾新增 Section：**修复收益预测**——对本次 P0/P1 问题逐一估算修复后 E2E 变化（参考 eval-report-eval-cc-sonnet-20260416-l2-v1.md 第 9 节的格式）

## 产出约束
- 所有断言必须指向具体 case / tx / 代码行，避免空泛评价
- 失败 case 用"现象 → 根因 → Action Item"三段式
- 报告结构参考模板，不要另创结构

## 报告包含
1. 总览（E2E 综合分 + 任务完成率 + 与 baseline 对比）
2. 逐 Case 评分（按 e2e_composite 从低到高）
3. 运行指标（时长/tokens/caw 命令/错误数/pact 效率）—— 数据来自 session_metrics.json，tokens 为 output_tokens
4. 逐 Case 详细分析（仅低分 case 深入，高分 case 一行总结）
5. 按场景类型分析（transfer/swap/lend/dca/...）
6. 阶段瓶颈分析（S1/S2/S3）
7. 改进建议（P0/P1/P2 分级，每条附理由）
8. 上线建议
"""
)
```

**模型选择说明**：
- 如果只想快速出草稿、人工过后 review，可将 `model="opus"` 改为 `model="sonnet"`（成本再降 60%，报告质量降级约 15%，关键 P0 判断和上线决策质量损失明显）。
- 若需严格成本控制，先用 `model="sonnet"` 出草稿，再用 `model="opus"` 启第二个 subagent 仅对"P0/P1 建议 + 上线决策"两段做 refine。

---

## Troubleshooting

| 问题 | 解决 |
|------|------|
| caw status 报错 | 运行 `scripts/bootstrap-env.sh` 安装 caw |
| signing_ready=false | 需要重新 onboard（`caw onboard --env sandbox`） |
| collect 找不到 session | 确认 subagent 全部完成，检查 `~/.claude/projects/` 下是否有 `agent-*.jsonl` |
| Langfuse 凭证缺失 | 配置 `scripts/.env`（参考 `.env.example`） |
| score_traces.py 报 "No items loaded" | 确认 `--dataset-name caw-agent-eval-seth-v2` 正确 |
