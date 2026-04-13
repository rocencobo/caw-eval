---
name: caw-eval-openclaw
metadata:
  version: "2026.04.13.1"
description: |
  在 Openclaw 服务器上跑 CAW Agent 弱模型评测。
  Use when: 用户说"弱模型评测"、"openclaw 评测"、"弱模型验证"、"caw 弱模型测试"、"模型兼容性测试"。
  通用词（"跑评测"/"eval"）不触发本 SKILL，改由 caw-eval（本地 CC）处理。
---

# CAW 评测（Openclaw 弱模型）

按以下步骤执行。每步完成后继续下一步。

## Step 0: 环境识别（必做）

执行任何后续步骤前，先确认当前环境：

```bash
[[ "$(hostname)" == *openclaw* ]] && echo "env=openclaw" || echo "env=local"
```

- `env=openclaw`：继续下面步骤。
- `env=local`：**本 SKILL 只能在 openclaw 服务器运行**。告诉用户：
  > "当前在本地，openclaw 弱模型评测 SKILL 不适用。请使用 `caw-eval` skill（说'跑评测'触发 CC 评测）。若确实要跑弱模型验证，请 ssh 到 openclaw 服务器再触发本 SKILL。"
  然后停止。

## Step 1: 生成评测任务

运行以下命令：

```bash
cd /home/luochong_cobo_com/skills/caw-eval/scripts
python3 run_eval_openclaw.py prepare --dataset-name caw-agent-eval-seth-v2
```

命令会在 `/tmp/eval-prompts/` 下生成文件。读取 `/tmp/eval-prompts/_all_tasks.txt` 的内容。

## Step 2: 并行执行任务（3 个并发）

对 `_all_tasks.txt` 中的每个 Task，提取 ```prompt 和 ``` 之间的内容作为 subagent 的 prompt。

**执行策略：始终保持 3 个 task subagent 并行运行**
1. 一次启动 3 个 task subagent（分别执行不同的 Task）
2. 任意一个 task 完成后，立即启动下一个未执行的 Task，保持 3 并发
3. 直到所有 Task 都启动并完成

**注意：**
- 不要等所有 3 个都完成再启动下一批，必须"完成一个补一个"
- 不需要分析结果，不需要上传，只需要执行完所有 Task
- 每个 task subagent 独立运行，互不干扰

## Step 3: 收集并打包

所有 Task 执行完后，运行：

```bash
cd /home/luochong_cobo_com/skills/caw-eval/scripts
python3 run_eval_openclaw.py collect --run-name eval-oc-$(date +%Y%m%d)
python3 run_eval_openclaw.py pack --run-name eval-oc-$(date +%Y%m%d)
```

告诉用户打包文件的路径和下载命令。
