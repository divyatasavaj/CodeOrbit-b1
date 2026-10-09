"""Explanations must come from local static AST analysis: never an LLM call.

The whole point of ``CODEORACLE_STATIC_EXPLANATIONS`` is that explanation
generation is instant and cannot be throttled, so these tests fail loudly if
any explanation entry point ever reaches for the network.
"""
import asyncio

import pytest

import config
import llm

SAMPLE = {
    "function_id": "f1",
    "name": "add_student",
    "language": "python",
    "parameters": [],
    "source": (
        "def add_student():\n"
        '    name = input("Enter name: ")\n'
        '    STUDENTS.append({"name": name})\n'
    ),
}


def _explode(*args, **kwargs):
    raise AssertionError("explanation path performed a network/LLM call")


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    monkeypatch.setattr(llm, "generate_with_retry", _explode)
    monkeypatch.setattr(llm, "get_llm_provider", _explode)
    monkeypatch.setattr(llm, "_call_llm_split", _explode)


def test_static_explanations_are_the_default():
    assert config.STATIC_EXPLANATIONS is True


def test_batch_explanations_never_call_the_llm():
    data = asyncio.run(llm.analyze_functions_batch([SAMPLE]))
    assert len(data["results"]) == 1
    entry = data["results"][0]
    assert entry["function_id"] == "f1"
    assert llm.validate_explanation_object(entry)
    assert "input()" in entry["risks"]


def test_explain_function_never_calls_the_llm():
    out = asyncio.run(
        llm.explain_function(
            {
                "display_name": "add_student",
                "name": "add_student",
                "args": [],
                "body": SAMPLE["source"],
                "filename": "app.py",
            }
        )
    )
    assert llm.validate_explanation_object(out["explanation"])


def test_module_batch_never_calls_the_llm():
    functions = [
        {
            "name": "fib",
            "args": ["n"],
            "body": (
                "def fib(n):\n"
                "    if n < 2:\n"
                "        return n\n"
                "    return fib(n - 1) + fib(n - 2)\n"
            ),
        }
    ]
    out = asyncio.run(llm.explain_module_batch("m.py", functions))
    assert llm.validate_explanation_object(out["functions"][0])
    assert "static AST analysis" in out["module_summary"]


def test_disabling_the_flag_restores_the_llm_path(monkeypatch):
    monkeypatch.setattr(config, "STATIC_EXPLANATIONS", False)
    with pytest.raises(AssertionError):
        asyncio.run(llm.analyze_functions_batch([SAMPLE]))
