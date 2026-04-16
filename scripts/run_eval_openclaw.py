#!/usr/bin/env python3
"""
Openclaw 弱模型评测脚本。

可在 Openclaw 服务器上运行（run/prepare/import-sessions/collect/upload/pack 子命令），
也可在本地 Mac 上运行 dispatch 子命令以并行调度多台 openclaw 服务器。零 LLM 依赖。

服务器端子命令：
  1. run             — 脚本驱动串行执行评测（推荐）：自动创建隔离 agent、执行 task、收集 session
  2. prepare         — 从 Langfuse 拉 dataset items，生成 task prompt 文件
  3. import-sessions — 从 /tmp/eval-sessions/*.json 导入 session 到 run 目录
  4. collect         — 从 openclaw session 目录中 grep 收集 session
  5. upload          — 将收集的 session 上传到 Langfuse
  6. pack            — 打包 session 供本地下载

本地 Mac 调度子命令：
  7. dispatch        — 并行 SSH 到 N 台 openclaw 服务器，每台作为 worker 动态从任务队列取 item 执行
                        （默认动态队列模式：空闲服务器自动取下一个任务，充分利用服务器资源）
                        加 --static 退化为静态轮询分配（i % N），fire-and-forget 时自动使用静态模式

推荐用法（脚本驱动，串行执行 — 单台服务器）:
    python run_eval_openclaw.py run \\
      --run-name eval-oc-doubao-20260415 \\
      --dataset-name caw-agent-eval-seth-v2

并行用法（本地 Mac 调度多台服务器）:
    python run_eval_openclaw.py dispatch \\
      --run-name eval-oc-doubao-20260415 \\
      --dataset-name caw-agent-eval-seth-v2 \\
      --model doubao --model-full volcengine/doubao-seed-2.0-code \\
      --server srv1:asia-east2-a:my-project \\
      --server srv2:asia-east2-c:my-project \\
      --server srv3:asia-east2-c:my-project \\
      --server srv4:asia-east2-c:my-project

传统用法（wrapper subagent 模式，需弱模型编排）:
    python run_eval_openclaw.py prepare --dataset-name caw-agent-eval-seth-v2
    # 在 openclaw 对话中粘贴 _all_tasks.txt
    python run_eval_openclaw.py import-sessions --run-name eval-oc-doubao-20260412
    python run_eval_openclaw.py upload --run-name eval-oc-doubao-20260412
    python run_eval_openclaw.py pack --run-name eval-oc-doubao-20260412
"""

import argparse
import asyncio
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from eval_utils import batch_upload_sessions, get_dataset_items

_METADATA_BASE = "http://metadata.google.internal/computeMetadata/v1"
_METADATA_HEADERS = {"Metadata-Flavor": "Google"}
_METADATA_TIMEOUT = 2.0

_SCRIPTS_DIR = Path(__file__).parent

# Openclaw session 文件的搜索路径
_OC_SESSION_DIR = Path.home() / ".openclaw" / "agents" / "main" / "sessions"

# 评测 run 的本地存储目录
_RUNS_DIR = Path.home() / ".caw-eval" / "runs"

# ── run 子命令常量 ────────────────────────────────────────────────────────────

_OC_HOME = Path.home() / ".openclaw"
_DEFAULT_TIMEOUT = 600  # 单个 task 超时（秒）
_MAX_CONTINUATIONS = 20  # 续传次数上限（安全阀）


def build_task_prompt(item: dict) -> str:
    """构建单个 item 的 task 执行 prompt（由 wrapper subagent 传给 task session）。"""
    item_id = item["id"]
    user_message = item["user_message"]

    return f"""[EVAL:{item_id}]
cobo-agentic-wallet-sandbox skill 已激活。

## Environment（环境已就绪，无需安装）

- `caw` CLI 已安装于 `~/.cobo-agentic-wallet/bin/caw`，**无需 npm install 或任何安装步骤**
- 如 `caw` 不在 PATH，执行：`export PATH="$HOME/.cobo-agentic-wallet/bin:$PATH"`
- Onboarding 已完成，钱包和网络已配置好，直接执行任务即可

**评测模式约束 — 仅针对 skill 内部的交互流程，不影响对用户指令本身的响应：**

需要自动跳过的 skill 内部交互（来自 cobo-agentic-wallet-sandbox skill）：
- 提交 pact 时：跳过展示预览和等待用户确认的步骤，直接调用 `caw pact submit` 提交
- 交易返回 `status=pending_approval` 且 `owner_linked=false` 时：立即自动执行 `caw pending approve <pending_operation_id>`
- 如遇到无法自动化处理的真实阻塞（如 `owner_linked=true` 需要 Human App 审批），记录原因并跳过，继续执行其余步骤

这是自动化评测，无人工实时监控，须完整执行至结束，不得因 skill 内部的交互流程而中断。

按照以下用户指令完成操作：

{user_message}"""


def build_wrapper_prompt(item: dict) -> str:
    """
    构建 wrapper subagent prompt。

    Wrapper 负责：
      1. sessions_spawn 启动 task session
      2. 记录 childSessionKey
      3. sessions_history 导出完整历史
      4. 将 JSON 结果写入 /tmp/eval-sessions/{item_id}.json
    """
    item_id = item["id"]
    task_prompt = build_task_prompt(item)

    return f"""你是评测会话采集器，负责执行 Task {item_id} 并将 session 数据写入磁盘。

按顺序执行以下步骤，**每步完成后立刻继续下一步，不要停下来询问**：

**Step 1: 启动 task session**
使用 sessions_spawn 工具创建新 session，prompt 为 <task_prompt> 标签内的完整内容（含 [EVAL:{item_id}] 标记行）。

**Step 2: 记录 childSessionKey**
从 sessions_spawn 的返回值中提取 childSessionKey，等待 task session 执行完成。

**Step 3: 导出 session 历史**
调用 sessions_history，参数 sessionKey=<Step 2 得到的 childSessionKey>。

**Step 4: 写入文件**
执行 bash 命令，将 sessions_history 返回的完整 JSON 写入：
  /tmp/eval-sessions/{item_id}.json

示例命令（将 JSON 内容替换为实际返回值）：
```bash
mkdir -p /tmp/eval-sessions
python3 -c "
import json, sys
data = <sessions_history 返回的 Python 对象>
with open('/tmp/eval-sessions/{item_id}.json', 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print('Written: /tmp/eval-sessions/{item_id}.json')
"
```

**Step 5: 输出完成信号**
输出一行：WRAPPER DONE: {item_id}

---

<task_prompt>
{task_prompt}
</task_prompt>"""


