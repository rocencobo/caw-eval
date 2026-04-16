# Openclaw 评测服务器快速搭建

在新的 GCP 实例上从零开始搭建 CAW eval 环境。按顺序执行可在 15 分钟内就绪。

---

## 一、创建 GCP 实例

参考现有服务器配置（项目 `openclaw-keq9xwm4`，区域 `asia-east2-c`）：

```bash
export CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11
export PROJECT=openclaw-keq9xwm4
export ZONE=asia-east2-c
export NEW_SERVER=luochong-openclew-dev-v1-$(date +%Y%m%d-%H%M%S)-test

# 克隆现有服务器磁盘镜像创建（推荐，省去从头安装 openclaw）：
gcloud compute instances create "$NEW_SERVER" \
  --project="$PROJECT" \
  --zone="$ZONE" \
  --machine-type=n2-standard-2 \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --boot-disk-size=50GB \
  --scopes=cloud-platform \
  --no-address   # 不分配公网 IP，通过 IAP 访问
```

> 若有已配置好的磁盘快照，用 `--source-snapshot` 参数可跳过后续所有安装步骤，直接到「五、验证」。

SSH 进入：

```bash
gcloud compute ssh --zone "$ZONE" "$NEW_SERVER" \
  --tunnel-through-iap --project "$PROJECT" \
  -- "sudo su - ubuntu"
```

---

## 二、安装 openclaw

```bash
# 以 ubuntu 用户操作
sudo su - ubuntu

# 安装 Node.js（若未安装）
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# 安装 openclaw（npm global）
npm install -g openclaw --prefix ~/.npm-global
echo 'export PATH=$HOME/.npm-global/bin:$PATH' >> ~/.bashrc
source ~/.bashrc

# 验证
openclaw --version
```

---

## 三、安装 caw CLI + 钱包 onboarding

```bash
# 安装 caw
curl -fsSL https://raw.githubusercontent.com/CoboGlobal/cobo-mpc-sdk/main/scripts/install.sh | bash
# caw 安装在 ~/.cobo-agentic-wallet/bin/caw
echo 'export PATH=$HOME/.cobo-agentic-wallet/bin:$PATH' >> ~/.bashrc
source ~/.bashrc

# 验证
caw --version
```

**钱包 onboarding**（每台服务器独立钱包，不能复用）：

```bash
caw onboard --env sandbox
# 按提示逐步完成：输入 API key → 创建 TSS node → 生成 MPC key → 激活钱包
# 最终 signing_ready=true 才算完成
caw status   # 确认 healthy=true, signing_ready=true
```

> onboarding 约需 3-5 分钟，TSS key 生成需等待网络协调。如卡住超过 5 分钟，重启 `caw onboard`，已完成的步骤会自动跳过。

---

## 四、安装 Python 依赖 + 配置凭证

```bash
# 安装系统 pip（Debian 12 默认不带）
sudo apt-get update && sudo apt-get install -y python3-pip

# ⚠️ 必须 pin langfuse==4.0.6
# langfuse 4.2.0+ 移除了 Langfuse.api 属性，会报 AttributeError
pip3 install --user --break-system-packages \
  "langfuse==4.0.6" \
  python-dotenv \
  requests

# 验证版本
python3 -c "import langfuse; print(langfuse.__version__)"  # 应输出 4.0.6
```

**配置 Langfuse + CAW 凭证**（从本地 Mac 推送）：

```bash
# 在本地 Mac 执行：
export CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11
gcloud compute scp --zone "$ZONE" --project "$PROJECT" --tunnel-through-iap \
  ~/.agents/skills/caw-eval/scripts/.env \
  ubuntu@"$NEW_SERVER":~/.agents/skills/caw-eval/scripts/.env

# 验证（在服务器上）：
cat ~/.agents/skills/caw-eval/scripts/.env
# 应包含 LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST
```

**同步评测脚本**（若服务器未自动同步 skills）：

```bash
# 从本地 Mac 推送：
gcloud compute scp --zone "$ZONE" --project "$PROJECT" --tunnel-through-iap \
  --recurse \
  <repo>/cobo-agent-wallet/sdk/skills/caw-eval/scripts/ \
  ubuntu@"$NEW_SERVER":~/.agents/skills/caw-eval/scripts/
```

