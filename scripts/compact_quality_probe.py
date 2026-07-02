#!/usr/bin/env python3
"""Run Parrot compact-rescue prompts against real historical Parrot conversations.

This is a side-channel quality probe: it does not restart or mutate the running
Parrot service. It loads historical request bodies from monthly log DBs,
constructs Claude Code-style compact requests, runs Parrot's compact_rescue
segment/reduce builders locally, and sends those internal Anthropic messages to
the currently running Parrot /v1/messages endpoint with model=gpt-5.5.

Outputs are written under --out-dir for manual review.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Allow running from scripts/ without installing the package.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import compact_rescue  # noqa: E402

DEFAULT_CASES = [
    # Five distinct historical periods; request IDs come from Parrot monthly logs.
    ("2026-04.db", "ac9c17a2-78fb-42c5-8844-5f4267897063"),
    ("2026-05.db", "862ca70a-c2e6-4b6c-8da1-6a8da23dc6a7"),
    ("2026-06.db", "ea4463f6-7ef5-47f7-aedc-b68c714acd3c"),
    ("2026-06.db", "bcb782f3-3826-49a3-be9c-48e69a0e2b24"),
    ("2026-07.db", "2b1fe6de-0497-4706-8395-7f349e6052af"),
]


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def extract_downstream_key(config_path: Path, key_name: str) -> str:
    cfg = load_json(config_path)
    entry = (cfg.get("apiKeys") or {}).get(key_name)
    if not isinstance(entry, dict) or not entry.get("key"):
        raise SystemExit(f"api key name not found or invalid: {key_name}")
    return str(entry["key"])


def load_compact_prompt(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.find("CRITICAL: Respond with TEXT ONLY")
    if start < 0:
        raise SystemExit(f"cannot find Claude compact prompt in {path}")
    end = text.find("\n================================================================================\n2.", start)
    if end < 0:
        end = len(text)
    return text[start:end].strip()


def text_from_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                typ = item.get("type")
                if typ == "text":
                    parts.append(str(item.get("text") or ""))
                elif typ == "tool_result":
                    parts.append(text_from_content(item.get("content")))
                elif typ == "tool_use":
                    parts.append(f"tool_use:{item.get('name')}")
                    parts.append(json.dumps(item.get("input") or {}, ensure_ascii=False, default=str)[:1000])
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, default=str)[:1000])
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value or "")


def tail_user_messages(messages: list[Any], limit: int = 8) -> list[str]:
    out: list[str] = []
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = re.sub(r"\s+", " ", text_from_content(msg.get("content"))).strip()
        if not text:
            continue
        out.append(text[:1200])
        if len(out) >= limit:
            break
    return list(reversed(out))


@dataclass
class Case:
    db_name: str
    request_id: str
    created_at: float
    requested_model: str
    final_model: str
    msg_count: int
    input_tokens: int
    output_tokens: int
    body: dict[str, Any]


def load_case(log_dir: Path, db_name: str, request_id: str) -> Case:
    db = log_dir / db_name
    if not db.exists():
        raise SystemExit(f"log DB not found: {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute(
        """
        select l.created_at, l.requested_model, l.final_model, l.msg_count,
               l.input_tokens, l.output_tokens, d.request_body
        from request_log l join request_detail d using(request_id)
        where l.request_id=?
        """,
        (request_id,),
    ).fetchone()
    con.close()
    if row is None:
        raise SystemExit(f"request not found: {db_name} {request_id}")
    body = json.loads(row["request_body"])
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise SystemExit(f"request body is not Anthropic messages-shaped: {request_id}")
    return Case(
        db_name=db_name,
        request_id=request_id,
        created_at=float(row["created_at"] or 0),
        requested_model=str(row["requested_model"] or ""),
        final_model=str(row["final_model"] or ""),
        msg_count=int(row["msg_count"] or 0),
        input_tokens=int(row["input_tokens"] or 0),
        output_tokens=int(row["output_tokens"] or 0),
        body=body,
    )


def make_compact_body(source: dict[str, Any], compact_prompt: str) -> dict[str, Any]:
    body = json.loads(json.dumps(source, ensure_ascii=False, default=str))
    body["model"] = "gpt-5.5"
    body["stream"] = False
    body["max_tokens"] = int(body.get("max_tokens") or 20000)
    body.setdefault("messages", [])
    body["messages"] = list(body["messages"])
    body["messages"].append({
        "role": "user",
        "content": [{"type": "text", "text": compact_prompt}],
    })
    return body


def call_parrot(
    endpoint: str,
    api_key: str,
    body: dict[str, Any],
    timeout: int,
    label: str,
    *,
    retries: int = 0,
) -> tuple[str, dict[str, Any]]:
    last_error: Exception | None = None
    for attempt in range(1, max(1, retries + 1) + 1):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            endpoint.rstrip("/") + "/v1/messages",
            data=payload,
            headers={
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            elapsed = time.time() - started
            obj = json.loads(raw)
            text = compact_rescue.extract_anthropic_message_text(obj).strip()
            if not text:
                raise RuntimeError(f"{label} returned empty text: {raw[:2000]}")
            usage = obj.get("usage") if isinstance(obj, dict) else None
            suffix = f" attempt={attempt}" if attempt > 1 else ""
            print(f"  {label}{suffix}: {len(text)} chars, {elapsed:.1f}s, usage={usage}", flush=True)
            return text, obj
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"{label} HTTP {exc.code}: {raw[:2000]}")
        except Exception as exc:
            last_error = RuntimeError(f"{label} request failed: {exc}")
        if attempt <= retries:
            print(f"  {label}: retry {attempt}/{retries} after error: {last_error}", flush=True)
            time.sleep(min(20, 3 * attempt))
    assert last_error is not None
    raise last_error


def quality_heuristics(summary: str, tail_users: list[str]) -> dict[str, Any]:
    low = summary.lower()
    section_names = [
        "primary request", "key technical", "files and code", "errors", "problem solving",
        "all user messages", "pending", "current work", "next step",
    ]
    path_hits = len(re.findall(r"(?:/opt/|/root/|src/|scripts/|config\.json|\.py|\.ts|\.go|\.vue|\.md)", summary))
    command_hits = len(re.findall(r"\b(?:pytest|python3?|curl|git|systemctl|sqlite3|ssh|npm|pnpm|go test)\b", summary))
    recent_overlap = []
    for item in tail_users[-3:]:
        words = [w for w in re.findall(r"[\w\-/\.]{4,}", item.lower()) if len(w) >= 4]
        if not words:
            recent_overlap.append(0.0)
            continue
        hits = sum(1 for w in words[:80] if w in low)
        recent_overlap.append(round(hits / min(len(words), 80), 3))
    return {
        "chars": len(summary),
        "has_analysis_block": "<analysis>" in low and "</analysis>" in low,
        "has_summary_block": "<summary>" in low and "</summary>" in low,
        "sections_present": [name for name in section_names if name in low],
        "section_count": sum(1 for name in section_names if name in low),
        "path_hits": path_hits,
        "command_hits": command_hits,
        "recent_tail_overlap_last3": recent_overlap,
    }


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_case(case_no: int, case: Case, args: argparse.Namespace, api_key: str, compact_prompt: str) -> dict[str, Any]:
    case_dir = Path(args.out_dir) / f"case_{case_no:02d}_{case.db_name.replace('.db','')}_{case.request_id[:8]}"
    case_dir.mkdir(parents=True, exist_ok=True)
    compact_body = make_compact_body(case.body, compact_prompt)
    messages = compact_body.get("messages") or []
    chunks = compact_rescue.split_messages_for_compact(
        messages,
        target_tokens=args.target_tokens,
        model="gpt-5.5",
    )
    tail_users = tail_user_messages(case.body.get("messages") or [])
    meta = {
        "case_no": case_no,
        "db_name": case.db_name,
        "request_id": case.request_id,
        "created_at": case.created_at,
        "created_at_local": time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(case.created_at)),
        "requested_model": case.requested_model,
        "final_model": case.final_model,
        "msg_count": case.msg_count,
        "input_tokens": case.input_tokens,
        "output_tokens": case.output_tokens,
        "body_chars": len(json.dumps(case.body, ensure_ascii=False, default=str)),
        "chunk_count": len(chunks),
        "target_tokens": args.target_tokens,
        "tail_user_messages": tail_users,
    }
    write_text(case_dir / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
    write_text(case_dir / "tail_user_messages.md", "\n\n---\n\n".join(tail_users))
    print(f"\nCASE {case_no}: {meta['created_at_local']} rid={case.request_id} messages={case.msg_count} chunks={len(chunks)}", flush=True)

    segment_summaries: list[str] = []
    for idx, chunk in enumerate(chunks, start=1):
        seg_md = case_dir / f"segment_{idx:02d}.md"
        if seg_md.exists():
            text = seg_md.read_text(encoding="utf-8")
            print(f"  segment {idx}/{len(chunks)}: reuse existing {len(text)} chars", flush=True)
        else:
            seg_body = compact_rescue.build_segment_summary_body(
                compact_body,
                chunk,
                segment_index=idx,
                segment_count=len(chunks),
            )
            seg_body["model"] = "gpt-5.5"
            if args.segment_max_tokens:
                seg_body["max_tokens"] = args.segment_max_tokens
            text, raw = call_parrot(
                args.endpoint,
                api_key,
                seg_body,
                args.timeout,
                f"segment {idx}/{len(chunks)}",
                retries=args.retries,
            )
            write_text(seg_md, text)
            write_text(case_dir / f"segment_{idx:02d}.json", json.dumps(raw, ensure_ascii=False, indent=2))
        segment_summaries.append(text)

    final_md = case_dir / "final_summary.md"
    if final_md.exists():
        summary = final_md.read_text(encoding="utf-8")
        print(f"  reduce: reuse existing {len(summary)} chars", flush=True)
    else:
        reduce_body = compact_rescue.build_reduce_summary_body(compact_body, segment_summaries)
        reduce_body["model"] = "gpt-5.5"
        if args.reduce_max_tokens:
            reduce_body["max_tokens"] = args.reduce_max_tokens
        summary, raw = call_parrot(
            args.endpoint,
            api_key,
            reduce_body,
            args.timeout,
            "reduce",
            retries=args.retries,
        )
        write_text(final_md, summary)
        write_text(case_dir / "final_response.json", json.dumps(raw, ensure_ascii=False, indent=2))
    heur = quality_heuristics(summary, tail_users)
    write_text(case_dir / "heuristics.json", json.dumps(heur, ensure_ascii=False, indent=2))
    return {**meta, "heuristics": heur, "case_dir": str(case_dir)}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--log-dir", default=str(ROOT / "logs"))
    p.add_argument("--config", default=str(ROOT / "config.json"))
    p.add_argument("--key-name", default="OpenClaw")
    p.add_argument("--endpoint", default="http://127.0.0.1:22122")
    p.add_argument("--compact-prompt", default="/opt/claude-code.txt")
    p.add_argument("--out-dir", default=str(ROOT / "_debug" / "compact-quality" / time.strftime("%Y%m%d-%H%M%S")))
    p.add_argument("--target-tokens", type=int, default=100000)
    p.add_argument("--segment-max-tokens", type=int, default=0)
    p.add_argument("--reduce-max-tokens", type=int, default=0)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--limit", type=int, default=5)
    args = p.parse_args()

    api_key = extract_downstream_key(Path(args.config), args.key_name)
    compact_prompt = load_compact_prompt(Path(args.compact_prompt))
    selected = DEFAULT_CASES[: max(1, args.limit)]
    results = []
    for idx, (db_name, request_id) in enumerate(selected, start=1):
        case = load_case(Path(args.log_dir), db_name, request_id)
        results.append(run_case(idx, case, args, api_key, compact_prompt))
    out_dir = Path(args.out_dir)
    write_text(out_dir / "report.json", json.dumps({"results": results}, ensure_ascii=False, indent=2))
    print(f"\nDONE: {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
