---
name: caw-eval-openclaw
metadata:
  version: "2026.04.15.1"
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

## Step 1: 运行评测

脚本自动逐个执行所有 task、收集 session、上传 Langfuse、打包。运行以下命令：

```bash
cd /home/luochong_cobo_com/skills/caw-eval/scripts

DATASET_NAME=caw-agent-eval-seth-v2

# 从 openclaw status 读取当前模型
MODEL_FULL=$(openclaw status | awk -F' \| ' 'NR==2{print $3}')
MODEL_SHORT=$(echo "$MODEL_FULL" | sed 's|.*/||' | cut -d'-' -f1)
RUN_NAME=eval-oc-${MODEL_SHORT}-$(date +%Y%m%d-%H%M)

python3 run_eval_openclaw.py run \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --model "$MODEL_SHORT" \
  --model-full "$MODEL_FULL"
```

脚本会为每个 task 自动创建隔离 agent → 执行 → 收集 session → 清理 agent，串行逐个执行。

完成后输出打包路径和 **`gcloud compute scp` 下载命令**，将该命令完整地展示给用户，例如：

```
打包完成: /tmp/eval-oc-<run-name>.tar.gz (X.X MB)

下载到本地（在 Mac 终端执行）：
  gcloud compute scp <实例名>:/tmp/eval-oc-<run-name>.tar.gz ~/Downloads/ --zone=<zone> --project=<project-id>
```

把上面的实际输出（含真实实例名、zone、project）原样告诉用户，让用户在 Mac 本地终端执行即可下载。
