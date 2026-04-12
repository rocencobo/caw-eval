"""
评测脚本公共工具模块。

提供 Langfuse 客户端初始化、数据集读取、session 上传和 dataset run 关联等功能。
供 run_eval_cc.py / run_eval_openclaw.py 等评测编排脚本共用。

环境变量:
    LANGFUSE_HOST         - Langfuse 服务地址
    LANGFUSE_PUBLIC_KEY   - Langfuse 公钥（数据集读写 + session 上传）
    LANGFUSE_SECRET_KEY   - Langfuse 私钥（数据集读写 + session 上传）
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from langfuse import Langfuse

# 自动加载同目录下的 .env（不覆盖已设置的环境变量）
load_dotenv(Path(__file__).parent / ".env", override=False)

# upload_session.py 的位置（与本脚本同目录）
_UPLOAD_SESSION_SCRIPT = Path(__file__).parent / "upload_session.py"

_DEFAULT_HOST = "https://langfuse.1cobo.com"


def get_langfuse_client() -> Langfuse:
    """创建并返回 Langfuse 客户端实例。

    凭据优先级: LANGFUSE_DATASET_* → LANGFUSE_* → .env file.
    """
    def _pick(specific: str, generic: str) -> str:
        return os.environ.get(specific) or os.environ.get(generic) or ""

    pub = _pick("LANGFUSE_DATASET_PUBLIC_KEY", "LANGFUSE_PUBLIC_KEY")
    sec = _pick("LANGFUSE_DATASET_SECRET_KEY", "LANGFUSE_SECRET_KEY")
    if not pub or not sec:
        print("[WARN] Langfuse credentials not set. "
              "Set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY "
              "(or LANGFUSE_DATASET_PUBLIC_KEY + LANGFUSE_DATASET_SECRET_KEY) in .env or env vars.")
    host = _pick("LANGFUSE_DATASET_HOST", "LANGFUSE_HOST") or _DEFAULT_HOST

    return Langfuse(
        public_key=pub,
        secret_key=sec,
        host=host,
    )


def get_dataset_items(dataset_name: str) -> list[dict]:
    """从 Langfuse 拉取 dataset items。

    处理 input 为 str 或 dict 两种情况，返回标准化的 item 列表。
    """
    lf = get_langfuse_client()
    dataset = lf.get_dataset(dataset_name)
    items = sorted(dataset.items, key=lambda i: i.id)
    result = []
    for item in items:
        # input 可能是 str 或 dict
        inp = item.input if isinstance(item.input, dict) else {"user_message": item.input or ""}
        meta = item.metadata if isinstance(item.metadata, dict) else {}
        exp = item.expected_output if isinstance(item.expected_output, dict) else {}
        # 优先用 metadata.id（如 E2E-01L1），回退到 Langfuse UUID
        item_id = meta.get("id", item.id)
        result.append(
            {
                "id": item_id,
                "langfuse_id": item.id,
                "user_message": inp.get("user_message", str(item.input or "")),
                "operation_type": meta.get("operation_type", ""),
                "difficulty": meta.get("difficulty", ""),
                "chain": meta.get("chain", ""),
                "success_criteria": exp.get("success_criteria", ""),
            }
        )
    return result


def preflight_check() -> bool:
    """检查 session 上传所需的运行前提条件。"""
    if not _UPLOAD_SESSION_SCRIPT.exists():
        print(f"[PREFLIGHT ERROR] upload_session.py not found at: {_UPLOAD_SESSION_SCRIPT}")
        return False
    print("[PREFLIGHT OK] upload_session.py found")
    print("[INFO] Langfuse credentials read from LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY "
          "(or LANGFUSE_DATASET_* variants) in .env or env vars.")
    return True


def _extract_session_id(session_path: str) -> str:
    """从 JSONL 文件提取 session_id（第一个 type=session 事件）。"""
    try:
        with open(session_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = json.loads(line)
                if ev.get("type") == "session":
                    return ev.get("id", "")
    except Exception:
        pass
    return Path(session_path).stem


def upload_session(
    session_path: str,
    skill_name: str = "cobo-agentic-wallet-sandbox",
) -> str | None:
    """
    通过 upload_session.py CLI 直接上传 session.jsonl 到 Langfuse。
    返回 session_id（作为 Langfuse trace_id），失败返回 None。

    Langfuse 凭据由 upload_session.py 从环境变量或 .env 文件读取。
    """
    session_id = _extract_session_id(session_path)
    if not session_id:
        print("    [UPLOAD ERROR] Cannot extract session_id from session file")
        return None

    if not _UPLOAD_SESSION_SCRIPT.exists():
        print(f"    [UPLOAD ERROR] upload_session.py not found at {_UPLOAD_SESSION_SCRIPT}")
        return None

    env = {**os.environ}

    try:
        result = subprocess.run(
            [sys.executable, str(_UPLOAD_SESSION_SCRIPT), session_path,
             "--skill", skill_name],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.stdout:
            print(f"    [UPLOAD] {result.stdout.strip()[:200]}")
        if result.returncode != 0:
            print(f"    [UPLOAD ERROR] exit={result.returncode} {result.stderr[:200]}")
            return None
        return session_id
    except subprocess.TimeoutExpired:
        print("    [UPLOAD ERROR] upload_session.py timed out")
        return None
    except Exception as e:
        print(f"    [UPLOAD ERROR] {e}")
        return None


def link_to_dataset_run(
    lf: Langfuse,
    dataset_name: str,
    item_id: str,
    run_name: str,
    trace_id: str,
) -> None:
    """将 Langfuse trace 关联到 dataset item run（Langfuse v4 API）。"""
    try:
        lf.api.dataset_run_items.create(
            run_name=run_name,
            dataset_item_id=item_id,
            trace_id=trace_id,
        )
        print(f"    [LINKED] trace={trace_id[:8]}... -> run={run_name} item={item_id}")
    except Exception as e:
        print(f"    [LINK ERROR] {e}")
