"""
Benchmark script for CodeOracle.
Measures performance of AST analysis, dependency graph, and LLM calls.
"""
import sys
import os
import time
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import ast_analyzer
import dependency_analyzer
import context_builder
import cache


def benchmark_file(filepath: str) -> dict:
    """Benchmark analysis of a single file."""
    results = {
        "file": filepath,
        "lines": 0,
        "functions": 0,
        "classes": 0,
        "methods": 0,
        "ast_nodes": 0,
        "complexity_avg": 0,
        "imports": 0,
        "global_vars": 0,
        "ast_time": 0,
        "graph_time": 0,
        "context_time": 0
    }
    
    start = time.time()
    analysis = ast_analyzer.analyze_file(filepath)
    results["ast_time"] = round(time.time() - start, 3)
    
    results["lines"] = analysis.line_count
    results["functions"] = len(analysis.functions)
    results["classes"] = len(analysis.classes)
    results["methods"] = sum(len(c.methods) for c in analysis.classes)
    results["ast_nodes"] = analysis.ast_nodes
    results["imports"] = len(analysis.imports)
    results["global_vars"] = len(analysis.global_variables)
    
    complexities = [f.complexity for f in analysis.functions]
    for cls in analysis.classes:
        complexities.extend([m.complexity for m in cls.methods])
    results["complexity_avg"] = round(sum(complexities) / len(complexities), 2) if complexities else 0
    
    start = time.time()
    graph = dependency_analyzer.build_dependency_graph([analysis.to_dict()])
    results["graph_time"] = round(time.time() - start, 3)
    results["graph_nodes"] = len(graph.nodes)
    results["graph_edges"] = len(graph.edges)
    
    start = time.time()
    batches = context_builder.build_file_context(
        analysis.to_dict(),
        analysis.source,
        max_functions_per_batch=10
    )
    results["context_time"] = round(time.time() - start, 3)
    results["context_batches"] = len(batches)
    
    candidates = ast_analyzer.find_refactoring_candidates(analysis)
    results["refactor_candidates"] = len(candidates)
    
    return results


def benchmark_directory(dirpath: str) -> dict:
    """Benchmark analysis of a directory."""
    all_results = []
    total_start = time.time()
    
    for root, dirs, files in os.walk(dirpath):
        dirs[:] = [d for d in dirs if d not in ["__pycache__", "node_modules", ".git"]]
        
        for file in files:
            if file.endswith(".py") and not file.startswith("test_"):
                filepath = os.path.join(root, file)
                try:
                    result = benchmark_file(filepath)
                    all_results.append(result)
                    print(f"  Analyzed: {file} ({result['lines']} lines, {result['functions']} functions)")
                except Exception as e:
                    print(f"  Error analyzing {file}: {e}")
    
    total_time = round(time.time() - total_start, 3)
    
    summary = {
        "total_files": len(all_results),
        "total_lines": sum(r["lines"] for r in all_results),
        "total_functions": sum(r["functions"] for r in all_results),
        "total_classes": sum(r["classes"] for r in all_results),
        "total_methods": sum(r["methods"] for r in all_results),
        "total_ast_nodes": sum(r["ast_nodes"] for r in all_results),
        "total_ast_time": round(sum(r["ast_time"] for r in all_results), 3),
        "total_graph_time": round(sum(r["graph_time"] for r in all_results), 3),
        "total_context_time": round(sum(r["context_time"] for r in all_results), 3),
        "total_time": total_time,
        "avg_complexity": round(sum(r["complexity_avg"] for r in all_results) / len(all_results), 2) if all_results else 0,
        "files": all_results
    }
    
    return summary


def print_summary(summary: dict):
    """Print benchmark summary."""
    print("\n" + "=" * 60)
    print("BENCHMARK RESULTS")
    print("=" * 60)
    print(f"Files analyzed: {summary['total_files']}")
    print(f"Total lines: {summary['total_lines']}")
    print(f"Functions: {summary['total_functions']}")
    print(f"Classes: {summary['total_classes']}")
    print(f"Methods: {summary['total_methods']}")
    print(f"AST nodes: {summary['total_ast_nodes']}")
    print(f"Average complexity: {summary['avg_complexity']}")
    print("-" * 60)
    print(f"AST analysis time: {summary['total_ast_time']}s")
    print(f"Graph build time: {summary['total_graph_time']}s")
    print(f"Context generation time: {summary['total_context_time']}s")
    print(f"Total analysis time: {summary['total_time']}s")
    print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python benchmark.py <directory_or_file>")
        print("Example: python benchmark.py demo/")
        sys.exit(1)
    
    target = sys.argv[1]
    
    if os.path.isfile(target):
        print(f"Analyzing file: {target}")
        result = benchmark_file(target)
        print_summary({"files": [result], **{k: v for k, v in result.items() if k != "files"}})
    elif os.path.isdir(target):
        print(f"Analyzing directory: {target}")
        summary = benchmark_directory(target)
        print_summary(summary)
        
        output_file = "benchmark_results.json"
        with open(output_file, 'w') as f:
            json.dump(summary, f, indent=2)
        print(f"\nDetailed results saved to: {output_file}")
    else:
        print(f"Error: {target} not found")
        sys.exit(1)
