"""
CodeOracle benchmark harness.

Drives a RUNNING backend over HTTP (SSE + REST) and records real, measured
timings - no simulated numbers. Start the backend first, e.g.:

    # offline / reproducible (mock LLM provider)
    set CODEORACLE_LLM_MOCK=1
    set CODEORACLE_LLM_MOCK_LATENCY=1.0
    uvicorn main:app --port 8000

    python benchmark.py --base-url http://127.0.0.1:8000

    # real provider
    uvicorn main:app --port 8000
    python benchmark.py --base-url http://127.0.0.1:8000 --functions 16,50
"""
import argparse
import io
import json
import statistics
import sys
import time
import zipfile

import httpx

SUPPORTED_SIZES = [16, 50, 100, 250, 500]


# --------------------------------------------------------------------------
# Synthetic repositories
# --------------------------------------------------------------------------
def _synth_function(idx: int) -> str:
    """Deterministic function with varied complexity (so priorities differ)."""
    if idx % 5 == 0:
        return f'''def service_{idx}(records, threshold, limit=None):
    """Process records, aggregate totals and branch on the threshold."""
    total = 0
    seen = []
    for row in records:
        if row is None:
            continue
        if row.get("value") is None:
            raise ValueError("value missing")
        if row["value"] > threshold:
            total += row["value"]
            seen.append(row.get("id"))
        elif row["value"] == threshold:
            total += 1
        else:
            try:
                total -= row["value"] % 7
            except Exception:
                pass
    if limit is not None and total > limit:
        total = limit
    return {{"total": total, "count": len(seen)}}
'''
    if idx % 5 == 1:
        return f'''def compute_{idx}(a, b, c):
    if a is None or b is None:
        return 0
    result = a * b
    while result > c:
        result -= c
    return result
'''
    if idx % 5 == 2:
        return f'''class Model_{idx}:
    def __init__(self, name, value):
        self.name = name
        self.value = value

    def get_name(self):
        return self.name

    def set_value(self, value):
        self.value = value

    def describe(self):
        return "Model %s has value %s" % (self.name, self.value)
'''
    if idx % 5 == 3:
        return f'''def handler_{idx}(request):
    payload = request.get("payload") or {{}}
    if not payload:
        return {{"error": "empty"}}
    items = payload.get("items", [])
    return {{"count": len(items), "ok": True}}
'''
    return f'''def helper_{idx}(value):
    return value
'''


def make_synthetic_zip(n_functions: int) -> bytes:
    """Build a ZIP with n_functions spread over files (10 per file)."""
    buf = io.BytesIO()
    per_file = 10
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        idx = 0
        file_no = 0
        while idx < n_functions:
            file_no += 1
            lines = [f'"""{file_no} synthetic module."""', "import datetime", ""]
            for _ in range(per_file):
                if idx >= n_functions:
                    break
                lines.append(_synth_function(idx))
                lines.append("")
                idx += 1
            z.writestr(f"pkg/module_{file_no}.py", "\n".join(lines))
        # noise that must be ignored
        z.writestr("node_modules/junk.js", "module.exports = 1;\n")
        z.writestr("__pycache__/x.pyc", "binary")
        z.writestr("package-lock.json", "{}")
    return buf.getvalue()


def demo_zip() -> bytes:
    with open("../demo/sample_legacy.py", "rb") as fh:
        content = fh.read()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("sample_legacy.py", content)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Job measurement
# --------------------------------------------------------------------------
def start_job(client: httpx.Client, zip_bytes: bytes, name: str) -> str:
    r = client.post("/analyze", files={"file": (name, zip_bytes, "application/zip")})
    r.raise_for_status()
    return r.json()["job_id"]


def watch_job(client: httpx.Client, job_id: str, timeout: float = 900.0) -> dict:
    """Follow the typed SSE stream and timestamp every milestone."""
    t0 = time.perf_counter()
    result = {
        "structural_wall": None,
        "first_result_wall": None,
        "first_ai_wall": None,
        "complete_wall": None,
        "thresholds": {},
        "events": 0,
        "batches": 0,
        "terminal": None,
    }
    observed = []  # (t, completed)
    with client.stream("GET", f"/jobs/{job_id}/events", timeout=timeout) as r:
        for line in r.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            result["events"] += 1
            t = time.perf_counter() - t0
            etype = ev.get("type")
            if etype == "structural_complete" and result["structural_wall"] is None:
                result["structural_wall"] = t
                total = ev.get("functions") or 0
                result["total"] = total
            elif etype == "batch_completed":
                result["batches"] += 1
                completed = ev.get("completed", 0)
                observed.append((t, completed))
                if result["first_result_wall"] is None and completed > 0:
                    result["first_result_wall"] = t
                if result["first_ai_wall"] is None and any(
                    (f or {}).get("ai_status") in ("ai", "cached") for f in (ev.get("functions") or [])
                ):
                    result["first_ai_wall"] = t
            elif etype in ("analysis_completed", "analysis_cancelled", "analysis_error"):
                result["terminal"] = etype
                result["complete_wall"] = t
                result["completed"] = ev.get("completed", 0)
                result["failed"] = ev.get("failed", 0)
                result["ai_status"] = ev.get("status") or ev.get("ai_status")
                result["llm_calls"] = ev.get("llm_calls", 0)
                result["avg_batch_size"] = ev.get("avg_batch_size", 0)
                result["max_concurrency"] = ev.get("max_concurrency", 0)
                break

    total = result.get("total") or 0
    for pct in (25, 50, 75, 100):
        target = total * pct / 100.0
        result["thresholds"][pct] = _crossing_time(observed, target)
    return result


