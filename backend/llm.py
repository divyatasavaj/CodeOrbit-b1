import os
import asyncio
import difflib
import json
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv
from google import genai

logger = logging.getLogger("codeoracle")

# Load .env reliably whether run from backend/ or project root
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)
else:
    load_dotenv()

logger = logging.getLogger("codeoracle")

api_key = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=api_key) if api_key else None
MODEL = "gemini-3.5-flash-lite"
FALLBACK_MODELS = ["gemini-3.5-flash-lite", "gemini-3.5-flash"]
SEM = asyncio.Semaphore(2)

# Quick-results mode: generate full LLM explanations for this fraction of each
# module's functions, then stop and fall back to the grounded AST engine for the
# remaining functions (time-optimized: 60%+ coverage instead of chasing 100%).
# Override via EXPLANATION_COVERAGE_TARGET env var (e.g. "0.8").
EXPLANATION_COVERAGE_TARGET = max(0.1, min(1.0, float(os.environ.get("EXPLANATION_COVERAGE_TARGET", "0.6"))))


def _extract_response_text(response: Any) -> str:
    """Safely extract generated text from Gemini API response object."""
    if response is None:
        return ""
    try:
        if hasattr(response, "text") and response.text is not None:
            return response.text.strip()
    except Exception:
        pass

    if hasattr(response, "candidates") and response.candidates:
        cand = response.candidates[0]
        if hasattr(cand, "content") and cand.content:
            parts = getattr(cand.content, "parts", [])
            text_parts = [p.text for p in parts if hasattr(p, "text") and p.text]
            if text_parts:
                return "".join(text_parts).strip()
    return ""


async def generate_with_retry(prompt: str, retries: int = 5, initial_delay: float = 1.5) -> str:
    """Execute Gemini generate_content with Semaphore, fallback models, and exponential backoff retry."""
    global client
    if not client:
        current_key = os.environ.get("GEMINI_API_KEY")
        if current_key:
            client = genai.Client(api_key=current_key)
        else:
            raise ValueError("GEMINI_API_KEY environment variable is not set")

    last_error = None
    for model_name in FALLBACK_MODELS:
        for attempt in range(retries):
            try:
                async with SEM:
                    response = await asyncio.to_thread(
                        client.models.generate_content,
                        model=model_name,
                        contents=prompt
                    )
                text = _extract_response_text(response)
                if text:
                    return text
                last_error = Exception("Gemini returned an empty response")
            except Exception as e:
                last_error = e
                err_str = str(e)
                if "404" in err_str or "NOT_FOUND" in err_str:
                    break
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    if attempt < retries - 1:
                        await asyncio.sleep(3.0 * (attempt + 1))
                    else:
                        break
                else:
                    if attempt < retries - 1:
                        await asyncio.sleep(initial_delay * (2 ** attempt))
                    else:
                        break

    raise last_error or Exception("Failed to generate content after retries")


BANNED_PHRASES = [
    "executes logic for",
    "maintains core operations for",
    "standard inputs and return values",
    "takes parameters as defined; returns computed operation result",
    "takes parameters as defined",
    "returns computed operation result",
    "no specific legacy risks identified",
    "no specific risks identified",
    "provides operational functionality for",
    "provides centralized",
    "processes incoming parameters and coordinates",
    "unanalyzed due to batch timeout",
    "unanalyzed due to",
    "standard execution",
    "standard input/output behavior",
    "standard inputs and outputs",
    "standard inputs",
    "returns computed result",
    "provides core operations for",
    "takes no parameters; returns none.",
    "standard parameters",
    "no specific risks"
]


def check_banned_phrases(text: str) -> List[str]:
    """Returns list of banned phrases found in text."""
    if not text:
        return []
    t_lower = text.lower().strip()
    return [b for b in BANNED_PHRASES if b in t_lower]


def _coerce_text(value: Any) -> str:
    """Normalize a model-generated field to a string; join lists/dicts so parsers never crash on `.strip()`."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "; ".join(str(v).strip() for v in value if str(v).strip())
    if isinstance(value, dict):
        return json.dumps(value)
    return str(value)


def validate_explanation_object(data: Dict[str, Any]) -> bool:
    """Validate that generated function explanation contains zero banned phrases, has distinct purpose/explanation, and includes usage."""
    if not isinstance(data, dict):
        return False
    exp = _coerce_text(data.get("explanation", ""))
    purp = _coerce_text(data.get("purpose", ""))
    usage = _coerce_text(data.get("usage", ""))
    io = _coerce_text(data.get("input_output") or data.get("inputOutput", ""))
    risks = _coerce_text(data.get("risks", ""))

    # Must have non-empty content in all 5 sections
    if not exp or not purp or not usage or not io or not risks:
        return False

    # Check for banned template phrases in every section
    for val in (exp, purp, usage, io, risks):
        if check_banned_phrases(val):
            return False

    # Check that explanation and purpose are not identical or near-duplicates
    if exp.lower() == purp.lower():
        return False
    if len(exp) > 20 and len(purp) > 20:
        words_exp = set(exp.lower().split())
        words_purp = set(purp.lower().split())
        overlap = len(words_exp & words_purp) / max(len(words_exp), len(words_purp))
        if overlap > 0.85:
            return False

    return True


def _text_similarity(a: str, b: str) -> float:
    """Return a 0.0-1.0 similarity ratio between two normalized strings."""
    a_norm = " ".join(a.lower().split())
    b_norm = " ".join(b.lower().split())
    if not a_norm or not b_norm:
        return 0.0
    return difflib.SequenceMatcher(None, a_norm, b_norm).ratio()


def _find_near_duplicate_risks(chunk_results: Dict[str, Dict[str, Any]], threshold: float = 0.8) -> List[str]:
    """Return names (in insertion order) whose Risks & Legacy text is identical or near-identical to an earlier function's in the same batch (similarity >= threshold)."""
    names = list(chunk_results.keys())
    flagged = []
    for i, a_name in enumerate(names):
        if a_name in flagged:
            continue
        a_risks = chunk_results[a_name].get("risks", "")
        for b_name in names[i + 1:]:
            b_risks = chunk_results[b_name].get("risks", "")
            if _text_similarity(a_risks, b_risks) >= threshold:
                flagged.append(b_name)
    return flagged


