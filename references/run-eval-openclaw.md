# Openclaw 弱模型验证：执行步骤

**本文件是 Agent 的执行指南。按 Step 1-4 依次执行即可。**

---

## 流程概览

```
服务器端                        本地端（Claude Code）
─────────                       ────────────────────
Step 1: SSH → run               Step 2: langfuse 评分（直接读 Langfuse API）
  执行 task                       生成 judge prompt（含 session 内容）
  上传 Langfuse                Step 3: CC subagent 评分
  （session JSONL 留在服务器）   Step 4: 应用评分 + 生成报告
```

session 数据通过 Langfuse API 读取，**无需 scp 下载或导入本地**。

**推荐模式（动态队列，阻塞）**：dispatch 不加 `--fire-and-forget`，本地等待所有 item 完成，服务器间动态调度（快台多跑），结束后再评分：

```
dispatch（默认动态队列）→ 等待所有 item 完成 → score_traces.py langfuse ...
  （空闲服务器自动取下一个任务）
```

**流水线模式（可选，边跑边评分）**：dispatch 加 `--fire-and-forget`（静态预分配）+ `--watch` 轮询：

```
dispatch --fire-and-forget  →  score_traces.py langfuse --watch --dump-judge-requests ...
  （SSH 立即返回）                （实时轮询，新 trace 出现即生成 judge req）
```

---

## 新服务器配置清单

新建 openclaw 服务器后，在运行评测前需完成以下配置（否则评测会因依赖缺失而失败）：

```bash
# 1. SSH 进服务器，切换到 ubuntu 用户
gcloud compute ssh --zone "$ZONE" "$SERVER" --tunnel-through-iap --project "$PROJECT" \
  -- "sudo su - ubuntu"

# 2. 安装 pip（系统 python 可能不带）
sudo apt-get update && sudo apt-get install -y python3-pip

# 3. 安装 Python 依赖（必须 pin langfuse==4.0.6：4.2.0 移除了 Langfuse.api，会报 AttributeError）
pip3 install --user --break-system-packages python-dotenv "langfuse==4.0.6"

# 4. 配置 .env（Langfuse + caw skill 凭证）
mkdir -p ~/.agents/skills/caw-eval/scripts/
# 从本地 Mac 推送（或手动填写）：
# gcloud compute scp --zone "$ZONE" --project "$PROJECT" \
#   ~/.agents/skills/caw-eval/scripts/.env \
#   ubuntu@"$SERVER":~/.agents/skills/caw-eval/scripts/.env --tunnel-through-iap

# 5. 同步评测脚本（若未通过 openclaw skill sync 同步）
# 检查：ls ~/.agents/skills/caw-eval/scripts/run_eval_openclaw.py
```

> **langfuse 版本锁定**：必须用 `langfuse==4.0.6`。4.2.0 以上版本移除了 `Langfuse.api` 属性，
> 会导致 `AttributeError: 'Langfuse' object has no attribute 'api'`。
> 远端安装时用 `pip3 install --user --break-system-packages "langfuse==4.0.6"`。

---

## SSH ControlMaster 配置（避免每次 gcloud IAP 重新认证）

在 Mac 的 `~/.ssh/config` 添加以下配置，SSH 连接会复用，dispatch 并行 SSH 时无需多次 gcloud 认证：

```
Host *
  AddKeysToAgent yes
  UseKeychain yes
  IdentityFile ~/.ssh/id_rsa
  ControlMaster auto
  ControlPath ~/.ssh/ssh-%C
  ControlPersist 24h
  StrictHostKeyChecking no
```

配置后只需 `gcloud auth login` 一次，后续 SSH 连接通过 ControlMaster 复用已建立的通道。

---

## 服务器连接信息

```
SSH: gcloud compute ssh --zone "asia-east2-a" "luochong-openclew-dev-v1-20260318-070641" --tunnel-through-iap --project "openclaw-keq9xwm4"
用户: ubuntu（通过 sudo su - ubuntu 切换）
脚本目录: ~/.agents/skills/caw-eval/scripts/
openclaw: /home/ubuntu/.npm-global/bin/openclaw
caw: /home/ubuntu/.cobo-agentic-wallet/bin/caw
```

如果 `gcloud` 报 Python 模块错误，命令前加 `export CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11`。

---

## Step 1: 在服务器上执行评测

