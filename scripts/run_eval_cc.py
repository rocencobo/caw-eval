#!/usr/bin/env python3
"""
Claude Code 评测编排脚本 — 替代 openclaw 的评测流程。

本脚本适用于在 Claude Code (VS Code 扩展) 环境中运行评测。
由于 Claude Code 没有独立 CLI，采用半自动化方式：
  1. prepare  — 生成每个 item 的评测 prompt，可直接粘贴到 Claude Code 中执行
  2. collect  — 执行完成后，扫描 Claude Code session 文件，匹配 eval 标记
  3. upload   — 批量上传收集的 session 到 Langfuse 并关联 dataset run
  4. score    — 对 session 评分（heuristic + 可选 LLM judge）

用法:
    # 1. 生成评测 prompt
    python run_eval_cc.py prepare --dataset-name caw-agent-eval-seth-v2

    # 2. 在 Claude Code 中执行后，收集 session 文件
    python run_eval_cc.py collect --run-name eval-cc-20260411

    # 3. 上传到 Langfuse
    python run_eval_cc.py upload --run-name eval-cc-20260411

    # 4. 评分
    python run_eval_cc.py score --run-name eval-cc-20260411 --report
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from eval_utils import batch_upload_sessions, get_dataset_items

_SCRIPTS_DIR = Path(__file__).parent

# Claude Code session 文件的搜索路径（含 subagent 目录）
_CC_SESSION_DIRS = [
    Path.home() / ".claude" / "projects",
]

# 评测 run 的本地存储目录
_RUNS_DIR = Path.home() / ".caw-eval" / "runs"


def build_eval_prompt(item: dict) -> str:
    """构建单个 item 的评测 prompt。"""
    item_id = item["id"]
    user_message = item["user_message"]

    return f"""[EVAL:{item_id}]
cobo-agentic-wallet-dev skill 已激活。

**评测模式约束 — 仅针对 skill 内部的交互流程，不影响对用户指令本身的响应：**

需要自动跳过的 skill 内部交互（来自 cobo-agentic-wallet-dev skill）：
- 提交 pact 时：跳过展示预览和等待用户确认的步骤，直接调用 `caw pact submit` 提交
- 交易返回 `status=pending_approval` 且 `owner_linked=false` 时：立即自动执行 `caw pending approve <pending_operation_id>`
- 如遇到无法自动化处理的真实阻塞（如 `owner_linked=true` 需要 Human App 审批），记录原因并跳过，继续执行其余步骤

这是自动化评测，无人工实时监控，须完整执行至结束，不得因 skill 内部的交互流程而中断。

按照以下用户指令完成操作：

