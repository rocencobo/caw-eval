---
name: caw-eval-openclaw
metadata:
  version: "2026.04.15.2"
description: |
  从本地 Mac 并行 dispatch 4 台 Openclaw 服务器跑 CAW Agent 弱模型评测（每台同时只跑 1 个 task）。
  Use when: 用户说"弱模型评测"、"openclaw 评测"、"弱模型验证"、"caw 弱模型测试"、"模型兼容性测试"、"4 台并行评测"。
  通用词（"跑评测"/"eval"）不触发本 SKILL，改由 caw-eval（本地 CC）处理。
---

# CAW 评测（Openclaw 弱模型，4 服务器并行）

本 SKILL 从**本地 Mac** 通过 `gcloud compute ssh` 并行调度 4 台 openclaw 服务器跑评测。
items 按 `i % 4` 轮询分配到 4 台，每台串行跑自己那份（同时只跑 1 个 task），都上传到同一个 Langfuse dataset run。

按以下步骤执行。每步完成后继续下一步。

## Step 0: 环境识别（必做）

```bash
[[ "$(hostname)" == *openclaw* ]] && echo "env=openclaw" || echo "env=local"
```

- `env=local`：继续（确保已 `gcloud auth login` 且 IAP 通道可用）。
- `env=openclaw`：提示用户并停止：
  > "本 SKILL 从本地 Mac 并行调度 4 台 openclaw 服务器，请回到本地 Mac 终端运行。"

## Step 1: 服务器池与模型对齐检查

4 台 openclaw 服务器固定清单（格式 `name:zone:project`）：

```bash
export CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11

SERVERS=(
  "luochong-openclew-dev-v1-20260318-070641:asia-east2-a:openclaw-keq9xwm4"
  "luochong-openclew-dev-v1-20260415-0253420-test1:asia-east2-c:openclaw-keq9xwm4"
  "luochong-openclew-dev-v1-20260415-025458-test2:asia-east2-c:openclaw-keq9xwm4"
  "luochong-openclew-dev-v1-20260415-025551-test3:asia-east2-c:openclaw-keq9xwm4"
)
```

并行读取 4 台的 `openclaw status`，确认模型一致：

```bash
for spec in "${SERVERS[@]}"; do
  IFS=':' read -r name zone project <<< "$spec"
  (echo "=== $name ==="
   gcloud compute ssh --zone "$zone" "$name" --tunnel-through-iap --project "$project" \
     -- "sudo su - ubuntu -c 'export PATH=/home/ubuntu/.npm-global/bin:\$PATH; openclaw status 2>&1 | head -3'"
  ) &
done
wait
```

人工比对 4 台输出，确认 model 完全一致。不一致则先对齐，再继续。

从 status 提取模型字段，保存到变量：

```bash
MODEL_FULL="<从 status 复制，如 volcengine/doubao-seed-2.0-code>"
MODEL_SHORT="<短标识，如 doubao>"
```

## Step 2: 并行 dispatch

```bash
cd <repo>/cobo-agent-wallet

DATASET_NAME=caw-agent-eval-seth-v2
RUN_NAME=eval-oc-${MODEL_SHORT}-$(date +%Y%m%d-%H%M)

.venv/bin/python sdk/skills/caw-eval/scripts/run_eval_openclaw.py dispatch \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --model "$MODEL_SHORT" \
  --model-full "$MODEL_FULL" \
  $(for s in "${SERVERS[@]}"; do echo --server "$s"; done)
```

`dispatch` 子命令会：
1. 从 Langfuse 拉取 dataset items 列表
2. 按 `i % 4` 轮询分配（14 items → 4/4/3/3）
3. 并行 `gcloud compute ssh` 到每台跑 `run_eval_openclaw.py run --item-id <chunk>`
4. 每台串行执行自己 chunk，同时只跑 1 个 task（openclaw agent 是单会话）
5. 每台各自收集 session 并上传 Langfuse（`--skip-pack`，无需打包下载）
6. 实时日志写到 `~/.caw-eval/runs/$RUN_NAME/dispatch-logs/<server>.log`

**跟踪某台进度**：
```bash
tail -f ~/.caw-eval/runs/$RUN_NAME/dispatch-logs/luochong-openclew-dev-v1-20260415-0253420-test1.log
```

**部分服务器失败**：dispatch 会 `exit 1` 并列出失败服务器，查对应日志排查后可用 `--item-id` 重跑失败项（单台或全部）。

## Step 3: 评分（参考 references/run-eval-openclaw.md Step 2-5）

dispatch 完成后所有 trace 已在 Langfuse 同一个 dataset run 下。继续走本地评分流程：

```bash
# Step 2: 生成 judge requests
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py langfuse \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --dump-judge-requests ~/.caw-eval/runs/$RUN_NAME/judge_req.json

# Step 3: 启动 CC subagent 并行评分（详见 references/run-eval-openclaw.md Step 3）

# Step 4: 合并 judge 结果并应用到 Langfuse
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py langfuse \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --judge-results ~/.caw-eval/runs/$RUN_NAME/judge_results.json \
  --report

# Step 5: 生成分析报告（Opus subagent，见 references/run-eval-cc.md Step 9）
```

详细步骤（含 judge subagent 编排、报告模板）参考 [references/run-eval-openclaw.md](./references/run-eval-openclaw.md)。

## Troubleshooting

| 问题 | 解决 |
|------|------|
| gcloud ssh 报 Python 错误 | 已在 Step 1 `export CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11` |
| 某台 IAP 连接失败 | 在 Mac 单独跑一次 `gcloud compute ssh ...` 确认能连上，必要时 `gcloud auth login` |
| 4 台 model 不一致 | 回 openclaw 服务器用 `openclaw model` 对齐后再 dispatch |
| 单台 run 失败但其他 ok | 看 `~/.caw-eval/runs/$RUN_NAME/dispatch-logs/<server>.log`，用 `--item-id` 重跑失败 item（不分发到失败那台） |
| 远端 Langfuse 凭证缺失 | 每台 openclaw 的 `~/.agents/skills/caw-eval/scripts/.env` 需存在 |
| 单 task 超时 | 加 `--timeout 900`（默认 600） |
