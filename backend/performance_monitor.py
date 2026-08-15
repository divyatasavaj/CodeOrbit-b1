"""
Performance monitoring and timing for the analysis pipeline.
"""
import time
import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from contextlib import contextmanager

logger = logging.getLogger("codeoracle")


@dataclass
class TimingEntry:
    """Single timing measurement."""
    name: str
    start: float = 0
    end: float = 0
    duration: float = 0
    
    def start_timer(self):
        self.start = time.time()
    
    def stop_timer(self):
        self.end = time.time()
        self.duration = self.end - self.start


@dataclass
class PerformanceMetrics:
    """Complete performance metrics for an analysis run."""
    zip_extraction: float = 0
    file_parsing: float = 0
    ast_analysis: float = 0
    dependency_graph: float = 0
    context_generation: float = 0
    llm_total: float = 0
    llm_requests: int = 0
    test_generation: float = 0
    test_execution: float = 0
    coverage_calculation: float = 0
    refactoring: float = 0
    total_time: float = 0
    cache_hits: int = 0
    cache_misses: int = 0
    files_analyzed: int = 0
    functions_found: int = 0
    
    @property
    def cache_hit_rate(self) -> float:
        """Calculate cache hit rate percentage."""
        total = self.cache_hits + self.cache_misses
        return (self.cache_hits / total * 100) if total > 0 else 0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "zip_extraction": round(self.zip_extraction, 2),
            "file_parsing": round(self.file_parsing, 2),
            "ast_analysis": round(self.ast_analysis, 2),
            "dependency_graph": round(self.dependency_graph, 2),
            "context_generation": round(self.context_generation, 2),
            "llm_total": round(self.llm_total, 2),
            "llm_requests": self.llm_requests,
            "test_generation": round(self.test_generation, 2),
            "test_execution": round(self.test_execution, 2),
            "coverage_calculation": round(self.coverage_calculation, 2),
            "refactoring": round(self.refactoring, 2),
            "total_time": round(self.total_time, 2),
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_hit_rate": round(self.cache_hit_rate, 1),
            "files_analyzed": self.files_analyzed,
            "functions_found": self.functions_found
        }
    
    def summary(self) -> str:
        """Generate human-readable summary."""
        return f"""Performance Summary:
  Total: {self.total_time:.1f}s
  AST Analysis: {self.ast_analysis:.1f}s
  Dependency Graph: {self.dependency_graph:.1f}s
  LLM Total: {self.llm_total:.1f}s ({self.llm_requests} requests)
  Test Generation: {self.test_generation:.1f}s
  Test Execution: {self.test_execution:.1f}s
  Coverage: {self.coverage_calculation:.1f}s
  Refactoring: {self.refactoring:.1f}s
  Cache Hit Rate: {self.cache_hit_rate:.1f}%
  Files: {self.files_analyzed}
  Functions: {self.functions_found}"""


class PerformanceMonitor:
    """Monitor and track performance metrics during analysis."""
    
    def __init__(self):
        self.metrics = PerformanceMetrics()
        self._timers: Dict[str, TimingEntry] = {}
        self._start_time = 0
    
    def start(self):
        """Start overall timing."""
        self._start_time = time.time()
    
    def stop(self):
        """Stop overall timing."""
        self.metrics.total_time = time.time() - self._start_time
    
    @contextmanager
    def timer(self, name: str):
        """Context manager for timing a code block."""
        entry = TimingEntry(name=name)
        entry.start_timer()
        try:
            yield entry
        finally:
            entry.stop_timer()
            self._update_metric(name, entry.duration)
    
    def _update_metric(self, name: str, duration: float):
        """Update the corresponding metric field."""
        metric_map = {
            "zip_extraction": "zip_extraction",
            "file_parsing": "file_parsing",
            "ast_analysis": "ast_analysis",
            "dependency_graph": "dependency_graph",
            "context_generation": "context_generation",
            "llm": "llm_total",
            "test_generation": "test_generation",
            "test_execution": "test_execution",
            "coverage": "coverage_calculation",
            "refactoring": "refactoring"
        }
        
        if name in metric_map:
            setattr(self.metrics, metric_map[name], duration)
    
    def record_llm_request(self):
        """Record an LLM request."""
        self.metrics.llm_requests += 1
    
    def record_cache_hit(self):
        """Record a cache hit."""
        self.metrics.cache_hits += 1
    
    def record_cache_miss(self):
        """Record a cache miss."""
        self.metrics.cache_misses += 1
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get current metrics as dictionary."""
        return self.metrics.to_dict()
    
    def get_summary(self) -> str:
        """Get human-readable summary."""
        return self.metrics.summary()
