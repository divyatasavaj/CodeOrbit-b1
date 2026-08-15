import json, requests, time

job_id = "demo"

# Poll for results
for i in range(20):
    resp = requests.get(f"http://localhost:8000/results/{job_id}")
    data = resp.json()
    prog = data.get("progress", "N/A")
    prog_str = prog[:80] if prog else "N/A"
    print(f"Status: {data.get('status')}, Progress: {prog_str}")
    if data.get("status") in ("complete", "error"):
        break
    time.sleep(3)

if data.get("status") == "complete":
    summary = data.get("summary", {})
    print(f"\nFiles analyzed: {summary.get('files_analyzed')}")
    print(f"Functions found: {summary.get('functions_found')}")
    print(f"Avg coverage: {summary.get('avg_coverage')}%")
    print(f"Languages: {summary.get('languages')}")
    print(f"\nNumber of explanations: {len(data.get('explanation', []))}")
    print(f"Number of test results: {len(data.get('tests', []))}")
    print(f"Number of refactor results: {len(data.get('refactor', []))}")
else:
    print(f"Error: {data.get('message', 'Unknown error')}")