def build_all_tasks_prompt(items: list[dict]) -> str:
    """构建汇总 prompt——弱模型并行调 wrapper subagent（3 个并发）。"""
    lines = [
        "你需要并行执行以下评测任务，使用 task subagent 执行，**始终保持 3 个并发**。",
        "",
        "## 执行方式",
        "",
        "1. 一次启动 3 个 task subagent，分别执行 3 个不同的 Task（prompt 为该任务 ```prompt 和 ``` 之间的完整内容）",
        "2. 任意一个 task 完成后，立即启动下一个未执行的 Task，保持 3 个并发",
        "3. 重复直到所有 Task 都启动并完成",
        "",
        "**不要等 3 个都完成再启动下一批，必须完成一个补一个。**",
        "每个 task subagent 输出 `WRAPPER DONE: {item_id}` 时表示该任务完成（session 已写入磁盘）。",
        "不需要上传、不需要评分、不需要分析结果。",
        "",
        f"共 {len(items)} 个任务。",
        "",
        "---",
        "",
    ]

    for i, item in enumerate(items):
        prompt = build_wrapper_prompt(item)
        lines.append(
            f"### Task {i + 1}: {item['id']} ({item['operation_type']} {item['difficulty']})"
        )
        lines.append("")
        lines.append("```prompt")
        lines.append(prompt)
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


# ── run 子命令（脚本驱动串行执行） ─────────────────────────────────────────────


_CAW_BIN = os.path.expanduser("~/.cobo-agentic-wallet/bin/caw")