通过 SSH 在 Openclaw 服务器上运行评测脚本。脚本自动为每个 task 创建隔离 agent、通过 `openclaw agent` CLI 驱动弱模型执行、收集 session、上传 Langfuse、打包。

先获取模型信息：

```bash
export CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11

gcloud compute ssh --zone "asia-east2-a" "luochong-openclew-dev-v1-20260318-070641" \
  --tunnel-through-iap --project "openclaw-keq9xwm4" \
  -- "sudo su - ubuntu -c 'export PATH=/home/ubuntu/.npm-global/bin:\$PATH; openclaw status 2>&1 | head -5'"
```

然后执行评测：

```bash
gcloud compute ssh --zone "asia-east2-a" "luochong-openclew-dev-v1-20260318-070641" \
  --tunnel-through-iap --project "openclaw-keq9xwm4" \
  -- "sudo su - ubuntu -c 'export PATH=/home/ubuntu/.npm-global/bin:/home/ubuntu/.cobo-agentic-wallet/bin:\$PATH; \
  cd ~/.agents/skills/caw-eval/scripts && \
  python3 run_eval_openclaw.py run \
    --run-name {run_name} \
    --dataset-name {dataset_name} \
    --model {model_short} \
    --model-full {model_full} \
    --timeout 600 \
    2>&1'"
```

**参数说明**：
- `{run_name}`: 格式 `eval-oc-{model}-{YYYYMMDD-HHMM}`，如 `eval-oc-doubao-20260415-1030`
- `{dataset_name}`: 默认 `caw-agent-eval-seth-v2`，Ethereum Sepolia 用 `caw-agent-eval-eth-v1`
- `{model_short}`: 从 `openclaw status` 获取，如 `doubao`
- `{model_full}`: 完整模型 ID，如 `volcengine/doubao-seed-2.0-code`
- `--item-id E2E-01L1 E2E-06L1`: 可选，指定只跑部分 item
- `--skip-pack`: 推荐加上（不再需要打包，session 数据已在 Langfuse）

**注意**：
- 每个 task 耗时约 2-8 分钟，全量 14-20 个 case 串行约 1-3 小时
- SSH 工具超时设置：`timeout: 600000`（10 分钟）+ `run_in_background: true`
- 完成后 session 已上传 Langfuse 关联到 dataset run，可在 UI 查看

**部分 item 失败**：脚本会输出失败项和重跑命令，用 `--item-id` 重跑。

---

## Step 2: 生成 judge requests（本地，从 Langfuse 拉数据）

```bash
cd <repo>/cobo-agent-wallet

.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py langfuse \
  --run-name {run_name} \
  --dataset-name {dataset_name} \
  --dump-judge-requests ~/.caw-eval/runs/{run_name}/judge_req.json
```

脚本自动：
1. 从 Langfuse 拉取 dataset run 中的所有 trace
2. 对每个 trace 拉取 observations，重建 StructuredExtraction
3. 跑代码断言（pact gate、diagnostics）
4. 生成 judge prompt（**session 内容直接嵌入 prompt 里，不依赖本地文件**）

输出 `judge_req.json`，包含每个 item 的 `prompt` 和 `system_prompt`。

---

## Step 3: LLM Judge 评分（CC subagent 并行）

读取 `judge_req.json`，对每个 request 启动后台 Sonnet subagent 评分。**始终保持 4-5 个并行**。

```python
# 读取 judge_req.json，对每个 request 启动一个 subagent：
import json
run_dir = "~/.caw-eval/runs/{run_name}"
requests = json.loads(open(f"{run_dir}/judge_req.json").read())

# 对每个 request 启动后台 subagent
for req in requests:
    Agent(
        model="sonnet",
        run_in_background=True,
        description=f"Judge {req['item_id']}",
        prompt=f"""{req['system_prompt']}

{req['prompt']}

将 JSON 评分结果（严格按上面要求的格式）写入：{run_dir}/judge_{req['item_id']}.json"""
    )
```

**重要**：openclaw 模式下 prompt 已经包含完整 session 内容，subagent 不需要再 Read 任何文件。直接根据 prompt 中嵌入的 session 内容评分即可。

所有 judge 完成后，合并结果：

```bash
cd ~/.caw-eval/runs/{run_name}
python3 -c "
import json, glob
results = []
for f in sorted(glob.glob('judge_E2E-*.json')):
    results.append(json.loads(open(f).read()))
open('judge_results.json', 'w').write(json.dumps(results, indent=2, ensure_ascii=False))
print(f'merged {len(results)} judge results')
"
```