def analyze_function_ast(func: Dict[str, Any], filename: str = "") -> Dict[str, str]:
    """Code-grounded AST & regex static analysis that inspects actual syntax, parameters, body, input(), mutations, and risks."""
    name = func.get("display_name") or func.get("name", "unknown")
    raw_name = func.get("name", "func")
    args = func.get("args", [])
    body_str = func.get("body", "")

    # Clean parameter names (excluding self/cls)
    param_names = [a.split('=')[0].strip() for a in args if a.strip() and a.strip() not in ("self", "cls")]

    # Check for interactive input() calls
    import re
    input_matches = re.findall(r'input\s*\(\s*([\'"].*?[\'"])?\s*\)', body_str)
    has_input = len(input_matches) > 0 or "input(" in body_str

    # Extract input prompt descriptions
    input_prompts = []
    for p in input_matches:
        clean_p = p.strip("'\"").replace(":", "").replace("Enter", "").strip()
        if clean_p:
            input_prompts.append(clean_p)

    # Check for state mutation
    mutates_global = bool(re.search(r'\b(global\s+\w+|append|extend|insert|pop|remove|\.update\(|del\s+)', body_str))
    mutated_vars = re.findall(r'(\w+)\.(?:append|extend|insert|pop|remove|update)', body_str)
    mutated_var_name = mutated_vars[0] if mutated_vars else "shared dataset"

    # Check return statements
    has_return = "return " in body_str
    returns_bool = any(r in body_str for r in ("return True", "return False", "return bool("))
    returns_none = "return None" in body_str or not has_return
    return_values = re.findall(r'return\s+(.+)', body_str)
    first_return = return_values[0].strip() if return_values else "None"

    # Infer domain topic
    clean_name = name.replace("_", " ").replace(".", " ")
    name_lower = name.lower()
    if any(k in name_lower for k in ("student", "grade", "mark", "roll", "roster")):
        domain_topic = "student roster records"
    elif any(k in name_lower for k in ("item", "price", "stock", "csv", "discount", "order", "inventory")):
        domain_topic = "inventory and order catalog"
    elif any(k in name_lower for k in ("user", "auth", "login", "session", "token")):
        domain_topic = "user authentication state"
    else:
        domain_topic = f"{clean_name} data"

    # 1. Concrete Input / Output
    inferred_params = []
    sample_call_args = []
    for p in param_names:
        p_clean = p.replace('*', '')
        p_lower = p_clean.lower()
        if "manager" in p_lower:
            ptype = "Manager"
            sample_val = "manager_instance"
        elif any(k in p_lower for k in ("items", "list", "array", "data", "rows", "students", "records")):
            ptype = "list"
            sample_val = "[{'name': 'Alice', 'roll': 101}]"
        elif any(k in p_lower for k in ("config", "dict", "map", "params", "student", "record")):
            ptype = "dict"
            sample_val = "{'name': 'Alice', 'roll': 101}"
        elif any(k in p_lower for k in ("price", "rate", "cost", "total", "discount", "tax", "gpa", "average", "avg", "score")):
            ptype = "float"
            sample_val = "3.8" if "gpa" in p_lower else ("85.5" if "avg" in p_lower else "19.99")
        elif any(k in p_lower for k in ("name", "str", "text", "path", "file", "url", "msg", "currency", "supplier", "email", "grade")):
            ptype = "str"
            sample_val = "'Alice Smith'" if "name" in p_lower else ("'USD'" if "currency" in p_lower else ("'A'" if "grade" in p_lower else "'sample.txt'"))
        elif any(k in p_lower for k in ("is_", "has_", "flag", "active", "enabled")):
            ptype = "bool"
            sample_val = "True"
        elif any(k in p_lower for k in ("_id", "id_", "qty", "count", "num", "stock", "amount", "limit", "roll", "age", "year", "marks")):
            ptype = "int"
            sample_val = "101" if "roll" in p_lower else ("95" if "marks" in p_lower else "10")
        else:
            ptype = "Any"
            sample_val = "sample_value"

        inferred_params.append(f"`{p_clean}` ({ptype})")
        sample_call_args.append(f"{p_clean}={sample_val}")

    if has_input:
        prompt_desc = f" ({', '.join(input_prompts)})" if input_prompts else ""
        side_eff = f"; its real output is the side effect of appending a new record dict to the shared `{mutated_var_name}` list" if mutates_global else ""
        input_output = f"Takes no formal parameters — instead reads {', '.join(input_prompts) if input_prompts else 'input values'} interactively via `input()` at runtime. Returns `None`{side_eff}."
    elif inferred_params:
        if returns_bool:
            ret_str = "boolean status (`True`/`False`)"
        elif not returns_none and first_return != "None":
            ret_str = f"computed `{first_return}`"
        else:
            ret_str = "`None`"
        if mutates_global:
            ret_str += f"; mutates `{mutated_var_name}` as a side effect"
        input_output = f"Takes {', '.join(inferred_params)}. Returns {ret_str}."
    else:
        if returns_bool:
            ret_str = "boolean status (`True`/`False`)"
        elif not returns_none and first_return != "None":
            ret_str = f"`{first_return}`"
        else:
            ret_str = "`None`"
        if mutates_global:
            ret_str += f"; mutates `{mutated_var_name}` as a side effect"
        input_output = f"Takes no arguments. Returns {ret_str}."

    # 2. Practical Usage & Invocation
    call_str = f"{name}({', '.join(sample_call_args)})"
    if has_input:
        usage = f"{name}()  # Interactively prompts console for {', '.join(input_prompts) if input_prompts else 'user input'} and records entry"
    elif returns_bool:
        usage = f"if {call_str}:  # Evaluates {clean_name} and returns True on success"
    elif not returns_none and first_return != "None":
        usage = f"result = {call_str}  # Computes and returns {first_return} from provided arguments"
    else:
        usage = f"{call_str}  # Executes {clean_name} routine across runtime dataset"

    # 3. Purpose (Why it exists / architectural problem solved)
    if has_input and mutates_global:
        purpose = f"Provides the primary entry point for growing the {domain_topic} — without it, no new entries could be added to the dataset once the program starts, since there is no other insertion path into `{mutated_var_name}`."
    elif any(k in name_lower for k in ("version", "config", "status", "info", "health")):
        purpose = f"Supplies the active {clean_name} constant or diagnostic state for system identification and compatibility checks."
    elif "grade" in name_lower or "score" in name_lower or "eval" in name_lower:
        purpose = f"Determines the categorical rating or score evaluation corresponding to numeric metrics, ensuring standardized classification rules across reporting."
    elif any(k in name_lower for k in ("calc", "compute", "sum", "average", "avg", "total")):
        purpose = f"Encapsulates the mathematical calculation for {clean_name}, ensuring accurate metric evaluation across the module without duplicate logic."
    elif any(k in name_lower for k in ("find", "search", "get", "lookup")):
        purpose = f"Provides direct querying capabilities to retrieve individual {domain_topic} entries without requiring callers to manually iterate over the storage collection."
    elif any(k in name_lower for k in ("display", "print", "show", "view", "menu", "list")):
        purpose = f"Renders formatted console output for {domain_topic} to present current state and interaction options directly to the user."
    elif any(k in name_lower for k in ("save", "load", "read", "write", "export", "import")):
        purpose = f"Handles persistent storage I/O by synchronizing {domain_topic} between runtime memory and local disk files."
    elif not param_names and not has_return:
        purpose = f"Initializes and updates internal {clean_name} state variables to coordinate multi-step workflows in `{filename or 'the module'}`."
    else:
        purpose = f"Encapsulates {clean_name} logic to isolate state transitions and support {domain_topic} workflows across `{filename or 'the module'}`."

    # 4. Function Explanation (Step-by-step mechanics for non-technical reader)
    if has_input:
        explanation = f"Asks the user to type in {', '.join(input_prompts) if input_prompts else 'the required data'} at the console, builds a record from the answers, and saves it into the active dataset."
    elif inferred_params:
        explanation = f"Takes {', '.join(param_names)} as inputs, works through the logic, and produces the {clean_name} result."
    else:
        explanation = f"Runs the internal {clean_name} routine and updates the related application state."

    # 5. Risks & Legacy Code Patterns
    risks_found = []
    if re.search(r'int\s*\(\s*input\s*\(', body_str) or re.search(r'float\s*\(\s*input\s*\(', body_str):
        risks_found.append("Calls `input()` with direct numeric casting (`int()`/`float()`) without `try...except`, causing unhandled `ValueError` crashes on non-numeric input")
    elif has_input:
        risks_found.append("Reads console input via `input()` without validating for empty strings or trimming whitespace, allowing malformed records to be appended")

    dict_access_match = re.search(r'(\w+)\[\s*([\'"]\w+[\'"])\s*\]', body_str)
    if dict_access_match and "get(" not in body_str and "try:" not in body_str:
        risks_found.append(f"Direct dictionary indexing (`{dict_access_match.group(1)}[{dict_access_match.group(2)}]`) without key existence check (`.get()` or `in`), risking unhandled `KeyError`")

    if "!= None" in body_str:
        risks_found.append("Uses non-idiomatic `!= None` comparison instead of `is not None`")
    if "== None" in body_str:
        risks_found.append("Uses non-idiomatic `== None` comparison instead of `is None`")
    if "except:" in body_str:
        risks_found.append("Contains bare `except:` clause that catches all exceptions indiscriminately and masks unexpected errors")
    if "print(" in body_str and not any(k in name_lower for k in ("print", "display", "show", "menu")):
        print_args = re.findall(r'print\s*\(\s*([^)\n]+)', body_str)
        print_sample = print_args[0].strip()[:40] if print_args else "the output"
        risks_found.append(f"Relies on `print()` side effects (e.g. prints `{print_sample}`) instead of returning structured data or using logging")
    if "open(" in body_str and "with open" not in body_str:
        risks_found.append("Opens files without a `with` context manager, risking unclosed file descriptors on error")
    if mutates_global:
        risks_found.append(f"Directly mutates shared global collection (`{mutated_var_name}`) without duplicate checking or concurrency protection")
    pct_fmt_match = re.search(r'(["\'])([^"\'\n]+)\1\s*%', body_str)
    if "%" in body_str and "%s" in body_str:
        pct_sample = f" like `{pct_fmt_match.group(0)[:30]}`" if pct_fmt_match else ""
        risks_found.append(f"Uses legacy Python 2 `%` string interpolation{pct_sample} instead of modern f-strings")

    # Checklist-driven detections grounded in this function's own source (additive, conservative)
    hardcoded_paths = re.findall(r'[\'"](?:(?:\.{1,2}[/\\\\]|[A-Za-z]:[/\\\\]|/tmp/|/var/|/home/|/usr/|~/)[^\'"]*)[\'"]', body_str)
    if hardcoded_paths:
        risks_found.append(f"Hardcodes the file path `{hardcoded_paths[0]}` in source instead of taking it as a parameter or config value")
    hardcoded_cred = re.search(r'(?i)[\'"](?:password|passwd|secret|api[_-]?key|token)[\'"]\s*=\s*[\'"][^\'"]{3,}[\'"]', body_str)
    if hardcoded_cred:
        risks_found.append("Hardcodes a credential value in source instead of injecting it via configuration or environment variables")
    if re.search(r'\bwhile\s+True\s*:', body_str) and "break" not in body_str:
        risks_found.append("Contains a `while True:` loop with no visible `break`, risking an unbounded loop")
    body_no_docstring = re.sub(r'^("""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\')', '', body_str.lstrip()) if body_str.strip().startswith(('"""', "'''")) else body_str
    _body_lines = body_no_docstring.split('\n')
    if _body_lines and re.match(r'^(?:async\s+)?def\s', _body_lines[0]):
        _body_lines = _body_lines[1:]
    body_no_def = "\n".join(_body_lines)
    if re.search(r'\b' + re.escape(raw_name) + r'\s*\(', body_no_def):
        risks_found.append(f"Calls itself recursively (`{raw_name}`) — verify it has a guaranteed base case or exit condition")

    # Missing return on some code paths (implicit None inconsistency) — Python-only AST check
    try:
        import ast as _ast
        _tree = _ast.parse(body_str)
        _module_body = _tree.body
        if len(_module_body) == 1 and isinstance(_module_body[0], _ast.FunctionDef):
            _last = _module_body[0].body[-1] if _module_body[0].body else None
        else:
            _last = _module_body[-1] if _module_body else None

        def _ends_in_return(stmt):
            if stmt is None:
                return False
            if isinstance(stmt, _ast.Return):
                return True
            if isinstance(stmt, _ast.If):
                body_ok = _ends_in_return(stmt.body[-1]) if stmt.body else False
                orelse_ok = _ends_in_return(stmt.orelse[-1]) if stmt.orelse else False
                return body_ok and orelse_ok
            if isinstance(stmt, _ast.Try):
                if stmt.finalbody:
                    return _ends_in_return(stmt.finalbody[-1])
                blocks = [b for b in [stmt.body, stmt.orelse] if b]
                blocks += [h.body for h in stmt.handlers]
                return all(_ends_in_return(b[-1]) for b in blocks if b)
            return False

        if has_return and return_values and not _ends_in_return(_last):
            risks_found.append("Returns a value on some code paths but can fall through to an implicit `None` at the end of the function on other paths")
    except Exception:
        pass

    # Generic / unclear parameter names that could cause misuse by other developers
    generic_param_names = [p for p in param_names if len(p) == 1 or p.lower() in ("data", "info", "temp", "val", "value", "stuff", "flag", "obj", "arg", "args")]
    if generic_param_names:
        risks_found.append(f"Uses generic parameter name(s) `{', '.join(generic_param_names)}` that don't describe their role, inviting misuse by other developers")

    non_empty_lines = [l for l in body_str.split('\n') if l.strip() and not l.strip().startswith('#')]
    has_branching = bool(re.search(r'\b(if|elif|else|for|while)\b', body_str))
    if risks_found:
        risks = "; ".join(risks_found) + "."
    elif len(non_empty_lines) <= 5 and not mutates_global and not has_input and not has_branching:
        risks = "Minimal operational risk as a pure deterministic helper without I/O, shared state mutation, or branching."
    else:
        risks = f"Takes `{', '.join(param_names) if param_names else 'its inputs'}` without defensive boundary validation or explicit type checks, so malformed inputs are only caught by whatever the runtime does by default."

    return {
        "name": name,
        "explanation": explanation,
        "usage": usage,
        "purpose": purpose,
        "input_output": input_output,
        "risks": risks
    }