async def _revoke_active_pacts(item_id: str) -> None:
    """Revoke 所有 active pact，确保每个 eval item 从干净的 pact 状态开始。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            _CAW_BIN, "pact", "list", "--status", "active",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return
        pact_data = json.loads(stdout.decode())
        pacts = pact_data.get("result", {}).get("pacts", [])
        for p in pacts:
            pid = p.get("id", "")
            if not pid:
                continue
            rp = await asyncio.create_subprocess_exec(
                _CAW_BIN, "pact", "revoke", pid,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(rp.communicate(), timeout=10)
        if pacts:
            print(f"  [{item_id}] revoked {len(pacts)} active pact(s)")
    except Exception:
        pass  # 清理失败不阻塞评测


async def _run_openclaw(
    openclaw_bin: str,
    args: list[str],
    timeout: int | None = None,
) -> tuple[int, str, str]:
    """调用 openclaw CLI，返回 (returncode, stdout, stderr)。超时时 kill 进程。"""
    proc = await asyncio.create_subprocess_exec(
        openclaw_bin,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", "timeout"

    return (
        proc.returncode or 0,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


def _parse_agent_result(stdout: str) -> dict:
    """从 ``openclaw agent --json`` 的 stdout 中解析 JSON 结果。

    openclaw 可能在 JSON 前输出非 JSON 文本（如 streaming），因此先尝试全文解析，
    失败则逐行倒序查找首个合法 JSON 对象。
    """
    stdout = stdout.strip()
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


def _get_stop_reason(result: dict) -> str:
    """从 openclaw agent --json 的结果中提取 stopReason。"""
    try:
        return result["result"]["meta"]["stopReason"]
    except (KeyError, TypeError):
        return ""


async def _run_single_task(
    item: dict,
    openclaw_bin: str,
    workspace: str,
    run_dir: Path,
    timeout: int,
) -> str:
    """执行单个评测 task，返回状态字符串 ("ok" | "error:<reason>")。"""
    item_id = item["id"]
    agent_name = f"eval-{item_id}"
    actual_agent_id = ""

    try:
        # 0. 预清理残留 agent（上次异常退出或 delete 失败时会留下同名 agent，
        #    导致 agents add 报 "already exists"；直接忽略失败）
        await _run_openclaw(
            openclaw_bin,
            ["agents", "delete", agent_name.lower(), "--force"],
            timeout=15,
        )
        # openclaw agents delete 只删注册信息，不删 session 文件目录。
        # 如果不清理，agents add 重建同名 agent 时会继承旧 session（包括上次
        # 卡住的 pending 交易上下文），导致本次评测从错误状态续跑。
        agent_session_dir = _OC_HOME / "agents" / agent_name.lower()
        if agent_session_dir.exists():
            shutil.rmtree(agent_session_dir, ignore_errors=True)

        # 0.5 清理所有 active pact（避免 agent 复用旧 pact 导致评测无法检测 pact submit）
        await _revoke_active_pacts(item_id)

        # 1. 创建隔离 agent
        rc, out, err = await _run_openclaw(
            openclaw_bin,
            ["agents", "add", agent_name, "--workspace", workspace, "--non-interactive", "--json"],
            timeout=30,
        )
        if rc != 0:
            print(f"  [{item_id}] ERROR  agents add 失败: {err.strip() or out.strip()}")
            return "error:agent_create_failed"

        # Openclaw 自动将 agent ID 转小写，从返回的 JSON 中读取实际 ID
        try:
            add_result = json.loads(out.strip())
            actual_agent_id = add_result.get("agentId", agent_name.lower())
        except json.JSONDecodeError:
            actual_agent_id = agent_name.lower()

        # 2. 构建 prompt 并发送
        prompt = build_task_prompt(item)
        rc, out, err = await _run_openclaw(
            openclaw_bin,
            ["agent", "--agent", actual_agent_id, "--message", prompt, "--json", "--timeout", str(timeout)],
            timeout=timeout + 60,  # 给 CLI 本身留出余量
        )

        if rc == -1:
            print(f"  [{item_id}] TIMEOUT  ({timeout}s)")
            status = "error:timeout"
        elif rc != 0:
            print(f"  [{item_id}] ERROR  agent 返回非零: rc={rc}")
            status = "error:agent_failed"
        else:
            result = _parse_agent_result(out)
            stop_reason = _get_stop_reason(result)
            status = "ok"

            # 3. 续传循环：stopReason 不是 stop 时发 "继续"
            continuations = 0
            while stop_reason and stop_reason != "stop" and continuations < _MAX_CONTINUATIONS:
                continuations += 1
                print(f"  [{item_id}] 续传 #{continuations} (stopReason={stop_reason})")
                rc, out, err = await _run_openclaw(
                    openclaw_bin,
                    ["agent", "--agent", actual_agent_id, "--message", "继续执行，不要停下", "--json", "--timeout", str(timeout)],
                    timeout=timeout + 60,
                )
                if rc == -1:
                    print(f"  [{item_id}] TIMEOUT  续传 #{continuations}")
                    status = "error:timeout"
                    break
                if rc != 0:
                    print(f"  [{item_id}] ERROR  续传 #{continuations} rc={rc}")
                    status = "error:agent_failed"
                    break
                result = _parse_agent_result(out)
                stop_reason = _get_stop_reason(result)

            if continuations >= _MAX_CONTINUATIONS and stop_reason != "stop":
                print(f"  [{item_id}] WARN  达到续传上限 ({_MAX_CONTINUATIONS})")
                status = "warn:max_continuations"

        # 4. 收集 session 文件
        session_dir = _OC_HOME / "agents" / actual_agent_id / "sessions"
        jsonl_files = sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if session_dir.exists() else []
        # 过滤掉 sessions.json（不是 session 数据文件）
        jsonl_files = [f for f in jsonl_files if f.name != "sessions.json"]

        if jsonl_files:
            dst = run_dir / f"{item_id}.jsonl"
            shutil.copy2(jsonl_files[0], dst)
            size_kb = dst.stat().st_size / 1024
            print(f"  [{item_id}] {status.upper()}  session={size_kb:.0f}KB -> {dst.name}")
        else:
            print(f"  [{item_id}] {status.upper()}  (no session file)")
            if status == "ok":
                status = "error:no_session"

        return status

    except Exception as e:
        print(f"  [{item_id}] EXCEPTION  {e}")
        return f"error:exception:{e}"

    finally:
        # 5. 清理 agent（无论成功失败都执行）
        if actual_agent_id:
            rc, _, err = await _run_openclaw(
                openclaw_bin,
                ["agents", "delete", actual_agent_id, "--force"],
                timeout=30,
            )
            if rc != 0:
                print(f"  [{item_id}] WARN  agent 清理失败: {err.strip()}")


async def _cmd_run(
    dataset_name: str,
    run_name: str,
    item_ids: list[str] | None,
    timeout: int,
    openclaw_bin: str,
    workspace: str,
    skip_upload: bool,
    skip_pack: bool,
    skill: str,
    model: str,
    model_full: str,
    description: str,
    skip_link: bool = False,
) -> None:
    """脚本驱动串行执行评测：为每个 task 创建隔离 agent，通过 CLI 执行，收集 session。

    Args:
        skip_link: True 时上传 trace 不创建/关联 dataset run（适合调试少量 case）。
    """
    items = get_dataset_items(dataset_name)
    if item_ids:
        items = [i for i in items if i["id"] in item_ids]

    if not items:
        print("[ERROR] 没有匹配的 items")
        sys.exit(1)

    run_dir = _RUNS_DIR / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== 脚本驱动评测 (run: {run_name}) ===")
    print(f"数据集: {dataset_name} ({len(items)} items)")
    print(f"openclaw: {openclaw_bin}")
    print(f"workspace: {workspace}")
    print(f"timeout: {timeout}s / task")
    print()

    results: dict[str, str] = {}

    for i, item in enumerate(items):
        item_id = item["id"]
        op = item["operation_type"]
        diff = item["difficulty"]
        print(f"[{i + 1}/{len(items)}] {item_id} ({op} {diff})")
        status = await _run_single_task(item, openclaw_bin, workspace, run_dir, timeout)
        results[item_id] = status

    # 写 manifest
    manifest = {
        "run_name": run_name,
        "dataset_name": dataset_name,
        "source": "openclaw-cli",
        "executed_at": datetime.now(tz=timezone.utc).isoformat(),
        "items": {
            item["id"]: {
                "status": results.get(item["id"], "skipped"),
                "operation_type": item["operation_type"],
                "difficulty": item["difficulty"],
            }
            for item in items
        },
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    # 汇总
    ok_count = sum(1 for s in results.values() if s == "ok")
    warn_count = sum(1 for s in results.values() if s.startswith("warn:"))
    err_count = sum(1 for s in results.values() if s.startswith("error:"))
    print(f"\n=== 完成: {ok_count} ok / {warn_count} warn / {err_count} error (共 {len(items)}) ===")
    print(f"文件位置: {run_dir}")

    if err_count > 0:
        failed = [iid for iid, s in results.items() if s.startswith("error:")]
        print(f"\n失败项: {', '.join(failed)}")
        print(f"重跑命令: python {sys.argv[0]} run --run-name {run_name} --item-id {' '.join(failed)}")

    # upload + pack
    if not skip_upload:
        print("\n--- 上传到 Langfuse ---")
        cmd_upload(
            run_name, dataset_name, item_ids, skill, model, model_full, description,
            skip_link=skip_link,
        )

    if not skip_pack:
        print("\n--- 打包 ---")
        cmd_pack(run_name)


# ── prepare 子命令 ──────────────────────────────────────────────────────────────


def cmd_prepare(dataset_name: str, output_dir: str | None, item_ids: list[str] | None) -> None:
    """生成 task prompt 文件。"""
    items = get_dataset_items(dataset_name)
    if item_ids:
        items = [i for i in items if i["id"] in item_ids]

    if not items:
        print("[ERROR] 没有匹配的 items")
        sys.exit(1)

    out_dir = Path(output_dir) if output_dir else Path("/tmp/eval-prompts")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== 生成 {len(items)} 个 task prompt ===\n")

    # 生成单个 prompt 文件
    for item in items:
        prompt = build_task_prompt(item)
        prompt_file = out_dir / f"{item['id']}.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        op = item["operation_type"]
        diff = item["difficulty"]
        print(f"  [{item['id']}] [{op:15s}] [{diff}] -> {prompt_file.name}")

    # 生成汇总 prompt
    all_prompt = build_all_tasks_prompt(items)
    all_file = out_dir / "_all_tasks.txt"
    all_file.write_text(all_prompt, encoding="utf-8")

    print(f"\n文件位置: {out_dir}")
    print(f"汇总 prompt: {all_file}")
    print("\n下一步：在 openclaw 对话中粘贴 _all_tasks.txt 的内容，弱模型会逐个执行 task。")


# ── import-sessions 子命令 ────────────────────────────────────────────────────


def convert_history_to_jsonl(data: dict | list) -> str:
    """
    将 sessions_history API 返回值转换为 JSONL 格式（upload_session.py 兼容）。

    sessions_history 可能返回以下结构之一：
      - list[dict]              : 事件列表，每项直接是 otel event
      - {"events": [...], ...}  : 包含 events 字段的包装对象
      - {"session": {...}, "events": [...]} : 包含 session 元数据的包装对象

    输出：每行一个 JSON 事件，符合 upload_session.py 的 OpenClaw otel 格式。
    """
    events: list[dict] = []

    if isinstance(data, list):
        events = data
    elif isinstance(data, dict):
        if "events" in data:
            raw_events = data["events"]
            # 如有 session 元数据，作为第一个 session event 写入
            if "session" in data and isinstance(data["session"], dict):
                session_ev = {**data["session"], "type": "session"}
                events = [session_ev] + list(raw_events)
            else:
                events = list(raw_events)
        else:
            # 整个 dict 本身作为单个事件（兜底）
            events = [data]

    lines = [json.dumps(ev, ensure_ascii=False) for ev in events]
    return "\n".join(lines) + ("\n" if lines else "")


def cmd_import_sessions(
    run_name: str,
    dataset_name: str,
    item_ids: list[str] | None,
    export_dir: str | None,
) -> None:
    """
    从 wrapper subagent 写入的 /tmp/eval-sessions/{item_id}.json 导入到 run 目录。

    每个 JSON 文件是 sessions_history API 的原始返回值，本命令负责：
      1. 读取 JSON
      2. 转换为 JSONL（upload_session.py 兼容格式）
      3. 写入 run_dir/{item_id}.jsonl
    """
    items = get_dataset_items(dataset_name)
    if item_ids:
        items = [i for i in items if i["id"] in item_ids]

    src_dir = Path(export_dir) if export_dir else Path("/tmp/eval-sessions")
    run_dir = _RUNS_DIR / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== 导入 session 文件 (run: {run_name}) ===")
    print(f"来源目录: {src_dir}\n")

    if not src_dir.exists():
        print(f"[ERROR] 来源目录不存在: {src_dir}")
        sys.exit(1)

    imported = 0
    missing = []

    for item in items:
        item_id = item["id"]
        src = src_dir / f"{item_id}.json"

        if not src.exists():
            print(f"  [{item_id}] MISSING  ({src})")
            missing.append(item_id)
            continue

        try:
            raw = json.loads(src.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"  [{item_id}] ERROR  JSON 解析失败: {e}")
            missing.append(item_id)
            continue

        jsonl_content = convert_history_to_jsonl(raw)
        dst = run_dir / f"{item_id}.jsonl"
        dst.write_text(jsonl_content, encoding="utf-8")
        size_kb = dst.stat().st_size / 1024
        print(f"  [{item_id}] OK  ({size_kb:.0f} KB) -> {dst.name}")
        imported += 1

    print(f"\n导入完成: {imported}/{len(items)} 个 session")
    if missing:
        print(f"缺失: {', '.join(missing)}")
    print(f"文件位置: {run_dir}")

    manifest = {
        "run_name": run_name,
        "dataset_name": dataset_name,
        "source": "openclaw-wrapper",
        "imported_at": datetime.now(tz=timezone.utc).isoformat(),
        "items": {
            item["id"]: {
                "status": "imported" if item["id"] not in missing else "missing",
                "operation_type": item["operation_type"],
                "difficulty": item["difficulty"],
            }
            for item in items
        },
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


# ── collect 子命令 ─────────────────────────────────────────────────────────────


def cmd_collect(
    dataset_name: str,
    run_name: str,
    item_ids: list[str] | None,
    session_dir: str | None,
) -> None:
    """收集 openclaw session 文件。"""
    items = get_dataset_items(dataset_name)
    if item_ids:
        items = [i for i in items if i["id"] in item_ids]

    search_dir = Path(session_dir) if session_dir else _OC_SESSION_DIR
    run_dir = _RUNS_DIR / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== 收集 session 文件 (run: {run_name}) ===")
    print(f"搜索目录: {search_dir}\n")

    if not search_dir.exists():
        print(f"[ERROR] 搜索目录不存在: {search_dir}")
        sys.exit(1)

    collected = 0
    missing = []

    for item in items:
        item_id = item["id"]
        marker = f"[EVAL:{item_id}]"
        found = []

        for jsonl_file in search_dir.rglob("*.jsonl"):
            try:
                # 只检查第一行（首条 user 消息），避免把包含所有 item prompt 的
                # 主 session 文件误匹配为多个 item 的 session
                first_line = jsonl_file.open(encoding="utf-8", errors="ignore").readline()
                if marker in first_line:
                    found.append(jsonl_file)
            except OSError:
                continue

        if found:
            # 按修改时间取最新
            found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            src = found[0]
            dst = run_dir / f"{item_id}.jsonl"
            shutil.copy2(src, dst)
            size_kb = dst.stat().st_size / 1024
            print(f"  [{item_id}] OK  ({size_kb:.0f} KB) <- {src.name}")
            collected += 1
        else:
            print(f"  [{item_id}] MISSING")
            missing.append(item_id)

    print(f"\n收集完成: {collected}/{len(items)} 个 session")
    if missing:
        print(f"缺失: {', '.join(missing)}")
    print(f"文件位置: {run_dir}")

    # 写 manifest
    manifest = {
        "run_name": run_name,
        "dataset_name": dataset_name,
        "source": "openclaw",
        "collected_at": datetime.now(tz=timezone.utc).isoformat(),
        "items": {
            item["id"]: {
                "status": "collected" if item["id"] not in missing else "missing",
                "operation_type": item["operation_type"],
                "difficulty": item["difficulty"],
            }
            for item in items
        },
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


# ── upload 子命令 ──────────────────────────────────────────────────────────────


def cmd_upload(
    run_name: str,
    dataset_name: str,
    item_ids: list[str] | None,
    skill: str,
    model: str,
    model_full: str,
    description: str,
    skip_link: bool = False,
) -> None:
    """上传 session 到 Langfuse。

    Args:
        skip_link: True 时只上传 trace，不创建/关联 dataset run（适合调试少量 case）。
    """
    run_dir = _RUNS_DIR / run_name
    if not run_dir.exists():
        print(f"[ERROR] Run 目录不存在: {run_dir}")
        sys.exit(1)

    # 自动构建 run_description（如未手动指定）
    run_description = description
    if not run_description:
        n_sessions = len(list(run_dir.glob("E2E-*.jsonl")))
        display_model = model_full or model
        run_description = (
            f"Openclaw 弱模型评测 | model: {display_model} | dataset: {dataset_name}"
            f" ({n_sessions} cases) | env: openclaw sandbox"
        )

    batch_upload_sessions(
        run_dir, run_name, dataset_name, skill, item_ids, run_description, skip_link=skip_link
    )


# ── pack 子命令 ────────────────────────────────────────────────────────────────


def _fetch_gce_metadata(path: str) -> str | None:
    """访问 GCE metadata server，拿不到时返回 None（非 GCE 环境/超时）。"""
    req = urllib.request.Request(f"{_METADATA_BASE}/{path}", headers=_METADATA_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=_METADATA_TIMEOUT) as resp:
            return resp.read().decode("utf-8").strip()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def _build_scp_command(archive: str) -> str:
    """拼接可直接复制粘贴的 gcloud scp 命令；非 GCE 环境退回占位符模板。"""
    # 以 zone 探测作为"是否在 GCE 上"的判据：metadata 拿到才信任 hostname 是实例名
    zone_full = _fetch_gce_metadata("instance/zone")  # 形如 projects/123/zones/asia-east2-b
    if zone_full is None:
        return (
            f"gcloud compute scp <实例名>:{archive} ~/Downloads/ "
            f"--zone=<zone> --project=<project-id>"
        )
    zone = zone_full.rsplit("/", 1)[-1]
    project = _fetch_gce_metadata("project/project-id") or "<project-id>"
    instance = socket.gethostname() or "<实例名>"
    return f"gcloud compute scp {instance}:{archive} ~/Downloads/ --zone={zone} --project={project}"


def cmd_pack(run_name: str) -> None:
    """打包 session 文件，方便下载到本地。"""
    run_dir = _RUNS_DIR / run_name
    if not run_dir.exists():
        print(f"[ERROR] Run 目录不存在: {run_dir}")
        sys.exit(1)

    archive = f"/tmp/eval-oc-{run_name}.tar.gz"
    subprocess.run(
        ["tar", "czf", archive, "-C", str(run_dir), "."],
        check=True,
    )

    size_mb = Path(archive).stat().st_size / 1024 / 1024
    print(f"打包完成: {archive} ({size_mb:.1f} MB)")
    print("\n下载到本地（在 Mac 终端执行）：")
    print(f"  {_build_scp_command(archive)}")


# ── dispatch 子命令（本地 Mac 端：并行调度多台 openclaw 服务器） ────────────────


def _parse_server_spec(spec: str) -> dict:
    """解析 server 规格 'name:zone:project' 为 dict。"""
    parts = spec.split(":")
    if len(parts) != 3:
        raise ValueError(
            f"invalid server spec '{spec}', expected format 'name:zone:project'"
        )
    return {"name": parts[0], "zone": parts[1], "project": parts[2]}


def _build_remote_run_cmd(
    dataset_name: str,
    run_name: str,
    item_ids: list[str],
    timeout: int,
    skill: str,
    model: str,
    model_full: str,
    *,
    fire_and_forget: bool = False,
    server_name: str = "",
) -> str:
    """构建要在远端 openclaw 服务器上执行的完整 shell 命令（传给 sudo su - ubuntu -c）。

    fire_and_forget=True 时：用 nohup 后台执行，SSH 在 echo 远端 PID 后立即返回。
    日志写到远端 ~/.caw-eval/runs/{run_name}/{server_name}.nohup.log。
    """
    item_args = " ".join(item_ids)
    # 远端 model-full 可能含 /，不会破坏 shell；但保险用 shlex.quote 包起来
    core_cmd = (
        "export PATH=/home/ubuntu/.npm-global/bin:/home/ubuntu/.cobo-agentic-wallet/bin:$PATH; "
        "cd ~/.agents/skills/caw-eval/scripts && "
        "python3 -u run_eval_openclaw.py run "
        f"--run-name {shlex.quote(run_name)} "
        f"--dataset-name {shlex.quote(dataset_name)} "
        f"--item-id {item_args} "
        f"--timeout {timeout} "
        f"--skill {shlex.quote(skill)} "
        f"--model {shlex.quote(model)} "
        f"--model-full {shlex.quote(model_full)} "
        "--skip-pack"
    )
    if not fire_and_forget:
        return core_cmd
    # fire-and-forget：nohup 后台运行，echo PID 后 SSH 立即返回
    # 本地日志文件（.log）只记录 PID 和 nohup log 路径；实际输出在远端 nohup log
    log_path = f"~/.caw-eval/runs/{run_name}/{server_name}.nohup.log"
    return (
        f"mkdir -p ~/.caw-eval/runs/{shlex.quote(run_name)}; "
        f"nohup bash -c {shlex.quote(core_cmd)} > {log_path} 2>&1 & echo $!"
    )


async def _ssh_dispatch_one(
    server: dict,
    item_ids: list[str],
    dataset_name: str,
    run_name: str,
    timeout: int,
    skill: str,
    model: str,
    model_full: str,
    log_dir: Path,
    *,
    fire_and_forget: bool = False,
) -> tuple[str, int]:
    """SSH 到一台 server 串行执行其分配的 items，stdout/stderr 写入 log_dir/{name}.log。

    fire_and_forget=True 时：远端用 nohup 后台启动，SSH 在拿到 PID 后立即返回。
    本地日志只记录 PID + 远端 nohup log 路径，实际输出在远端。
    """
    if not item_ids:
        return server["name"], 0

    remote_cmd = _build_remote_run_cmd(
        dataset_name, run_name, item_ids, timeout, skill, model, model_full,
        fire_and_forget=fire_and_forget,
        server_name=server["name"],
    )
    ssh_cmd = [
        "gcloud",
        "compute",
        "ssh",
        "--zone",
        server["zone"],
        server["name"],
        "--tunnel-through-iap",
        "--project",
        server["project"],
        "--ssh-flag=-o ServerAliveInterval=60",
        "--ssh-flag=-o ServerAliveCountMax=10",
        "--",
        f"sudo su - ubuntu -c {shlex.quote(remote_cmd)}",
    ]

    log_file = log_dir / f"{server['name']}.log"
    print(f"[DISPATCH→ {server['name']}] items={item_ids} log={log_file}")

    if fire_and_forget:
        # FF 模式：SSH 只等远端 echo PID，不等进程结束
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        pid = stdout.decode().strip()
        nohup_log = f"~/.caw-eval/runs/{run_name}/{server['name']}.nohup.log"
        with log_file.open("w", encoding="utf-8") as f:
            f.write(f"# fire-and-forget dispatch\n")
            f.write(f"# command: {' '.join(shlex.quote(c) for c in ssh_cmd)}\n")
            f.write(f"# remote PID: {pid}\n")
            f.write(f"# nohup log (on server): {nohup_log}\n")
            if stderr.strip():
                f.write(f"# stderr: {stderr.decode().strip()}\n")
        print(f"[DISPATCH← {server['name']}] fire-and-forget PID={pid}")
        print(f"  nohup log (on server): {nohup_log}")
        return server["name"], 0

    with log_file.open("w", encoding="utf-8") as f:
        f.write(f"# dispatch command:\n# {' '.join(shlex.quote(c) for c in ssh_cmd)}\n\n")
        f.flush()
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=f,
            stderr=asyncio.subprocess.STDOUT,
        )
        rc = await proc.wait()

    print(f"[DISPATCH← {server['name']}] rc={rc}")
    return server["name"], rc


async def _dynamic_worker(
    server: dict,
    queue: asyncio.Queue,
    item_results: dict,
    dataset_name: str,
    run_name: str,
    timeout: int,
    skill: str,
    model: str,
    model_full: str,
    log_dir: Path,
) -> str:
    """动态 worker：从队列持续取 item 执行，直到队列空为止。

    每次只跑 1 个 item，完成后立即从队列取下一个，实现服务器间负载均衡。
    item_results[item_id] = (server_name, rc) 记录每个 item 的执行结果。
    """
    while True:
        try:
            item_id = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        remote_cmd = _build_remote_run_cmd(
            dataset_name, run_name, [item_id], timeout, skill, model, model_full,
            fire_and_forget=False,
            server_name=server["name"],
        )
        ssh_cmd = [
            "gcloud",
            "compute",
            "ssh",
            "--zone",
            server["zone"],
            server["name"],
            "--tunnel-through-iap",
            "--project",
            server["project"],
            "--ssh-flag=-o ServerAliveInterval=60",
            "--ssh-flag=-o ServerAliveCountMax=10",
            "--",
            f"sudo su - ubuntu -c {shlex.quote(remote_cmd)}",
        ]

        log_file = log_dir / f"{server['name']}-{item_id}.log"
        print(f"[DISPATCH→ {server['name']}] item={item_id} log={log_file.name}")

        with log_file.open("w", encoding="utf-8") as f:
            f.write(f"# dispatch command:\n# {' '.join(shlex.quote(c) for c in ssh_cmd)}\n\n")
            f.flush()
            proc = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=f,
                stderr=asyncio.subprocess.STDOUT,
            )
            rc = await proc.wait()

        status = "OK" if rc == 0 else f"FAIL rc={rc}"
        print(f"[DISPATCH← {server['name']}] item={item_id} {status}")
        item_results[item_id] = (server["name"], rc)
        queue.task_done()

    return server["name"]


async def _cmd_dispatch(
    dataset_name: str,
    run_name: str,
    item_ids: list[str] | None,
    servers: list[dict],
    timeout: int,
    skill: str,
    model: str,
    model_full: str,
    *,
    fire_and_forget: bool = False,
    static: bool = False,
) -> None:
    """并行 dispatch 评测任务到多台 openclaw 服务器。

    默认动态队列模式（非 fire-and-forget）：所有 items 放入队列，每台服务器作为 worker
    持续取任务执行，完成一个立即取下一个，充分利用空闲服务器。

    fire_and_forget=True 或 static=True 时：退化为静态轮询分配（i % N），
    各台服务器预先分配固定 chunk，SSH 启动后（fire-and-forget 时）立即返回。
    """
    items = get_dataset_items(dataset_name)
    if item_ids:
        items = [i for i in items if i["id"] in item_ids]

    if not items:
        print("[ERROR] 没有匹配的 items")
        sys.exit(1)

    if not servers:
        print("[ERROR] 至少需要一台 --server")
        sys.exit(1)

    n = len(servers)
    log_dir = _RUNS_DIR / run_name / "dispatch-logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # ── 静态分配路径（fire-and-forget 或显式 --static）────────────────────────
    if fire_and_forget or static:
        chunks: list[list[str]] = [[] for _ in range(n)]
        for i, item in enumerate(items):
            chunks[i % n].append(item["id"])

        mode = "fire-and-forget" if fire_and_forget else "static"
        print(f"=== Dispatch [{mode}] (run: {run_name}) ===")
        print(f"数据集: {dataset_name} ({len(items)} items)")
        print(f"服务器: {n}, 模型: {model_full or model}")
        for srv, chunk in zip(servers, chunks):
            print(f"  → {srv['name']} [{srv['zone']}]: {chunk}")
        print(f"日志目录: {log_dir}")
        print()

        coroutines = [
            _ssh_dispatch_one(
                srv, chunk, dataset_name, run_name, timeout, skill, model, model_full, log_dir,
                fire_and_forget=fire_and_forget,
            )
            for srv, chunk in zip(servers, chunks)
        ]
        static_results = await asyncio.gather(*coroutines, return_exceptions=True)

        print("\n=== 完成 ===")
        failures: list[str] = []
        for srv, result in zip(servers, static_results):
            if isinstance(result, Exception):
                print(f"  {srv['name']}: EXCEPTION {result}")
                failures.append(srv["name"])
            else:
                _, rc = result  # type: ignore[misc]
                status = "OK" if rc == 0 else f"FAIL rc={rc}"
                print(f"  {srv['name']}: {status}")
                if rc != 0:
                    failures.append(srv["name"])

        if failures:
            print(f"\n失败服务器: {failures}")
            print(f"查看日志: {log_dir}/<server>.log")
            if fire_and_forget:
                print(f"或查看 nohup log：ssh 到各服务器看 ~/.caw-eval/runs/{run_name}/<server>.nohup.log")
        else:
            print(f"\n所有 server 执行完毕。Langfuse run: {run_name}")
            print(f"下一步：参考 SKILL-openclaw.md Step 4 评分（score_traces.py langfuse）")
        return

    # ── 动态队列路径（默认）────────────────────────────────────────────────────
    print(f"=== Dispatch [dynamic] (run: {run_name}) ===")
    print(f"数据集: {dataset_name} ({len(items)} items)")
    print(f"服务器: {n} workers, 模型: {model_full or model}")
    print(f"模式: 动态队列（空闲服务器自动取下一个任务）")
    all_ids = [item["id"] for item in items]
    print(f"任务队列: {all_ids}")
    print(f"日志目录: {log_dir}")
    print()

    queue: asyncio.Queue = asyncio.Queue()
    for item in items:
        await queue.put(item["id"])

    item_results: dict[str, tuple[str, int]] = {}

    workers = [
        _dynamic_worker(
            srv, queue, item_results,
            dataset_name, run_name, timeout, skill, model, model_full, log_dir,
        )
        for srv in servers
    ]
    await asyncio.gather(*workers)

    print("\n=== 完成 ===")
    failed_items: list[str] = []
    for item_id, (srv_name, rc) in sorted(item_results.items()):
        status = "OK" if rc == 0 else f"FAIL rc={rc}"
        print(f"  [{srv_name}] {item_id}: {status}")
        if rc != 0:
            failed_items.append(item_id)

    if failed_items:
        print(f"\n失败 items: {failed_items}")
        print(f"查看日志: {log_dir}/<server>-<item_id>.log")
        print(f"重跑命令示例: --item-id {' '.join(failed_items)}")
        sys.exit(1)
    else:
        print(f"\n所有 {len(item_results)} 个 item 执行完毕。Langfuse run: {run_name}")
        print(f"下一步：参考 SKILL-openclaw.md Step 4 评分（score_traces.py langfuse）")


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M")

    parser = argparse.ArgumentParser(
        description="Openclaw 弱模型评测脚本（三层分离方案的服务器端）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd")

    # ── run（推荐）
    p_run = sub.add_parser("run", help="脚本驱动串行执行评测（推荐）")
    p_run.add_argument("--dataset-name", default="caw-agent-eval-seth-v2")
    p_run.add_argument("--run-name", required=True)
    p_run.add_argument("--item-id", nargs="*", help="只执行指定 item")
    p_run.add_argument("--timeout", type=int, default=_DEFAULT_TIMEOUT, help="单个 task 超时秒数")
    p_run.add_argument("--openclaw-bin", default="openclaw", help="openclaw 二进制路径")
    p_run.add_argument(
        "--workspace",
        default=str(_OC_HOME / "workspace"),
        help="Openclaw workspace 路径（默认 ~/.openclaw/workspace）",
    )
    p_run.add_argument("--skip-upload", action="store_true", help="跳过上传 Langfuse")
    p_run.add_argument("--skip-pack", action="store_true", help="跳过打包")
    p_run.add_argument(
        "--no-link",
        action="store_true",
        help="只上传 trace，不创建/关联 dataset run（指定少量 case 调试时用）",
    )
    p_run.add_argument("--skill", default="cobo-agentic-wallet-sandbox")
    p_run.add_argument("--model", default="doubao", help="模型短标识")
    p_run.add_argument("--model-full", default="", help="完整模型 ID")
    p_run.add_argument("--description", default="", help="自定义 run description")

    # ── prepare
    p_prepare = sub.add_parser("prepare", help="生成 task prompt 文件")
    p_prepare.add_argument("--dataset-name", default="caw-agent-eval-seth-v2")
    p_prepare.add_argument("--output-dir", help="输出目录（默认 /tmp/eval-prompts）")
    p_prepare.add_argument("--item-id", nargs="*", help="只生成指定 item")

    # ── import-sessions
    p_import = sub.add_parser(
        "import-sessions", help="从 wrapper subagent 导出的 JSON 导入 session"
    )
    p_import.add_argument("--dataset-name", default="caw-agent-eval-seth-v2")
    p_import.add_argument("--run-name", required=True)
    p_import.add_argument("--item-id", nargs="*", help="只导入指定 item")
    p_import.add_argument("--export-dir", help="wrapper 写入目录（默认 /tmp/eval-sessions）")

    # ── collect
    p_collect = sub.add_parser("collect", help="收集 openclaw session 文件")
    p_collect.add_argument("--dataset-name", default="caw-agent-eval-seth-v2")
    p_collect.add_argument(
        "--model", default="ark-code", help="模型短标识，用于构建 run name（如 ark-code）"
    )
    p_collect.add_argument("--run-name", default="", help="run 名称（默认 eval-oc-<model>-<ts>）")
    p_collect.add_argument("--item-id", nargs="*", help="只收集指定 item")
    p_collect.add_argument("--session-dir", help="自定义 session 搜索目录")

    # ── upload
    p_upload = sub.add_parser("upload", help="上传 session 到 Langfuse")
    p_upload.add_argument("--run-name", required=True)
    p_upload.add_argument("--dataset-name", default="caw-agent-eval-seth-v2")
    p_upload.add_argument("--item-id", nargs="*", help="只上传指定 item")
    p_upload.add_argument("--skill", default="cobo-agentic-wallet-sandbox")
    p_upload.add_argument("--model", default="ark-code", help="模型短标识（用于 run description）")
    p_upload.add_argument(
        "--model-full", default="ark-code-latest", help="完整模型 ID，写入 run description"
    )
    p_upload.add_argument(
        "--description", default="", help="自定义 run description（覆盖自动生成）"
    )
    p_upload.add_argument(
        "--no-link",
        action="store_true",
        help="只上传 trace，不创建/关联 dataset run",
    )

    # ── pack
    p_pack = sub.add_parser("pack", help="打包 session 文件供下载")
    p_pack.add_argument("--run-name", required=True)

    # ── dispatch（本地 Mac 端：并行调度 N 台 openclaw 服务器）
    p_dispatch = sub.add_parser(
        "dispatch",
        help="本地 Mac 端：并行 SSH 到多台 openclaw 服务器，每台串行执行其分配的 items",
    )
    p_dispatch.add_argument("--dataset-name", default="caw-agent-eval-seth-v2")
    p_dispatch.add_argument("--run-name", required=True)
    p_dispatch.add_argument("--item-id", nargs="*", help="只分发指定 item（否则使用整个 dataset）")
    p_dispatch.add_argument(
        "--server",
        action="append",
        required=True,
        metavar="name:zone:project",
        help="gcloud 服务器规格，可重复；items 轮询分配（i %% N）到各台",
    )
    p_dispatch.add_argument(
        "--timeout", type=int, default=_DEFAULT_TIMEOUT, help="远端单 task 超时（秒）"
    )
    p_dispatch.add_argument("--skill", default="cobo-agentic-wallet-sandbox")
    p_dispatch.add_argument("--model", required=True, help="模型短标识，如 doubao")
    p_dispatch.add_argument(
        "--model-full", default="", help="完整模型 ID，如 volcengine/doubao-seed-2.0-code"
    )
    p_dispatch.add_argument(
        "--static",
        action="store_true",
        help=(
            "静态轮询分配模式（i %% N）：items 预先固定分给每台服务器，不做动态调度。"
            "默认为动态队列模式（空闲服务器自动取下一个任务）。"
            "fire-and-forget 时自动启用静态模式。"
        ),
    )
    p_dispatch.add_argument(
        "--fire-and-forget",
        action="store_true",
        help=(
            "后台模式：SSH 启动远端 nohup 进程后立即返回，不等待评测完成。"
            "进度通过 score_traces.py langfuse --watch 轮询 Langfuse 跟踪。"
            "隐含 --static（后台模式无法动态调度）。"
        ),
    )

    args = parser.parse_args()

    if args.cmd == "run":
        asyncio.run(
            _cmd_run(
                dataset_name=args.dataset_name,
                run_name=args.run_name,
                item_ids=args.item_id,
                timeout=args.timeout,
                openclaw_bin=args.openclaw_bin,
                workspace=args.workspace,
                skip_upload=args.skip_upload,
                skip_pack=args.skip_pack,
                skill=args.skill,
                model=args.model,
                model_full=args.model_full,
                description=args.description,
                skip_link=args.no_link,
            )
        )
    elif args.cmd == "prepare":
        cmd_prepare(
            dataset_name=args.dataset_name,
            output_dir=args.output_dir,
            item_ids=args.item_id,
        )
    elif args.cmd == "import-sessions":
        cmd_import_sessions(
            run_name=args.run_name,
            dataset_name=args.dataset_name,
            item_ids=args.item_id,
            export_dir=args.export_dir,
        )
    elif args.cmd == "collect":
        run_name = args.run_name or f"eval-oc-{args.model}-{ts}"
        cmd_collect(
            dataset_name=args.dataset_name,
            run_name=run_name,
            item_ids=args.item_id,
            session_dir=args.session_dir,
        )
    elif args.cmd == "upload":
        cmd_upload(
            run_name=args.run_name,
            dataset_name=args.dataset_name,
            item_ids=args.item_id,
            skill=args.skill,
            model=args.model,
            model_full=args.model_full,
            description=args.description,
            skip_link=args.no_link,
        )
    elif args.cmd == "pack":
        cmd_pack(run_name=args.run_name)
    elif args.cmd == "dispatch":
        servers = [_parse_server_spec(s) for s in args.server]
        asyncio.run(
            _cmd_dispatch(
                dataset_name=args.dataset_name,
                run_name=args.run_name,
                item_ids=args.item_id,
                servers=servers,
                timeout=args.timeout,
                skill=args.skill,
                model=args.model,
                model_full=args.model_full,
                fire_and_forget=args.fire_and_forget,
                static=args.static,
            )
        )
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
