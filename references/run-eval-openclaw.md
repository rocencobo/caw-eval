# Openclaw 弱模型验证：完整说明

## 概述

在 Openclaw 服务器上用弱模型验证 CAW Skill 的兼容性。脚本驱动架构：

```
服务器（Python 脚本编排）               本地 Claude Code（Sonnet）
┌───────────────────────────┐       ┌─────────────────────────┐
│ python run_eval_openclaw  │       │                         │
│   .py run                 │       │ 导入 session            │
│                           │       │ LLM Judge 评分          │
│ 串行执行每个 task:         │       │ 上传 Langfuse           │
│   agents add → agent CLI  │       │ 生成报告                │
│   → collect → delete      │       │                         │
│   → upload → pack         │       │                         │
└───────────────────────────┘       └─────────────────────────┘
         ↓ gcloud scp
```

脚本通过 `openclaw agent` CLI 驱动弱模型逐个执行评测 task，弱模型不参与编排。

---

## 前置条件（一次性准备）

### 服务器环境

```bash
# 1. caw 环境就绪
export PATH="$HOME/.cobo-agentic-wallet/bin:$PATH"
caw status          # healthy=true, signing_ready=true
caw wallet balance  # SETH >= 0.2

# 2. 充值测试币（如果余额不足）
# 从公共 Sepolia faucet 领取：
#   https://www.alchemy.com/faucets/ethereum-sepolia（每次 0.5 SETH）
#   https://faucets.chain.link/sepolia（每次 0.1 SETH）

# 3. cobo-agentic-wallet-sandbox skill 已安装
npx skills add cobosteven/cobo-agent-wallet-manual --skill cobo-agentic-wallet-sandbox --yes --global

# 4. 评测脚本已部署到服务器
# 需要以下文件：
#   run_eval_openclaw.py
#   eval_utils.py
#   upload_session.py
#   .env（含 Langfuse 凭证）
# 放到统一目录，如 /home/luochong_cobo_com/skills/caw-eval/scripts/

# 5. Python 依赖
pip install langfuse python-dotenv

# 6. openclaw CLI 在 PATH 中
which openclaw  # 应输出路径，如 /home/ubuntu/.npm-global/bin/openclaw
```

### 本地环境

确认 Claude Code 能访问项目代码即可。无需额外准备。

---

## 操作流程

### 第一步：在服务器上跑评测

在服务器终端运行（不需要通过 openclaw 对话）：

```bash
cd /home/luochong_cobo_com/skills/caw-eval/scripts

DATASET_NAME=caw-agent-eval-seth-v2
MODEL_FULL=$(openclaw status | awk -F' | ' 'NR==2{print $3}')
MODEL_SHORT=$(echo "$MODEL_FULL" | sed 's|.*/||' | cut -d'-' -f1)
RUN_NAME=eval-oc-${MODEL_SHORT}-$(date +%Y%m%d-%H%M)

python3 run_eval_openclaw.py run \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --model "$MODEL_SHORT" \
  --model-full "$MODEL_FULL"
```

脚本自动执行：
1. 从 Langfuse 拉取 dataset items
2. 对每个 item 串行执行：创建隔离 agent → 通过 `openclaw agent` CLI 发送 task → 收集 session → 删除 agent
3. 上传 session 到 Langfuse
4. 打包 session 文件并输出下载命令

完成后会输出打包文件路径和下载命令。

> **部分 item 失败时**：脚本会输出失败项列表和重跑命令，用 `--item-id` 参数只重跑失败项。

### 第二步：下载到本地

在 Mac 终端执行脚本给出的下载命令：

```bash
gcloud compute scp <实例名>:/tmp/eval-oc-*.tar.gz ~/Downloads/ \
  --zone=<zone> --project=<project-id>

mkdir -p /tmp/oc-sessions
tar xzf ~/Downloads/eval-oc-*.tar.gz -C /tmp/oc-sessions/
```

### 第三步：在 Claude Code 中评分

在 Claude Code 中说：

```
读 cobo-agent-wallet/sdk/skills/caw-eval/SKILL.md
导入 /tmp/oc-sessions/ 的 openclaw session，run name 为 eval-oc-doubao-YYYYMMDD-HHMM，然后评分出报告
```

Claude Code 自动执行：import-sessions → LLM Judge 评分 → 上传 Langfuse → 生成报告。

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
| `run_eval_openclaw.py` | `run` | **推荐**。脚本驱动串行执行：为每个 task 创建隔离 agent、通过 CLI 执行、收集 session、清理 agent |
| | `prepare` | 从 Langfuse 拉 items，生成 task prompt 文件（传统 wrapper 模式用） |
| | `import-sessions` | 从 /tmp/eval-sessions/ 导入 wrapper 写入的 session JSON |
| | `collect` | 在 openclaw session 目录中 grep 搜索 eval 标记，收集到统一目录 |
| | `upload` | 上传 session 到 Langfuse |
| | `pack` | 打包 session 目录为 tar.gz，输出下载命令 |
| `eval_utils.py` | — | 公共工具（Langfuse 客户端/数据集操作/上传函数） |
| `upload_session.py` | — | session.jsonl → Langfuse trace |

---

## Troubleshooting

| 问题 | 解决 |
|------|------|
| `openclaw: command not found` | 确认 openclaw 在 PATH 中（`export PATH=/home/ubuntu/.npm-global/bin:$PATH`）|
| `run` 子命令 agents add 失败 | 检查 `openclaw agents list`，确认没有同名 agent 残留；手动 `openclaw agents delete eval-xxx --force` 清理 |
| task 超时 | 默认 600 秒，DeFi 操作可能更久，用 `--timeout 900` 增加超时 |
| session 文件格式不对 | `run` 子命令直接收集 otel JSONL，无需转换；如果用传统 `collect` 模式，确认文件名以 `E2E-` 开头 |
| prepare 报 "Langfuse credentials not set" | 检查 `.env` 文件是否存在且凭证正确 |
| gcloud scp 报错 | 确认 zone/project-id 正确（在服务器上查 metadata） |
| 本地导入后评分报错 | 确认 session 文件是 `.jsonl` 格式，文件名以 `E2E-` 开头 |
| 部分 task 失败需要重跑 | 用 `--item-id E2E-01L1 E2E-02L1` 只重跑指定 item，run 目录会累积结果 |
