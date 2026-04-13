---
name: caw-eval-openclaw
metadata:
  version: "2026.04.12.1"
description: |
  在 Openclaw 上跑 CAW Agent 评测。
  Use when: 用户说"跑评测", "测评 CAW", "eval", "评估"。
---

# CAW 评测

按以下 3 步执行。每步完成后继续下一步。

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