CONTRASTIVE_EXPLANATION_GUIDE = """
=============================================================================
CRITICAL INSTRUCTION: ZERO GENERIC FILLER — CONCRETE CONTRASTIVE TARGETS
=============================================================================
Every section must be derived directly from the function's ACTUAL source code, parameters, input() calls, and logic.
Never produce name-templated generic sentences. Look at these exact BAD (BANNED) vs GOOD (TARGET QUALITY) examples for a function like `add_student()`:

• Purpose:
  - BAD (BANNED FILLER): "Maintains core operations for add student."
  - GOOD (TARGET QUALITY): "Provides the primary entry point for growing the student roster — without it, no new students could be added to the dataset once the program starts, since there is no other insertion path into the shared records list."

• Input / Output:
  - BAD (BANNED FILLER): "Takes parameters as defined; returns computed operation result."
  - GOOD (TARGET QUALITY): "Takes no formal parameters — instead reads student name and roll number interactively via input() at runtime. Returns None; its real output is the side effect of appending a new record dict to the shared students list."

• Risks & Legacy:
  - BAD (BANNED FILLER): "No specific risks identified."
  - GOOD (TARGET QUALITY): "Uses input() without validating that roll number is numeric or non-duplicate, so malformed or duplicate entries can silently corrupt the dataset. No try/except around the input calls means a bad input could crash the program instead of prompting again."

=============================================================================
ADAPTIVE LENGTH RULE
=============================================================================
- Length should scale with what is actually true about the function:
  • Trivial functions (e.g. 1-line getter/setter or pure helper) -> 1 short, code-grounded sentence per section. Do NOT pad.
  • Complex functions (multiple side effects, input() validation risks, state mutations, non-obvious architecture) -> 2-4 specific sentences per section.
- Content must ALWAYS name real symbols, parameters, variables, return types, or line behaviors. Never generic filler.

=====================================================================
RISK ANALYSIS CHECKLIST — SCAN EVERY FUNCTION INDEPENDENTLY
=====================================================================
For EACH function, run this exact checklist against THAT function's OWN source code. Only cite a risk if the pattern is actually present in that code — never infer, guess, or recycle a sibling function's findings, even when the functions look structurally similar. For every risk you list, name the actual evidence: the specific variable, key, index, parameter, line, or statement from the code.
  1. `input()` calls without type / format / duplicate validation → name exactly what is unvalidated (e.g. "roll number is never checked to be numeric or unique").
  2. Dict/list access or indexing without an existence check → name the specific key/index at risk (e.g. "`student['name']` raises `KeyError` if the key is absent").
  3. Bare `except:`, no exception handling, or exceptions swallowed silently (caught and ignored).
  4. Mutation of global/shared state (appending to a shared list, modifying a module-level dict) → name the specific variable being mutated.
  5. Hardcoded values that should be configurable (file paths, credentials, magic numbers).
  6. Missing `return` on some code paths (implicit `None` in some branches, an explicit value in others).
  7. Unbounded loops or recursion without a clear exit condition.
  8. Naming/clarity issues that could cause misuse by other developers.

INDEPENDENT WORDING RULE: analyze each function in complete isolation. Never reuse, copy, or paraphrase the Risks & Legacy wording of another function in the same batch — even when two functions genuinely share the same risk category (for example, both read `input()` without validation). When the underlying category matches, still write a fresh sentence that cites THIS function's own variable names and code context. Only "No specific risks identified" (or equivalent) is acceptable for a genuinely trivial function (~5 lines, no I/O, no shared state, no branching) — and that must stay a rare, justified exception, never a fallback.

=====================================================================
PLAIN-LANGUAGE EXPLANATION STYLE
=====================================================================
Write `explanation` for a non-technical reader in plain, everyday language. Prefer simple verbs — creates, builds, calls, checks, adds, reads, saves, returns, runs — over stiff technical verbs such as constructs, instantiates, invokes, executes, receives, processes. When the function does several distinct things, it is fine to use two short sentences instead of one dense sentence: first what triggers or starts the function, then what it produces or changes (e.g. "Adds a new student to the class list."). Keep every claim grounded in the real function body — the goal is clarity of language over compressed technical phrasing, not loss of precision.
"""



