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

## Step 1.5: 钱包余额预检

并行查询每台服务器的 caw 钱包余额，确认资金充足：

```bash
mkdir -p /tmp/oc-balance
for spec in "${SERVERS[@]}"; do
  IFS=':' read -r name zone project <<< "$spec"
  (gcloud compute ssh --zone "$zone" "$name" --tunnel-through-iap --project "$project" \
     -- "sudo su - ubuntu -c 'export PATH=/home/ubuntu/.npm-global/bin:/home/ubuntu/.cobo-agentic-wallet/bin:\$PATH; caw wallet balance 2>&1'" > /tmp/oc-balance/$name.txt 2>&1
  ) &
done
wait
# 解析并汇总
for f in /tmp/oc-balance/*.txt; do
  name=$(basename $f .txt | sed 's/luochong-openclew-dev-v1-//')
  python3 -c "
import json
raw = open('$f').read()
idx = raw.find('{')
if idx == -1: print(f'  {\"$name\"}: 无数据'); exit()
d = json.loads(raw[idx:])
for r in d.get('result', []):
    print(f'  {\"$name\":30s} {r[\"token_id\"]:12s} available={r[\"amount\"]:>24s}')
"
done
```

**最低余额要求**（Ethereum Sepolia 评测）：
- **SETH ≥ 0.1**（gas + swap/transfer 操作消耗）
- **SETH_USDC ≥ 14**（DeFi 类 case 需要 USDC 做 deposit/bridge/stream）

不足时的补充方法：
- **SETH 不足**：从余额充裕的服务器转入（通过 `openclaw agent --agent main --message "转 X SETH 到 <地址>（Ethereum Sepolia）"`），或用 `caw faucet` 领测试币
- **USDC 不足**：并行 SSH 到各服务器，用 `openclaw agent` 执行 swap。注意 prompt 需要明确授权，避免 agent 卡在确认环节：

```bash
MSG="把 0.005 ETH 换成 USDC（Ethereum Sepolia，Uniswap V3）。这是已授权操作，直接创建 pact 并执行，不需要确认。完成后告诉我拿到了多少 USDC 和交易 hash。"
for spec in "${SERVERS[@]}"; do
  IFS=':' read -r name zone project <<< "$spec"
  (gcloud compute ssh --zone "$zone" "$name" --tunnel-through-iap --project "$project" \
     -- "sudo su - ubuntu -c 'export PATH=/home/ubuntu/.npm-global/bin:/home/ubuntu/.cobo-agentic-wallet/bin:\$PATH; \
     openclaw agent --agent main --message \"$MSG\" 2>&1'" > /tmp/oc-swap/$name.txt 2>&1
  ) &
done
wait
```

当前 ETH ≈ $2336，0.005 ETH ≈ 11.6 USDC。swap 涉及 wrap→approve→swap 三步链上交易，单台约 2-5 分钟。

## Step 2: 并行 dispatch（动态队列模式，推荐）

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
1. 从 Langfuse 拉取 dataset items 列表，放入任务队列
2. **动态队列模式（默认）**：4 台服务器各启动一个 worker 协程，每台完成一个 item 后立即从队列取下一个，空闲服务器不会等待；快台多跑、慢台少跑，所有 item 跑完即结束
3. 每台 SSH 到服务器，远端串行执行单个 item（每次只跑 1 个）
4. 各台收集 session 并上传 Langfuse（`--skip-pack`，无需打包下载）
5. 本地日志：`~/.caw-eval/runs/$RUN_NAME/dispatch-logs/<server>-<item_id>.log`（每个 item 独立日志）

**选项说明**：
- 默认（无额外 flag）：动态队列，阻塞等待所有 item 完成，适合正式评测
- `--fire-and-forget`：静态预分配（i % N）+ nohup 后台启动，SSH 立即返回；搭配 `--watch` 轮询进度。适合不想等待、评测时间较长时
- `--static`：静态预分配但 SSH 阻塞等待；调试用

**部分 item 失败**：dispatch 会 `exit 1` 并列出失败 item 和重跑命令，用 `--item-id` 重跑。

## Step 3: 评分（参考 references/run-eval-openclaw.md Step 2-5）

**推荐：dispatch 后立即启动 --watch 轮询**（与 dispatch 并行，实现流水线）：

```bash
# dispatch 后台启动（Step 2），立即运行 watch 开始监听新 trace：
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py langfuse \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --watch \
  --expected-count 14 \
  --dump-judge-requests ~/.caw-eval/runs/$RUN_NAME/judge_req.json
# 每有新 trace 上传，自动生成对应 judge req 并追加到文件
# 达到 expected-count 后自动退出

# Step 3: 启动 CC subagent 并行评分（详见 references/run-eval-openclaw.md Step 3）

# Step 4: 合并 judge 结果并应用到 Langfuse
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py langfuse \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --judge-results ~/.caw-eval/runs/$RUN_NAME/judge_results.json \
  --report

# Step 5: 生成分析报告（Opus subagent，见 references/run-eval-cc.md Step 9）
```

**普通模式**（等 dispatch 全部完成后再运行）：

```bash
# Step 2: 一次性生成所有 judge requests
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py langfuse \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --dump-judge-requests ~/.caw-eval/runs/$RUN_NAME/judge_req.json
```

详细步骤（含 judge subagent 编排、报告模板）参考 [references/run-eval-openclaw.md](./references/run-eval-openclaw.md)。

## 新服务器快速搭建

新建 openclaw 评测服务器（GCP 实例创建 → openclaw/caw 安装 → onboarding → 充值 → 验证）：

→ [server-setup.md](./references/server-setup.md)

---

## Troubleshooting

| 问题 | 解决 |
|------|------|
| gcloud ssh 报 Python 错误 | 已在 Step 1 `export CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11` |
| 某台 IAP 连接失败 | 在 Mac 单独跑一次 `gcloud compute ssh ...` 确认能连上，必要时 `gcloud auth login` |
| 4 台 model 不一致 | 回 openclaw 服务器用 `openclaw model` 对齐后再 dispatch |
| 单台 run 失败但其他 ok | 看 `~/.caw-eval/runs/$RUN_NAME/dispatch-logs/<server>.log`，用 `--item-id` 重跑失败 item（不分发到失败那台） |
| 远端 Langfuse 凭证缺失 | 每台 openclaw 的 `~/.agents/skills/caw-eval/scripts/.env` 需存在 |
| 单 task 超时 | 加 `--timeout 900`（默认 600） |
| `AttributeError: Langfuse.api` | 远端 langfuse 版本过新。修复：`pip3 install --user --break-system-packages "langfuse==4.0.6"` |
| `Agent "eval-xxx" already exists` | 上次异常残留。脚本已内置预清理，手动修复：`openclaw agents delete eval-xxx --force` |
| `Loaded 0 judge result(s)` | judge_results.json 每条缺 `trace_id`/`item_id` 字段，需从 judge_req.json 补充后重新合并 |
| SSH 阻塞等待过久 | 使用 `--fire-and-forget` + `--watch` 流水线模式，dispatch 立即返回 |
| SETH 余额不足 | 从余额充裕的服务器用 openclaw agent 转入（`转 0.1 SETH 到 <地址>`），或 `caw faucet` |
| USDC 余额不足 | Step 1.5 的 swap 脚本并行执行。prompt 必须含"已授权操作，不需要确认"，否则 agent 会卡在确认环节 |
| swap agent 卡在确认 | 非交互模式下 agent 可能要求确认。确保 prompt 包含明确授权语句，或 SSH 进服务器用 `openclaw tui` 交互式操作 |