---

## 五、为钱包充值

评测需要的最低余额（Ethereum Sepolia）：
- **SETH ≥ 0.1**（gas + transfer/swap 操作）
- **SETH_USDC ≥ 14**（DeFi 类 case 需要 USDC）

```bash
# 查询当前余额
caw wallet balance --chain-id SETH

# 获取钱包地址（用于接收转账）
caw status  # 找到 wallet.addresses[].address
```

**充值方式**：

```bash
# 方式 1：从余额充裕的已有服务器转入 SETH（openclaw agent 操作）
# 在已有服务器上执行：
openclaw agent --agent main --message \
  "转 0.15 SETH 到 <新服务器钱包地址>（Ethereum Sepolia）。已授权操作，直接创建 pact 并执行。"

# 方式 2：caw faucet 领测试币（每天有限额）
caw faucet --token-id SETH

# USDC 充值（swap ETH → USDC）：
openclaw agent --agent main --message \
  "把 0.01 ETH 换成 USDC（Ethereum Sepolia，Uniswap V3）。已授权操作，直接创建 pact 并执行，不需要确认。完成后告诉我拿到了多少 USDC 和交易 hash。"
```

> 当前参考汇率：1 ETH ≈ \$2300，0.01 ETH ≈ 23 USDC。如需 14 USDC，swap 0.007 ETH 即可留出余量。

---

## 六、验证清单

```bash
export PATH=$HOME/.npm-global/bin:$HOME/.cobo-agentic-wallet/bin:$PATH

# ✅ openclaw 可用
openclaw status 2>&1 | head -3

# ✅ caw 可用，钱包就绪
caw status | grep -E "healthy|signing_ready"
# 预期：healthy=true, signing_ready=true

# ✅ 余额充足
caw wallet balance --chain-id SETH | python3 -c "
import json, sys
d = json.load(sys.stdin)
for b in d.get('result', []):
    t, amt = b['token_id'], float(b.get('total', 0))
    if amt > 0: print(f'{t}: {amt}')
"
# 预期：SETH ≥ 0.1, SETH_USDC ≥ 14

# ✅ Python 依赖正确
python3 -c "import langfuse; assert langfuse.__version__ == '4.0.6', langfuse.__version__; print('langfuse OK')"

# ✅ 脚本存在
ls ~/.agents/skills/caw-eval/scripts/run_eval_openclaw.py

# ✅ .env 已配置
python3 -c "
from dotenv import load_dotenv; import os
load_dotenv(os.path.expanduser('~/.agents/skills/caw-eval/scripts/.env'))
assert os.getenv('LANGFUSE_PUBLIC_KEY'), 'LANGFUSE_PUBLIC_KEY missing'
print('env OK')
"
```

全部通过后，服务器就绪，可加入 `SKILL-openclaw.md` 的服务器池并运行评测。

---

## 七、加入服务器池

验证通过后，更新 `SKILL-openclaw.md` 中的 `SERVERS` 列表：

```bash
SERVERS=(
  # 已有服务器...
  "$NEW_SERVER:$ZONE:$PROJECT"   # 新增
)
```

---

## 常见坑

| 坑 | 现象 | 解决 |
|----|------|------|
| langfuse 版本错 | `AttributeError: 'Langfuse' object has no attribute 'api'` | `pip3 install --user --break-system-packages "langfuse==4.0.6"` |
| PATH 未生效 | `caw: command not found` / `openclaw: command not found` | SSH 命令中显式 `export PATH=...`，不能依赖 `.bashrc` 自动加载 |
| pip install 被拒绝 | `error: externally-managed-environment` | 加 `--break-system-packages` |
| onboarding 卡住 | TSS key 长时间不就绪 | 等待或重跑 `caw onboard`，已完成步骤自动跳过 |
| USDC swap 卡确认 | agent 等待用户确认，不自动执行 | prompt 中加 "已授权操作，直接创建 pact 并执行，不需要确认" |
| scp .env 权限问题 | Permission denied | 目标目录不存在，先 `mkdir -p ~/.agents/skills/caw-eval/scripts/` |
| IAP 连接失败 | Connection timed out | `gcloud auth login` 重新认证；或检查服务器是否已关机 |