async def _call_llm_split(prompts: list, model: Optional[str] = None) -> list:
    """Call LLM with prompts split across Groq and Gemini concurrently."""
    try:
        router = get_llm_provider()
        return await router.generate_split(prompts, model)
    except Exception as e:
        logger.warning(f"Split provider call failed: {e}")
        # Fallback to sequential single calls
        results = []
        for p in prompts:
            try:
                result = await generate_with_retry(p)
                results.append(result)
            except Exception as e:
                results.append(f"Error: {e}")
        return results


async def explain_module_batch(filename: str, functions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Generate module summary AND distinct, code-grounded function breakdowns in Gemini requests with contrastive targets, validation & AST fallback."""
    if not functions:
        return {
            "module_summary": f"Module {filename} contains no functions to analyze.",
            "functions": []
        }

    batch_size = 15  # FIX 3: Increased from 3 to 15 for better token utilization
    all_func_results = []
    module_summary = f"Module {filename} provides core functionality and components."

    total_funcs = len(functions)
    coverage_target = max(1, (total_funcs * int(EXPLANATION_COVERAGE_TARGET * 100) + 99) // 100)
    covered_count = 0

    for chunk_start in range(0, len(functions), batch_size):
        chunk = functions[chunk_start:chunk_start + batch_size]
        func_entries = []
        for f in chunk:
            name = f.get("display_name") or f.get("name", "unknown")
            args = f.get("args", [])
            args_str = ", ".join(args) if args else "no parameters"
            body = f.get("body", "").strip()
            if not body:
                body = f"def {f.get('name', 'func')}({args_str}):\n    pass"
            func_entries.append(f"Function Name: {name}\nParameters: {args_str}\nFull Source Code:\n```python\n{body}\n```")

        func_list = "\n\n---\n\n".join(func_entries)

        base_prompt = f"""You are a principal software engineer documenting code in module `{filename}`.

{CONTRASTIVE_EXPLANATION_GUIDE}

Functions in `{filename}` to analyze:
{func_list}

Produce a JSON response with:
1. `module_summary`: 2-3 sentences describing the module's architectural role.
2. `functions`: List of objects for EACH function with 5 DISTINCT fields:
- `name`: Exact function name as provided.
- `explanation`: 1-2 plain-English sentences in plain, everyday language for a non-technical reader (avoid stiff verbs like constructs/instantiates/invokes/executes — prefer creates/builds/calls/checks/adds/saves). Describe what triggers or starts the function, then what it produces or changes. Must NOT be identical or similar to Purpose.
- `usage`: Single-line realistic code invocation with plausible arguments and a trailing Python comment explaining the exact outcome of that specific call. NEVER use "..." or placeholders.
- `purpose`: 1-3 sentences explaining WHY this function exists and what problem it solves in context. Must NOT be a duplicate or paraphrase of Explanation.
- `input_output`: Concrete parameter names and inferred types, plus return type and what it represents (or if it uses `input()`, state that it reads console input and mutates shared state).
- `risks`: Findings from the Risk Analysis Checklist above, grounded ONLY in THIS function's source — each risk must name the actual variable, key, index, parameter, or statement from this function's code. Never reuse wording from another function in the batch, even for the same risk category. Only "No specific risks identified" (or equivalent) is acceptable for a genuinely trivial function (~5 lines, no I/O, no shared state, no branching) and must remain a rare exception.

Analyze each function independently — never copy, reuse, or rephrase another function's Risks & Legacy text, even when they share the same risk category.

Respond ONLY with valid JSON (no markdown fences):
{{
  "module_summary": "...",
  "functions": [
    {{
      "name": "...",
      "explanation": "...",
      "usage": "...",
      "purpose": "...",
      "input_output": "...",
      "risks": "..."
    }}
  ]
}}"""

        chunk_results: Dict[str, Dict[str, Any]] = {}
        max_attempts = 2
        current_prompt = base_prompt

        for attempt in range(max_attempts):
            try:
                response_text = await generate_with_retry(current_prompt)
                if response_text.startswith("```json"):
                    response_text = response_text[7:]
                elif response_text.startswith("```"):
                    response_text = response_text[3:]
                if response_text.endswith("```"):
                    response_text = response_text[:-3]

                import json as json_mod
                data = json_mod.loads(response_text.strip())
                if chunk_start == 0 and data.get("module_summary"):
                    module_summary = data.get("module_summary")

                func_exps = data.get("functions", [])
                invalid_funcs = []

                for f in chunk:
                    name = f.get("display_name") or f.get("name", "unknown")
                    matched = None
                    for item in func_exps:
                        if item.get("name") == name or item.get("name") == f.get("name"):
                            matched = item
                            break

                    if matched and validate_explanation_object(matched):
                        chunk_results[name] = {
                            "name": name,
                            "explanation": _coerce_text(matched.get("explanation", "")),
                            "usage": _coerce_text(matched.get("usage", "")),
                            "purpose": _coerce_text(matched.get("purpose", "")),
                            "input_output": _coerce_text(matched.get("input_output") or matched.get("inputOutput", "")),
                            "risks": _coerce_text(matched.get("risks", ""))
                        }
                    else:
                        invalid_funcs.append((f, name, matched, None))

                # Cross-function Risks & Legacy similarity check: flag near-duplicate text for independent regeneration
                near_dup_names = _find_near_duplicate_risks(chunk_results)
                if near_dup_names:
                    name_to_func = {f.get("display_name") or f.get("name", "unknown"): f for f in chunk}
                    for nm in near_dup_names:
                        invalid_funcs.append((name_to_func[nm], nm, chunk_results[nm], "similarity"))
                        del chunk_results[nm]

                if not invalid_funcs:
                    break

                if covered_count + len(chunk_results) >= coverage_target:
                    logger.info(
                        f"Reached {min(covered_count + len(chunk_results), total_funcs)}/{total_funcs} "
                        f"function explanation coverage in `{filename}`; stopping to optimize time "
                        f"(target {EXPLANATION_COVERAGE_TARGET * 100:.0f}%)"
                    )
                    break

                if attempt < max_attempts - 1:
                    logger.info(f"Retrying explanation generation for {len(invalid_funcs)} functions in {filename} (attempt {attempt + 1})")
                    feedback_items = []
                    for f, name, matched, reason in invalid_funcs:
                        body_code = f.get("body", "")
                        if reason == "similarity":
                            feedback_items.append(f"- Function `{name}`: Your Risks & Legacy text is too similar to another function in the same batch. Do not reuse wording from other functions — analyze this function's own code independently and name the actual variables and patterns from ITS source:\n```python\n{body_code}\n```")
                        else:
                            feedback_items.append(f"- Function `{name}`: Your previous answer contained generic template filler or failed validation. Be specific about the actual code:\n```python\n{body_code}\n```")
                    
                    current_prompt = f"""{base_prompt}

CRITICAL RETRY FEEDBACK (Attempt {attempt + 2}):
Your previous output contained generic template filler or invalid sections for the following functions:
{chr(10).join(feedback_items)}

DO NOT USE ANY BANNED PHRASES. Ensure Purpose, Input/Output, and Risks are 100% specific to the provided source code! Each Risks & Legacy entry must be written independently for its own function — never copy or rephrase another function's risks text, even when they share the same risk category."""

            except Exception as e:
                logger.warning(f"Batch explanation attempt {attempt + 1} failed for {filename}: {e}")
                if attempt == max_attempts - 1:
                    break

        # For any functions that didn't pass or were missing, use our deep AST analysis engine
        for f in chunk:
            name = f.get("display_name") or f.get("name", "unknown")
            if name in chunk_results:
                all_func_results.append(chunk_results[name])
                covered_count += 1
            else:
                logger.info(f"Using grounded AST engine analysis for `{name}` in `{filename}`")
                all_func_results.append(analyze_function_ast(f, filename))

        if covered_count >= coverage_target:
            logger.info(
                f"Explanation coverage target reached for `{filename}` "
                f"({covered_count}/{total_funcs} functions); skipping remaining chunks and "
                f"filling the rest with grounded AST analysis (time optimization)"
            )
            for f in functions[len(all_func_results):]:
                name = f.get("display_name") or f.get("name", "unknown")
                logger.info(f"Using grounded AST engine analysis for `{name}` in `{filename}` (coverage target reached)")
                all_func_results.append(analyze_function_ast(f, filename))
            break

    return {
        "module_summary": module_summary,
        "functions": all_func_results
    }


async def explain_function(func: Dict[str, Any]) -> Dict[str, Any]:
    """Explain a single function with high-depth, distinct sections using contrastive targets."""
    func_name = func.get("display_name") or func.get("name", "unknown")
    raw_name = func.get("name", "unknown")
    args = func.get("args", [])
    args_str = ", ".join(args) if args else "no parameters"
    body = func.get("body", "").strip()
    if not body:
        body = f"def {raw_name}({args_str}):\n    pass"

    prompt = f"""You are a principal software engineer documenting a legacy function.

{CONTRASTIVE_EXPLANATION_GUIDE}

Analyze this function carefully:
Name: {func_name}
Parameters: {args_str}
Source code:
```python
{body}
```

Ground your analysis strictly in the actual source code. DO NOT use generic filler phrases and DO NOT duplicate text across sections.

Provide a JSON response with these 5 DISTINCT fields:
- `explanation`: 1-2 plain-English sentences in plain, everyday language for a non-technical reader (avoid stiff verbs like constructs/instantiates/invokes/executes — prefer creates/builds/calls/checks/adds/saves). Describe what triggers or starts the function, then what it produces or changes.
- `usage`: A realistic single-line code invocation using plausible argument values based on parameter types, with a trailing comment describing the exact outcome of that specific call (e.g. `{func_name}(...)  # Outcome with these values`). Do NOT use "..." or placeholders.
- `purpose`: 1-3 sentences explaining WHY this function exists and what problem it solves in context.
- `input_output`: Concrete parameter names and inferred types, plus return type and what it represents (or if it uses `input()`, state that it reads console input and mutates shared state).
- `risks`: Findings from the Risk Analysis Checklist above, grounded ONLY in this function's source — name the actual variable, key, index, parameter, or statement for each risk. Only "No specific risks identified" (or equivalent) is acceptable for a genuinely trivial function (~5 lines, no I/O, no shared state, no branching) and must remain a rare exception.

Return ONLY valid JSON (no markdown fences):
{{
  "name": "{func_name}",
  "explanation": "...",
  "usage": "...",
  "purpose": "...",
  "input_output": "...",
  "risks": "..."
}}"""

    explanation_data = None
    for attempt in range(2):
        try:
            response_text = await generate_with_retry(prompt)
            if response_text.startswith("```json"):
                response_text = response_text[7:]
            elif response_text.startswith("```"):
                response_text = response_text[3:]
            if response_text.endswith("```"):
                response_text = response_text[:-3]

            import json as json_mod
            data = json_mod.loads(response_text.strip())
            candidate = {
                "name": func_name,
                "explanation": _coerce_text(data.get("explanation", "")),
                "usage": _coerce_text(data.get("usage", "")),
                "purpose": _coerce_text(data.get("purpose", "")),
                "input_output": _coerce_text(data.get("input_output") or data.get("inputOutput", "")),
                "risks": _coerce_text(data.get("risks", ""))
            }
            if validate_explanation_object(candidate):
                explanation_data = candidate
                break
        except Exception:
            pass

    if not explanation_data:
        explanation_data = analyze_function_ast(func, func.get("filename", ""))

    return {
        "name": func_name,
        "raw_name": raw_name,
        "class_name": func.get("class_name"),
        "filename": func.get("filename", ""),
        "explanation": explanation_data
    }


async def explain_module(filename: str, function_explanations: List[str]) -> str:
    """Generate a 3-sentence module summary from function explanations."""
    valid_exps = [e for e in function_explanations if e and not e.startswith("Error generating explanation")]
    if not valid_exps:
        if not function_explanations:
            return f"Module {filename} has no functions to analyze."
        valid_exps = function_explanations

    combined = "\n".join(valid_exps)
    prompt = f"""Based on these function explanations from {filename}, provide a 3-sentence module summary:

{combined}

Return only 3 sentences, no extra formatting."""

    try:
        return await generate_with_retry(prompt)
    except Exception as e:
        return f"Error generating module summary: {str(e)}"


async def generate_tests_batch(functions: List[Dict[str, Any]], source_code: str, source_file: str = "") -> str:
    """Generate tests for ALL functions in a file in a SINGLE Gemini request. Supports Python (pytest) and JS (node:test)."""
    if not functions:
        return "# No testable functions found"

    is_js = source_file.endswith(('.js', '.ts', '.jsx', '.tsx'))
    source_basename = os.path.basename(source_file)
    source_module = os.path.splitext(source_basename)[0]

    func_names = [f.get("display_name") or f.get("name", "unknown") for f in functions]
    func_list = "\n".join([f"- {name}({', '.join(f.get('args', []))})" for name, f in zip(func_names, functions)])

    if is_js:
        prompt = f"""Generate comprehensive Node.js unit tests using `node:test` and `node:assert` for these JavaScript functions.

Functions to test:
{func_list}

Source Code:
```javascript
{source_code[:4000]}
```

REQUIREMENTS:
1. Import test and assert:
const test = require('node:test');
const assert = require('node:assert');
const src = require('./{source_basename}');
2. Test ALL listed functions with meaningful assertions (normal cases, edge cases, error cases).
3. Call functions as `src.funcName(...)` or destructured imported functions.
4. Return ONLY valid runnable JavaScript code. No markdown fences."""
    else:
        prompt = f"""Generate comprehensive pytest unit tests for the following functions from a Python module.

Available functions to test:
{func_list}

Source code of the module:
```python
{source_code[:4000]}
```

CRITICAL REQUIREMENTS:
1. Import the source module using: import {source_module}
2. The source file is named "{source_file}" so the module name is "{source_module}"
3. Test ALL listed functions with meaningful test cases
4. For functions requiring input parameters, provide realistic test data
5. For functions with side effects (print, file I/O), use unittest.mock to mock them
6. Include normal cases, edge cases, and error cases
7. Do NOT test main() functions or interactive entry points
8. Use pytest fixtures where appropriate
9. Handle global state safely by patching if needed
10. Each test function must have a unique name (use test_<function_name>_<scenario>)

Return ONLY the Python test code. No explanation. No markdown fences.
The test file should be complete and runnable with `pytest`."""

    try:
        test_code = await generate_with_retry(prompt)
        for fence in ("```javascript", "```js", "```python", "```"):
            if test_code.startswith(fence):
                test_code = test_code[len(fence):].strip()
        if test_code.endswith("```"):
            test_code = test_code[:-3].strip()
        return test_code
    except Exception as e:
        return f"# Error generating tests: {str(e)}"


async def generate_tests(func: Dict[str, Any]) -> str:
    """Generate pytest unit tests for a single function (used for individual retry)."""
    raw_name = func.get("name", "func")
    args = func.get("args", [])
    args_str = ", ".join(args) if args else ""
    body = func.get("body", "").strip()
    if not body:
        body = f"def {raw_name}({args_str}):\n    pass"

    prompt = f"""Generate pytest unit tests for this function. Include:
- Normal case test
- Edge case (None, zero, empty where relevant)
- Exception/error case if applicable

Return ONLY the Python test code. No explanation. No markdown fences.

Function:
```python
{body}
```"""

    try:
        test_code = await generate_with_retry(prompt)
        if test_code.startswith("```python"):
            test_code = test_code[len("```python"):].strip()
        elif test_code.startswith("```"):
            test_code = test_code[3:].strip()
        if test_code.endswith("```"):
            test_code = test_code[:-3].strip()
        return test_code
    except Exception as e:
        return f"# Error generating tests: {str(e)}"


async def generate_tests_for_coverage(func: Dict[str, Any], uncovered_lines: str) -> str:
    """Generate improved tests targeting uncovered lines."""
    body = func.get('body', '').strip()
    if not body:
        body = f"def {func.get('name', 'func')}():\n    pass"

    prompt = f"""Generate pytest unit tests for this function, focusing on covering these uncovered lines/branches:

Uncovered areas: {uncovered_lines}

Function:
```python
{body}
```

Include tests that specifically target the uncovered paths. Return ONLY Python test code. No explanation. No markdown fences."""

    try:
        test_code = await generate_with_retry(prompt)
        if test_code.startswith("```python"):
            test_code = test_code[len("```python"):].strip()
        elif test_code.startswith("```"):
            test_code = test_code[3:].strip()
        if test_code.endswith("```"):
            test_code = test_code[:-3].strip()
        return test_code
    except Exception as e:
        return f"# Error generating coverage tests: {str(e)}"


async def refactor_function(func: Dict[str, Any]) -> Dict[str, Any]:
    """Refactor a function and return refactored code with breaking changes."""
    func_name = func.get("display_name") or func.get("name", "unknown")
    raw_name = func.get("name", "func")
    body = func.get("body", "").strip()
    if not body:
        body = f"def {raw_name}():\n    pass"

    prompt = f"""Refactor this Python function to modern Python 3 standards.
Fix legacy patterns like bare except, != None checks, old string formatting, etc.

Return EXACTLY this format and no other text:

```python
<refactored code>
```

```json
[{{"change": "...", "risk": "high|medium|low", "why": "..."}}]
```

Function:
```python
{body}
```"""

    try:
        content = await generate_with_retry(prompt)

        refactored_code = ""
        breaking_changes = []

        if "```python" in content and "```json" in content:
            parts = content.split("```python")
            if len(parts) > 1:
                code_part = parts[1].split("```")[0].strip()
                refactored_code = code_part

            json_part = content.split("```json")[1].split("```")[0].strip()
            try:
                breaking_changes = json.loads(json_part)
            except json.JSONDecodeError:
                breaking_changes = [{"change": "Could not parse JSON", "risk": "unknown", "why": ""}]
        else:
            refactored_code = content
            breaking_changes = [{"change": "Could not parse response format", "risk": "unknown", "why": ""}]

    except Exception as e:
        refactored_code = f"# Error during refactor: {str(e)}"
        breaking_changes = [{"change": "Could not parse", "risk": "unknown", "why": str(e)}]

    return {
        "name": func_name,
        "refactored_code": refactored_code,
        "breaking_changes": breaking_changes
    }


async def refactor_batch(functions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Refactor multiple functions in a SINGLE Gemini request to reduce API calls."""
    if not functions:
        return []

    func_data = []
    for f in functions:
        body = f.get("body", "").strip()[:400]
        func_data.append({
            "name": f.get("display_name") or f.get("name", "unknown"),
            "raw_name": f.get("name", "func"),
            "body": body
        })

    funcs_json = json.dumps(func_data, indent=2)

    prompt = f"""Refactor these Python functions to modern Python 3 standards.
Fix legacy patterns like bare except, != None checks, old string formatting, etc.

Functions to refactor:
{funcs_json}

Return your response in this EXACT JSON format (no markdown fences):
{{
  "refactored": [
    {{
      "name": "function_name",
      "code": "refactored code here",
      "changes": [{{"change": "...", "risk": "high|medium|low", "why": "..."}}]
    }},
    ...
  ]
}}"""

    try:
        response_text = await generate_with_retry(prompt)

        if response_text.startswith("```json"):
            response_text = response_text[7:]
        elif response_text.startswith("```"):
            response_text = response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]

        data = json.loads(response_text.strip())
        refactored_list = data.get("refactored", [])

        results = []
        for f in functions:
            name = f.get("display_name") or f.get("name", "unknown")
            found = False
            for r in refactored_list:
                if r.get("name") == name:
                    results.append({
                        "name": name,
                        "refactored_code": r.get("code", ""),
                        "breaking_changes": r.get("changes", [])
                    })
                    found = True
                    break
            if not found:
                results.append({
                    "name": name,
                    "refactored_code": f.get("body", ""),
                    "breaking_changes": []
                })

        return results
    except Exception as e:
        logger.warning(f"Batch refactor failed: {e}")
        return [{"name": f.get("display_name") or f.get("name", "unknown"), "refactored_code": f.get("body", ""), "breaking_changes": []} for f in functions]


async def process_all_functions(functions: List[Dict[str, Any]], task: str) -> List[Any]:
    """Process all functions concurrently for a given task."""
    if task == "explain":
        tasks = [explain_function(f) for f in functions]
    elif task == "tests":
        tasks = [generate_tests(f) for f in functions]
    elif task == "refactor":
        tasks = [refactor_function(f) for f in functions]
    else:
        raise ValueError(f"Unknown task: {task}")

    results = await asyncio.gather(*tasks, return_exceptions=True)

    processed = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            func_name = functions[i].get("display_name") or functions[i].get("name", "unknown")
            if task == "explain":
                processed.append({
                    "name": func_name,
                    "raw_name": functions[i].get("name", "unknown"),
                    "class_name": functions[i].get("class_name"),
                    "filename": functions[i].get("filename", ""),
                    "explanation": f"Error: {str(result)}"
                })
            elif task == "tests":
                processed.append(f"# Error: {str(result)}")
            elif task == "refactor":
                processed.append({
                    "name": func_name,
                    "refactored_code": f"# Error: {str(result)}",
                    "breaking_changes": []
                })
        else:
            processed.append(result)

    return processed