def watch_job_legacy(client: httpx.Client, job_id: str, timeout: float = 1800.0) -> dict:
    """Watch the pre-optimization pipeline (poll /results; no progressive events)."""
    t0 = time.perf_counter()
    result = {
        "structural_wall": None,
        "first_result_wall": None,
        "first_ai_wall": None,
        "complete_wall": None,
        "thresholds": {25: None, 50: None, 75: None, 100: None},
        "events": 0,
        "batches": 0,
        "terminal": None,
    }
    while time.perf_counter() - t0 < timeout:
        try:
            data = client.get(f"/results/{job_id}").json()
        except Exception:  # noqa: BLE001
            time.sleep(1.0)
            continue
        status = data.get("status")
        if status == "processing":
            time.sleep(0.5)
            continue
        if status == "error":
            result["terminal"] = "analysis_error"
            result["complete_wall"] = time.perf_counter() - t0
            return result
        result["complete_wall"] = time.perf_counter() - t0
        result["terminal"] = "analysis_completed"
        perf = data.get("performance", {}) or {}
        result["llm_calls"] = perf.get("llm_requests") or perf.get("llm_total")
        result["llm_calls"] = perf.get("llm_requests")
        total = (data.get("summary") or {}).get("functions_found") or 0
        result["total"] = total
        result["completed"] = total
        result["failed"] = 0
        result["ai_status"] = "legacy_complete"
        result["avg_batch_size"] = None
        result["max_concurrency"] = 1
        return result
    result["terminal"] = "timeout"
    return result


def _crossing_time(observed, target) -> float:
    if target <= 0:
        return 0.0
    prev_t, prev_c = 0.0, 0
    for t, c in observed:
        if c >= target:
            if c == prev_c:
                return t
            frac = (target - prev_c) / float(c - prev_c)
            return prev_t + frac * (t - prev_t)
        prev_t, prev_c = t, c
    return observed[-1][0] if observed else None


def fetch(client: httpx.Client, job_id: str) -> dict:
    return client.get(f"/results/{job_id}").json()


def cache_stats(client: httpx.Client) -> dict:
    return client.get("/cache/stats").json()


def clear_cache(client: httpx.Client) -> None:
    client.post("/cache/clear")


# --------------------------------------------------------------------------
# Benchmarks
# --------------------------------------------------------------------------
def bench_repo(client, zip_bytes, name, *, clear=True, do_tests=False, mode="new"):
    if clear:
        clear_cache(client)
    t_upload = time.perf_counter()
    job_id = start_job(client, zip_bytes, name)
    upload_time = time.perf_counter() - t_upload
    res = watch_job_legacy(client, job_id) if mode == "legacy" else watch_job(client, job_id)
    res["job_id"] = job_id
    res["upload_time"] = upload_time
    perf = fetch(client, job_id).get("performance", {})
    res["phases"] = {
        "zip_extraction": perf.get("zip_extraction"),
        "file_parsing": perf.get("file_parsing"),
        "dependency_graph": perf.get("dependency_graph"),
        "structural_total": perf.get("structural_total"),
        "ai_total": perf.get("ai_total"),
        "first_ai_result": perf.get("first_ai_result"),
        "ai_llm_calls": perf.get("ai_llm_calls"),
    }
    if do_tests:
        res["tests"] = bench_on_demand(client, job_id)
    return res


def bench_on_demand(client, job_id):
    """Measure one test generation + one refactor generation on real functions."""
    data = fetch(client, job_id)
    out = {}
    picked = None
    for group in data.get("explanation", []):
        for func in group.get("functions", []):
            if func.get("ai_status") in ("ai", "cached"):
                picked = (func.get("name"), group.get("filename"))
                break
        if picked:
            break
    if not picked:
        return out
    name, filename = picked
    out["function"] = name
    t0 = time.perf_counter()
    r = client.post(f"/generate/tests/{job_id}", json={"function_name": name, "filename": filename})
    out["tests_cold"] = round(time.perf_counter() - t0, 3)
    out["tests_ok"] = r.status_code == 200
    t0 = time.perf_counter()
    client.post(f"/generate/tests/{job_id}", json={"function_name": name, "filename": filename})
    out["tests_warm"] = round(time.perf_counter() - t0, 3)
    t0 = time.perf_counter()
    r = client.post(f"/generate/refactor/{job_id}", json={"function_name": name, "filename": filename})
    out["refactor_cold"] = round(time.perf_counter() - t0, 3)
    out["refactor_ok"] = r.status_code == 200
    t0 = time.perf_counter()
    client.post(f"/generate/refactor/{job_id}", json={"function_name": name, "filename": filename})
    out["refactor_warm"] = round(time.perf_counter() - t0, 3)
    return out


