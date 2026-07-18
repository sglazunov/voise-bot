"""Д16: эталонный тест качества протоколов.

Прогоняет каждую эталонную встречу (tests/eval/cases/*) через настоящий
analyze_transcript (+ опционально verify_protocol), сверяет с reference.json и
пишет метрики в history.jsonl. Пороги релиза: task recall >= 0.90,
owner accuracy >= 0.95.

Запуск:  python -m tests.eval.run_eval --provider ollama [--strict]
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
CASES_DIR = EVAL_DIR / "cases"
HISTORY = EVAL_DIR / "history.jsonl"

# Matching thresholds: reference wording differs from the model's, so tasks are
# matched by normalised similarity, not equality.
RATIO_MIN = 0.55        # difflib ratio on normalised text
JACCARD_MIN = 0.40      # ...or stem-set overlap (word-order independent)
TOPIC_JACCARD_MIN = 0.25
_STEM = 5


def _norm(s: str) -> str:
    s = re.sub(r"[^\w\s]", " ", (s or "").lower().replace("ё", "е"))
    return re.sub(r"\s+", " ", s).strip()


def _stems(s: str) -> set[str]:
    return {w[:_STEM] for w in _norm(s).split() if len(w) >= 4}


def _similar(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    sa, sb = _stems(a), _stems(b)
    jac = len(sa & sb) / len(sa | sb) if sa | sb else 0.0
    return max(ratio, jac / max(JACCARD_MIN / RATIO_MIN, 1e-9) * RATIO_MIN)


def _matches(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if difflib.SequenceMatcher(None, na, nb).ratio() >= RATIO_MIN:
        return True
    sa, sb = _stems(a), _stems(b)
    return bool(sa | sb) and len(sa & sb) / len(sa | sb) >= JACCARD_MIN


def _norm_owner(o) -> str | None:
    o = _norm(str(o or ""))
    if not o or o in ("-", "—", "не назначен", "неизвестно"):
        return None
    return o.split()[0]     # match by first name — «Кирилл» == «Кирилл С.»


def _out_tasks(result: dict) -> list[dict]:
    """All task-shaped items the protocol produced, whatever list they landed in."""
    out = []
    for key in ("tasks", "minor_tasks", "done_tasks"):
        for it in result.get(key) or []:
            if isinstance(it, dict) and (it.get("task") or "").strip():
                out.append({"task": it["task"], "owner": it.get("owner"), "list": key})
    return out


def evaluate_case(result: dict, ref: dict) -> dict:
    """Compare one protocol against its reference; pure function (unit-tested)."""
    out_tasks = _out_tasks(result)
    ref_tasks = [t for t in (ref.get("tasks") or []) + (ref.get("done_tasks") or [])
                 if (t.get("task") or "").strip()]

    matched: list[tuple[dict, dict]] = []
    used: set[int] = set()
    for rt in ref_tasks:
        best, best_sim = None, 0.0
        for i, ot in enumerate(out_tasks):
            if i in used or not _matches(rt["task"], ot["task"]):
                continue
            sim = _similar(rt["task"], ot["task"])
            if sim > best_sim:
                best, best_sim = i, sim
        if best is not None:
            used.add(best)
            matched.append((rt, out_tasks[best]))

    recall = len(matched) / len(ref_tasks) if ref_tasks else 1.0
    precision = len(matched) / len(out_tasks) if out_tasks else 1.0
    owner_ok = sum(1 for rt, ot in matched
                   if _norm_owner(rt.get("owner")) == _norm_owner(ot.get("owner")))
    owner_acc = owner_ok / len(matched) if matched else 1.0

    ref_dec = [d for d in (ref.get("decisions") or []) if str(d).strip()]
    out_dec = [str(d) for d in (result.get("decisions") or [])]
    dec_hit = sum(1 for rd in ref_dec
                  if any(_matches(str(rd), od) for od in out_dec))
    dec_recall = dec_hit / len(ref_dec) if ref_dec else 1.0

    ref_topics = [t for t in (ref.get("topics") or []) if str(t).strip()]
    out_blobs = [f"{d.get('topic', '')} {d.get('details', '')}"
                 for d in (result.get("detailed") or []) if isinstance(d, dict)]
    top_hit = 0
    for rt in ref_topics:
        rs = _stems(str(rt))
        if rs and any(len(rs & _stems(b)) / len(rs) >= TOPIC_JACCARD_MIN
                      for b in out_blobs):
            top_hit += 1
    topic_cov = top_hit / len(ref_topics) if ref_topics else 1.0

    missed = [rt["task"] for rt in ref_tasks
              if not any(m[0] is rt for m in matched)]
    wrong_owner = [{"task": rt["task"], "ref": rt.get("owner"),
                    "got": ot.get("owner")}
                   for rt, ot in matched
                   if _norm_owner(rt.get("owner")) != _norm_owner(ot.get("owner"))]
    return {"task_recall": round(recall, 3), "task_precision": round(precision, 3),
            "owner_accuracy": round(owner_acc, 3),
            "decisions_recall": round(dec_recall, 3),
            "topics_coverage": round(topic_cov, 3),
            "ref_tasks": len(ref_tasks), "out_tasks": len(out_tasks),
            "missed_tasks": missed, "wrong_owner": wrong_owner}


def _git_rev() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10,
                              cwd=EVAL_DIR).stdout.strip()
    except Exception:
        return "?"


def run(provider: str, cases: list[str] | None, verify: bool, strict: bool,
        team: str = "") -> int:
    from app.analyze import analyze_transcript, verify_protocol

    keys = None
    if team:
        # Use the team's own (encrypted-at-rest) LLM keys — the same ones a real
        # job of that team would run with. Server-side only, obviously.
        from app import user_creds
        keys = user_creds.load(team)

    dirs = sorted(d for d in CASES_DIR.iterdir()
                  if d.is_dir() and (d / "transcript.txt").exists()
                  and (d / "reference.json").exists())
    if cases:
        dirs = [d for d in dirs if d.name in cases]
    if not dirs:
        print("Нет эталонных кейсов (tests/eval/cases/*) — см. README.md")
        return 1

    per_case = {}
    for d in dirs:
        transcript = (d / "transcript.txt").read_text(encoding="utf-8")
        notes = ((d / "notes.txt").read_text(encoding="utf-8")
                 if (d / "notes.txt").exists() else "")
        ref = json.loads((d / "reference.json").read_text(encoding="utf-8"))
        t0 = time.time()
        result = analyze_transcript(transcript, provider=provider,
                                    user_notes=notes, keys=keys)
        if verify:
            result = verify_protocol(result, transcript, user_notes=notes,
                                     provider=provider, keys=keys)
        m = evaluate_case(result, ref)
        m["engine"] = result.get("_provider")
        m["seconds"] = round(time.time() - t0, 1)
        per_case[d.name] = m
        print(f"[{d.name}] recall={m['task_recall']} precision={m['task_precision']} "
              f"owner={m['owner_accuracy']} decisions={m['decisions_recall']} "
              f"topics={m['topics_coverage']} ({m['seconds']}s, {m['engine']})")
        if m["missed_tasks"]:
            print("   потеряны:", "; ".join(m["missed_tasks"][:5]))
        if m["wrong_owner"]:
            print("   owner мимо:", json.dumps(m["wrong_owner"][:3], ensure_ascii=False))

    n = len(per_case)
    agg = {k: round(sum(c[k] for c in per_case.values()) / n, 3)
           for k in ("task_recall", "task_precision", "owner_accuracy",
                     "decisions_recall", "topics_coverage")}
    passed = agg["task_recall"] >= 0.90 and agg["owner_accuracy"] >= 0.95
    print(f"\nИТОГО ({n} кейс.): {json.dumps(agg, ensure_ascii=False)}")
    print("ПОРОГИ РЕЛИЗА:", "✅ пройдены" if passed else
          "❌ НЕ пройдены (recall>=0.90, owner>=0.95)")

    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "git": _git_rev(),
            "provider": provider, "verify": verify, "aggregate": agg,
            "passed": passed,
            "cases": {k: {kk: vv for kk, vv in v.items()
                          if kk not in ("missed_tasks", "wrong_owner")}
                      for k, v in per_case.items()},
        }, ensure_ascii=False) + "\n")
    return 0 if (passed or not strict) else 2


def main() -> None:
    ap = argparse.ArgumentParser(description="Эталонный тест качества протоколов")
    ap.add_argument("--provider", default="auto")
    ap.add_argument("--cases", default="", help="имена кейсов через запятую")
    ap.add_argument("--no-verify", action="store_true",
                    help="без grounding-прохода (Д5)")
    ap.add_argument("--strict", action="store_true",
                    help="ненулевой код выхода при провале порогов (CI)")
    ap.add_argument("--team", default="",
                    help="чьими LLM-ключами пользоваться (логин админа команды)")
    a = ap.parse_args()
    sys.exit(run(a.provider, [c.strip() for c in a.cases.split(",") if c.strip()],
                 verify=not a.no_verify, strict=a.strict, team=a.team))


if __name__ == "__main__":
    main()