> **重要**：每条 judge result 必须含 `trace_id` **和** `item_id` 两个字段，否则
> `score_traces.py` 的 `load_judge_results()` 无法索引，会打印 "Loaded 0 judge result(s)"。
> subagent 评分 prompt 模板（`judge_cc.py`）已包含这两个字段；如手动合并需确保保留。

---

## Step 4: 应用评分到 Langfuse

```bash
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py langfuse \
  --run-name {run_name} \
  --dataset-name {dataset_name} \
  --judge-results ~/.caw-eval/runs/{run_name}/judge_results.json \
  --report
```

脚本自动：
1. 重新从 Langfuse 拉 trace + observations
2. 跑断言 + 应用预计算的 judge 评分
3. 计算 S1/S2/S3/TC/E2E 综合分
4. 上传所有维度评分到 Langfuse trace（含 ClickHouse 查询元数据）
5. 打印汇总

---

## Step 5: 生成报告（Opus subagent）

**必须**通过 Agent 工具启动 **Opus subagent** 写报告，主会话（Sonnet）不要直接写。

**为什么用 Opus**：报告阶段需要跨 case 根因归纳、P0/P1 权衡判断、上线决策这类深度推理，Sonnet 可行但质量明显弱。Opus subagent 在隔离 context 中按需读产物，成本比主 Opus 直接写低约 40%。

**Openclaw 模式差异**（相比 CC 评测）：
- **session 数据来自 Langfuse**：subagent 通过 `judge_req.json`（含 session_text）获取 session 内容，无需本地 .jsonl 文件
- **无 session_metrics.json**：Openclaw 评测不生成运行指标文件，报告中省略 Section 3（运行指标）
- **Skill 路径**：使用 `cobo-agentic-wallet-sandbox`（非 `-dev`）

### 5.1 主会话先整理"运行观察"

在启动 Opus 之前，主会话自己写一段 briefing（Opus subagent 看不到主会话历史，必须显式传入）：

- 哪些 case 失败/超时/需重试
- 环境异常（余额、faucet、API 超时、pending 卡住等）
- 跑了几轮，每轮有没有差异
- 任何影响分析结论的元信息

保存为临时变量或文件，注入到下面 prompt 的 `## 主会话观察` 段落。

### 5.2 启动 Opus subagent

```python
Agent(
    subagent_type="general-purpose",
    model="opus",
    description="生成 CAW 评测报告",
    prompt=f"""基于以下产物写 EVAL_REPORT.md。主会话已跑完评测，你负责深度分析。

## 产物路径
- Judge 结果: ~/.caw-eval/runs/{{run_name}}/judge_results.json（N case × 6 维度 score + reasoning）
- Judge 请求（含 session_text）: ~/.caw-eval/runs/{{run_name}}/judge_req.json
- Skill 源文件: {{repo}}/cobo-agent-wallet/sdk/skills/cobo-agentic-wallet-sandbox/
  - SKILL.md, references/pact.md, references/error-handling.md, references/security.md
- 报告模板参考: cobo-agent-wallet/sdk/skills/caw-eval/reports/eval-report-eval-oc-doubao-20260415-eth-v1.md
- 输出路径: cobo-agent-wallet/sdk/skills/caw-eval/reports/eval-report-{{run_name}}.md

## 主会话观察（重要，你看不到主会话历史但需要这些信息）
- 执行模型: {{model_full}}
- 运行轮次: {{n_runs}}
- 数据集: {{dataset_name}}
- 环境: Openclaw, {{server_info}}
- 异常: {{observations}}

## 分析要求
- Sonnet baseline: {{sonnet_baseline_e2e}} ({{sonnet_baseline_run}})

1. 先 Read judge_results.json 全文，按 e2e_composite 从低到高排序
2. 低分 case（<0.6）必须从 judge_req.json 中读对应 item 的 session_text 追根因；高分 case 不需读 session
3. 遇到疑似 skill 指令缺陷时，Read 对应 skill 文件验证（不要猜）
4. P0/P1/P2 按"风险严重度 × 发生频率 × 修复成本"排序，每条附依据
5. 上线建议三选一：可上 / 有条件上 / 建议延期，附理由
6. 报告末尾新增 Section：**修复收益预测**——对本次 P0/P1 问题逐一估算修复后 E2E 变化

## 产出约束
- 所有断言必须指向具体 case / tx / 代码行，避免空泛评价
- 失败 case 用"现象 → 根因 → Action Item"三段式
- 报告结构参考模板，不要另创结构

## 报告包含
1. 总览（E2E 综合分 + 任务完成率 + 与 baseline 对比）
2. 逐 Case 评分（按 E2E 从高到低）
3. 逐 Case 详细分析（仅低分 case 深入，高分 case 一行总结）
4. 按场景类型分析（transfer/swap/lend/dca/nft/bridge/stream/multi_step/should_refuse）
5. 阶段瓶颈分析（S1/S2/S3/TC）
6. 高频失败模式
7. 与基线对比分析
8. 改进建议（P0/P1/P2 分级，每条附理由）
9. 修复收益预测
"""
)
```