{user_message}"""


# ── prepare 子命令 ──────────────────────────────────────────────────────────────


def cmd_prepare(dataset_name: str, item_ids: list[str] | None, output_dir: str | None) -> None:
    """生成评测 prompt，输出到终端或文件。"""
    items = get_dataset_items(dataset_name)
    if item_ids:
        items = [i for i in items if i["id"] in item_ids]

    if not items:
        print("[ERROR] 没有匹配的 items")
        sys.exit(1)

    out_dir = Path(output_dir) if output_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== 生成 {len(items)} 个评测 prompt ===\n")

    for item in items:
        prompt = build_eval_prompt(item)

        if out_dir:
            # 写到文件
            prompt_file = out_dir / f"{item['id']}.txt"
            prompt_file.write_text(prompt, encoding="utf-8")
            print(
                f"  [{item['id']}] [{item['operation_type']}] [{item['difficulty']}] -> {prompt_file}"
            )
        else:
            # 输出到终端
            print(f"{'=' * 60}")
            print(f"Item: {item['id']} | {item['operation_type']} | {item['difficulty']}")
            print(f"{'=' * 60}")
            print(prompt)
            print()

    if out_dir:
        print(f"\nPrompt 文件已写入: {out_dir}")
        print("在 Claude Code 中激活 cobo-agentic-wallet-dev skill 后，逐个粘贴执行。")


# ── collect 子命令 ─────────────────────────────────────────────────────────────


def _search_cc_sessions(item_id: str) -> list[Path]:
    """在 Claude Code session 目录中搜索包含指定 item_id eval 标记的文件。

    只检查第一行（首条用户消息）是否包含 marker，避免匹配到 judge session
    （judge session 在后续内容中读取了 eval session 数据，也包含 marker 文本，
    但首行是 judge prompt 而非 eval prompt）。

    优先返回 subagent session（agent-*.jsonl），因为主 session 包含所有
    subagent 的 prompt 文本，会误匹配多个 item。
    """
    marker = f"[EVAL:{item_id}]"
    subagent_files = []
    main_files = []

    for base_dir in _CC_SESSION_DIRS:
        if not base_dir.exists():
            continue
        for jsonl_file in base_dir.rglob("*.jsonl"):
            try:
                first_line = jsonl_file.open(encoding="utf-8", errors="ignore").readline()
                if marker in first_line:
                    if jsonl_file.name.startswith("agent-"):
                        subagent_files.append(jsonl_file)
                    else:
                        main_files.append(jsonl_file)
            except OSError:
                continue

    # 优先 subagent 文件，按修改时间最新排序
    subagent_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    main_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return subagent_files + main_files


def cmd_collect(
    dataset_name: str,
    run_name: str,
    item_ids: list[str] | None,
) -> None:
    """收集 Claude Code session 文件，按 item_id 关联。"""
    items = get_dataset_items(dataset_name)
    if item_ids:
        items = [i for i in items if i["id"] in item_ids]

    run_dir = _RUNS_DIR / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== 收集 session 文件 (run: {run_name}) ===\n")

    collected = 0
    missing = []

    for item in items:
        item_id = item["id"]
        matches = _search_cc_sessions(item_id)

        if matches:
            # 取最新的匹配
            src = matches[0]
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
    print(f"Manifest: {manifest_path}")


# ── upload 子命令 ──────────────────────────────────────────────────────────────


def cmd_upload(
    run_name: str,
    dataset_name: str,
    item_ids: list[str] | None,
    skill: str,
    model: str,
    model_full: str,
    description: str,
) -> None:
    """批量上传 run 目录下的 session 文件到 Langfuse。"""
    run_dir = _RUNS_DIR / run_name

    if not run_dir.exists():
        print(f"[ERROR] Run 目录不存在: {run_dir}")
        print(f"请先运行: python run_eval_cc.py collect --run-name {run_name}")
        sys.exit(1)

    # 自动构建 run_description（如未手动指定）
    run_description = description
    if not run_description:
        n_sessions = len(list(run_dir.glob("E2E-*.jsonl")))
        display_model = model_full or model
        run_description = (
            f"Claude Code 评测 | model: {display_model} | dataset: {dataset_name}"
            f" ({n_sessions} cases) | env: Claude Code"
        )

    batch_upload_sessions(run_dir, run_name, dataset_name, skill, item_ids, run_description)


# ── score 子命令 ───────────────────────────────────────────────────────────────


def cmd_score(
    run_name: str,
    dataset_name: str,
    report: bool,
    dump_judge: str | None,
    judge_results: str | None,
) -> None:
    """对 run 目录下的 session 评分。"""
    run_dir = _RUNS_DIR / run_name

    if not run_dir.exists():
        print(f"[ERROR] Run 目录不存在: {run_dir}")
        sys.exit(1)

    # 构建 score_traces.py 调用参数
    cmd = [
        sys.executable,
        str(_SCRIPTS_DIR / "score_traces.py"),
        "session",
        "--session",
        str(run_dir),
    ]

    if report:
        cmd.append("--report")
    if dump_judge:
        cmd.extend(["--dump-judge-requests", dump_judge])
    if judge_results:
        cmd.extend(["--judge-results", judge_results])

    print(f"=== 评分 (run: {run_name}) ===\n")
    result = subprocess.run(cmd, timeout=600)
    sys.exit(result.returncode)


# ── import-sessions 子命令 ────────────────────────────────────────────────────


def cmd_import_sessions(
    from_dir: str,
    run_name: str,
) -> None:
    """从外部目录导入 session 文件到本地 run 目录。用于导入 Openclaw 服务器拉下来的 session。"""
    src_dir = Path(from_dir)
    if not src_dir.exists():
        print(f"[ERROR] 源目录不存在: {src_dir}")
        sys.exit(1)

    run_dir = _RUNS_DIR / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    session_files = list(src_dir.glob("E2E-*.jsonl"))
    if not session_files:
        # 也尝试不带 E2E 前缀的 jsonl 文件
        session_files = list(src_dir.glob("*.jsonl"))

    if not session_files:
        print(f"[ERROR] 源目录中没有 session 文件: {src_dir}")
        sys.exit(1)

    print(f"=== 导入 {len(session_files)} 个 session 到 {run_name} ===\n")

    imported = 0
    for sf in sorted(session_files):
        dst = run_dir / sf.name
        shutil.copy2(sf, dst)
        size_kb = dst.stat().st_size / 1024
        print(f"  [{sf.stem}] OK  ({size_kb:.0f} KB)")
        imported += 1

    # 复制 manifest（如果有）
    manifest_src = src_dir / "manifest.json"
    if manifest_src.exists():
        shutil.copy2(manifest_src, run_dir / "manifest.json")

    print(f"\n导入完成: {imported} 个 session")
    print(f"文件位置: {run_dir}")
    print("\n下一步：")
    print(f"  python run_eval_cc.py score --run-name {run_name} --report")


# ── metrics 子命令 ────────────────────────────────────────────────────────────


def _extract_session_metrics(jsonl_path: Path) -> dict:
    """从单个 session JSONL 文件提取运行指标。

    返回字段：
      duration_secs, tokens, tool_calls, caw_cmds, pact_submits, tx_cmds, errors
    """
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    timestamps: list[str] = []
    total_tokens = 0
    tool_call_count = 0

    # caw 命令记录：{id, command, is_pact_submit, is_tx}
    caw_records: list[dict] = []

    # tool_result 索引：tool_use_id -> result_text
    result_index: dict[str, str] = {}

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue

        ts = ev.get("timestamp", "")
        if ts:
            timestamps.append(ts)

        ev_type = ev.get("type", "")
        msg = ev.get("message", {})
        if not isinstance(msg, dict):
            continue

        if ev_type == "user":
            # 收集 tool_result
            for block in msg.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result" and block.get("tool_use_id"):
                    raw_content = block.get("content", "")
                    if isinstance(raw_content, list):
                        text = "\n".join(
                            b.get("text", "") for b in raw_content if isinstance(b, dict)
                        )
                    else:
                        text = str(raw_content)
                    result_index[block["tool_use_id"]] = text

        elif ev_type == "assistant":
            # 累计 tokens：output_tokens（模型生成量，不受 cache 影响，最能反映实际工作量）
            usage = msg.get("usage", {})
            total_tokens += usage.get("output_tokens", 0)

            for block in msg.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                tool_call_count += 1
                if block.get("name") != "Bash":
                    continue
                inp = block.get("input", {})
                cmd = inp.get("command", "") if isinstance(inp, dict) else ""
                # 只统计实际 caw 命令（排除 PATH export 等前缀）
                if not re.search(r"\bcaw\s+\w", cmd):
                    continue
                caw_records.append(
                    {
                        "id": block.get("id", ""),
                        "command": cmd,
                        "is_pact_submit": bool(re.search(r"\bcaw\s+pact\s+submit\b", cmd)),
                        "is_tx": bool(re.search(r"\bcaw\s+tx\b", cmd)),
                    }
                )

    # 时长
    duration_secs = 0
    if len(timestamps) >= 2:
        t0 = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
        duration_secs = int((t1 - t0).total_seconds())

    # 错误数：caw 命令返回 error_code 或 "error": true
    error_count = 0
    for rec in caw_records:
        result = result_index.get(rec["id"], "")
        is_error = False
        try:
            data = json.loads(result)
            inner = data.get("result", data)
            is_error = bool(inner.get("error_code")) or bool(data.get("error"))
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            pass
        if not is_error:
            lower = result.lower()
            is_error = '"error": true' in lower or '"error_code"' in lower
        if is_error:
            error_count += 1

    mins, secs = divmod(duration_secs, 60)
    return {
        "duration_secs": duration_secs,
        "duration_str": f"{mins}:{secs:02d}",
        "tokens": total_tokens,
        "tool_calls": tool_call_count,
        "caw_cmds": len(caw_records),
        "pact_submits": sum(1 for r in caw_records if r["is_pact_submit"]),
        "tx_cmds": sum(1 for r in caw_records if r["is_tx"]),
        "errors": error_count,
    }


def cmd_metrics(run_name: str) -> None:
    """从 run 目录的 session 文件提取运行指标，写入 session_metrics.json。"""
    run_dir = _RUNS_DIR / run_name
    if not run_dir.exists():
        print(f"[ERROR] run 目录不存在: {run_dir}")
        sys.exit(1)

    session_files = sorted(run_dir.glob("E2E-*.jsonl"))
    if not session_files:
        print(f"[ERROR] 没有找到 session 文件: {run_dir}")
        sys.exit(1)

    print(f"=== 提取运行指标 ({len(session_files)} 个 session) ===\n")

    items: list[dict] = []
    for sf in session_files:
        m = _extract_session_metrics(sf)
        m["item_id"] = sf.stem
        items.append(m)
        print(
            f"  [{sf.stem}]  {m['duration_str']:>6s}  "
            f"tokens={m['tokens']:>7,}  tool={m['tool_calls']:>3}  "
            f"caw={m['caw_cmds']:>3}  pact_sub={m['pact_submits']}  "
            f"tx={m['tx_cmds']}  err={m['errors']}"
        )

    # 合计 / 平均
    def _sum(key: str) -> int:
        return sum(it[key] for it in items)

    n = len(items)
    totals = {
        "duration_secs": _sum("duration_secs"),
        "tokens": _sum("tokens"),
        "tool_calls": _sum("tool_calls"),
        "caw_cmds": _sum("caw_cmds"),
        "pact_submits": _sum("pact_submits"),
        "tx_cmds": _sum("tx_cmds"),
        "errors": _sum("errors"),
    }
    tm, ts_ = divmod(totals["duration_secs"], 60)
    totals["duration_str"] = f"{tm}:{ts_:02d}"

    averages = {k: round(v / n, 1) for k, v in totals.items() if k not in ("duration_str",)}
    am, as_ = divmod(int(averages["duration_secs"]), 60)
    averages["duration_str"] = f"{am}:{as_:02d}"

    def _fmt(d: dict) -> str:
        return (
            f"{d['duration_str']}  tokens={d['tokens']:,}  tool={d['tool_calls']}"
            f"  caw={d['caw_cmds']}  pact_sub={d['pact_submits']}"
            f"  tx={d['tx_cmds']}  err={d['errors']}"
        )

    print(f"\n  合计: {_fmt(totals)}")
    print(f"  平均: {_fmt(averages)}")

    output = {
        "run_name": run_name,
        "extracted_at": datetime.now(tz=timezone.utc).isoformat(),
        "items": items,
        "totals": totals,
        "averages": averages,
    }
    out_path = run_dir / "session_metrics.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print(f"\n已写入: {out_path}")


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M")

    parser = argparse.ArgumentParser(
        description="Claude Code 评测编排脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd")

    # ── prepare ───────────────────────────────────────────────────────────────
    p_prepare = sub.add_parser("prepare", help="生成评测 prompt")
    p_prepare.add_argument("--dataset-name", default="caw-agent-eval-seth-v2")
    p_prepare.add_argument("--item-id", nargs="*", help="只生成指定 item")
    p_prepare.add_argument("--output-dir", help="输出到目录（每个 item 一个 txt 文件）")

    # ── collect ───────────────────────────────────────────────────────────────
    p_collect = sub.add_parser("collect", help="收集 Claude Code session 文件")
    p_collect.add_argument("--dataset-name", default="caw-agent-eval-seth-v2")
    p_collect.add_argument("--run-name", default=f"eval-cc-{ts}")
    p_collect.add_argument("--item-id", nargs="*", help="只收集指定 item")

    # ── upload ────────────────────────────────────────────────────────────────
    p_upload = sub.add_parser("upload", help="批量上传 session 到 Langfuse")
    p_upload.add_argument("--run-name", required=True)
    p_upload.add_argument("--dataset-name", default="caw-agent-eval-seth-v2")
    p_upload.add_argument("--item-id", nargs="*", help="只上传指定 item")
    p_upload.add_argument("--skill", default="cobo-agentic-wallet-dev")
    p_upload.add_argument(
        "--model", default="sonnet", help="模型短标识，用于 run description（如 sonnet）"
    )
    p_upload.add_argument(
        "--model-full", default="claude-sonnet-4-6", help="完整模型 ID，写入 run description"
    )
    p_upload.add_argument(
        "--description", default="", help="自定义 run description（覆盖自动生成）"
    )

    # ── score ─────────────────────────────────────────────────────────────────
    p_score = sub.add_parser("score", help="对 session 评分")
    p_score.add_argument("--run-name", required=True)
    p_score.add_argument("--dataset-name", default="caw-agent-eval-seth-v2")
    p_score.add_argument("--report", action="store_true", help="打印评分报告")
    p_score.add_argument("--dump-judge-requests", help="导出 LLM judge 请求到文件")
    p_score.add_argument("--judge-results", help="读取 LLM judge 结果文件")

    # ── import-sessions ──────────────────────────────────────────────────────
    p_import = sub.add_parser("import-sessions", help="从外部目录导入 session 文件")
    p_import.add_argument(
        "--from", dest="from_dir", required=True, help="源目录（如 /tmp/oc-sessions/）"
    )
    p_import.add_argument("--run-name", required=True, help="导入到的 run 名称")

    # ── metrics ───────────────────────────────────────────────────────────────
    p_metrics = sub.add_parser(
        "metrics", help="从 session 文件提取运行指标（时长/tokens/caw命令等）"
    )
    p_metrics.add_argument("--run-name", required=True)

    args = parser.parse_args()

    if args.cmd == "prepare":
        cmd_prepare(
            dataset_name=args.dataset_name,
            item_ids=args.item_id,
            output_dir=args.output_dir,
        )
    elif args.cmd == "collect":
        cmd_collect(
            dataset_name=args.dataset_name,
            run_name=args.run_name,
            item_ids=args.item_id,
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
        )
    elif args.cmd == "score":
        cmd_score(
            run_name=args.run_name,
            dataset_name=args.dataset_name,
            report=args.report,
            dump_judge=args.dump_judge_requests,
            judge_results=args.judge_results,
        )
    elif args.cmd == "import-sessions":
        cmd_import_sessions(
            from_dir=args.from_dir,
            run_name=args.run_name,
        )
    elif args.cmd == "metrics":
        cmd_metrics(run_name=args.run_name)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