def _fmt(v, suffix="s"):
    if v is None:
        return "-"
    return f"{v:.2f}{suffix}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--functions", default="16,50,100,250,500")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--warm", action="store_true", help="also measure a warm (cached) run per size")
    ap.add_argument("--on-demand", action="store_true", help="measure test/refactor generation on the first repo")
    ap.add_argument("--mode", choices=["new", "legacy"], default="new", help="'legacy' measures the pre-optimization pipeline (poll /results)")
    args = ap.parse_args()

    sizes = [int(x) for x in args.functions.split(",") if x.strip()]
    client = httpx.Client(base_url=args.base_url, timeout=httpx.Timeout(connect=5.0, read=1200.0, write=30.0, pool=5.0))

    try:
        health = client.get("/health")
        health.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"Backend not reachable at {args.base_url}: {exc}")
        return 1

    print(f"Backend: {args.base_url} | {health.json()}")
    print(f"Mode: {args.mode} | sizes: {sizes} | repeats: {args.repeats} | warm: {args.warm}\n")

    summary_rows = []
    for size in sizes:
        zip_bytes = demo_zip() if size == 16 else make_synthetic_zip(size)
        name = f"bench_{size}.zip"
        for run in range(args.repeats):
            cold = bench_repo(client, zip_bytes, name, clear=(run == 0), do_tests=(args.on_demand and size == sizes[0] and run == 0), mode=args.mode)
            warm = None
            if args.warm:
                warm = bench_repo(client, zip_bytes, name, clear=False, mode=args.mode)
            summary_rows.append({
                "size": size,
                "run": run,
                "total": cold.get("total"),
                "structural": cold["structural_wall"],
                "first": cold["first_result_wall"],
                "first_ai": cold["first_ai_wall"],
                "t25": cold["thresholds"].get(25),
                "t50": cold["thresholds"].get(50),
                "t75": cold["thresholds"].get(75),
                "t100": cold["thresholds"].get(100),
                "complete": cold.get("complete_wall"),
                "llm_calls": cold.get("llm_calls"),
                "avg_batch": cold.get("avg_batch_size"),
                "max_conc": cold.get("max_concurrency"),
                "phases": cold.get("phases"),
                "warm_complete": (warm or {}).get("complete_wall"),
                "tests": (cold.get("tests") or {}).get("tests_cold"),
                "refactor": (cold.get("tests") or {}).get("refactor_cold"),
                "on_demand": cold.get("tests"),
                "failed": cold.get("failed"),
                "ai_status": cold.get("ai_status"),
            })
            print(f"[size={size}] run={run} ai_status={cold.get('ai_status')} failed={cold.get('failed')}")

    print("\n=== PER-REPOSITORY RESULTS (wall clock, seconds) ===")
    header = (f"{'Asked':>6} {'Analyzed':>8} {'Struct':>8} {'1stAny':>8} {'1stAI':>8} {'25%':>8} {'50%':>8} "
              f"{'75%':>8} {'100%':>8} {'Warm':>8} {'Calls':>6} {'Batch':>6} {'Conc':>5}")
    print(header)
    print("-" * len(header))
    for row in summary_rows:
        print(f"{row['size']:>6} {str(row['total']):>8} {_fmt(row['structural']):>8} {_fmt(row['first']):>8} "
              f"{_fmt(row.get('first_ai')):>8} "
              f"{_fmt(row['t25']):>8} {_fmt(row['t50']):>8} {_fmt(row['t75']):>8} {_fmt(row['t100']):>8} "
              f"{_fmt(row['warm_complete']):>8} {str(row['llm_calls']):>6} "
              f"{str(row['avg_batch']):>6} {str(row['max_conc']):>5}")

    print("\n=== SERVER-REPORTED PHASE BREAKDOWN ===")
    for row in summary_rows:
        p = row["phases"] or {}
        print(f"[{row['size']:>4} funcs] zip={p.get('zip_extraction')} parse={p.get('file_parsing')} "
              f"graph={p.get('dependency_graph')} structural={p.get('structural_total')} "
              f"ai_total={p.get('ai_total')} first_ai={p.get('first_ai_result')} llm_calls={p.get('ai_llm_calls')}")

    if args.on_demand:
        for row in summary_rows:
            od = row.get("on_demand")
            if od:
                print(f"\n=== ON-DEMAND ({row['size']} funcs, function '{od.get('function')}') ===")
                print(f"tests cold={od.get('tests_cold')}s warm={od.get('tests_warm')}s ok={od.get('tests_ok')}")
                print(f"refactor cold={od.get('refactor_cold')}s warm={od.get('refactor_warm')}s ok={od.get('refactor_ok')}")

    print("\n=== CACHE ===")
    print(cache_stats(client))
    return 0


if __name__ == "__main__":
    sys.exit(main())