**模型选择说明**：
- 如果只想快速出草稿、人工过后 review，可将 `model="opus"` 改为 `model="sonnet"`（成本再降 60%，报告质量降级约 15%，关键 P0 判断和上线决策质量损失明显）。
- 若需严格成本控制，先用 `model="sonnet"` 出草稿，再用 `model="opus"` 启第二个 subagent 仅对"P0/P1 建议 + 上线决策"两段做 refine。

报告输出到：
```
cobo-agent-wallet/sdk/skills/caw-eval/reports/eval-report-{run_name}.md
```

---

## 对比分析

评分完成后，对比 Sonnet vs 弱模型的分数：

| 情况 | 含义 | 行动 |
|------|------|------|
| Sonnet 过 + 弱模型也过 | Skill 兼容性好 | 上线质量有保障 |
| Sonnet 过 + 弱模型挂 | Skill 指令不够清晰 | 简化 Skill 指令 |
| Sonnet 也挂 | Skill 有 bug | 必须修 |

---

## 服务器端脚本说明

| 脚本 | 子命令 | 说明 |
|------|--------|------|
| `run_eval_openclaw.py` | `run` | **服务器端**。脚本驱动串行执行 + 上传 Langfuse |
| `score_traces.py` | `langfuse` | **本地端**。从 Langfuse 拉 trace + observations 重建评分数据，无需本地 session 文件 |
| `score_traces.py` | `session` | **本地端**（CC 评测用）。读本地 .jsonl 评分 |

---

## Troubleshooting

| 问题 | 解决 |
|------|------|
| gcloud ssh 报 Python 错误 | `export CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11` |
| SSH 超时 | 增加 Bash 工具 timeout（600000 = 10 分钟），或用 `run_in_background=True`。推荐改用 `dispatch --fire-and-forget` 彻底消除 SSH 阻塞 |
| `openclaw: command not found` | SSH 中确认 PATH 包含 `/home/ubuntu/.npm-global/bin` |
| Langfuse 凭证缺失 | 检查 `~/.agents/skills/caw-eval/scripts/.env`（服务器）和本地 `.env` |
| `AttributeError: 'Langfuse' object has no attribute 'api'` | 服务器 langfuse 版本过新（4.2.0+ 移除了 `.api`）。修复：`pip3 install --user --break-system-packages "langfuse==4.0.6"` |
| `langfuse` 子命令报 v2 API 不可用 | 已用 `legacy.observations_v1` API，无影响。如仍报错检查 langfuse 包版本（需 4.0.6） |
| `Loaded 0 judge result(s)` | judge_results.json 每条缺少 `trace_id` 或 `item_id` 字段。重新合并时需从 judge_req.json 补充这两个字段 |
| `Agent "eval-xxx" already exists` | 上次评测异常退出残留同名 agent。脚本已内置预清理（Step 0），手动修复：`openclaw agents delete eval-xxx --force` |
| `pip install` 报 PEP 668 错误 | Debian 系统保护，加 `--break-system-packages` 参数即可 |
| task 超时 | `--timeout 900` 增加单 task 超时 |
| 部分 item 失败 | 用 `--item-id` 重跑失败项 |
| Langfuse 拉到的 trace 缺 observations | 确认 `run` 子命令的 upload 步骤未跳过（`--skip-upload` 关掉就行） |
| dispatch 日志为空但 Langfuse 有 trace | IAP tunnel 有 stdout 缓冲。数据实际已上传；可用 `--fire-and-forget` + nohup 避免此问题 |
