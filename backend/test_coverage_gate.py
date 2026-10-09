"""The placeholder gate must accept every suite that genuinely ran the target.

A suite can execute a function without literally spelling its name - most
obviously ``Class(...)``, which runs ``__init__``. Rejecting those produced
"coverage=100% but GENERATION_FAILURE" results.
"""
import main


def test_explicit_call_is_accepted():
    assert main._test_references_function("calc_price(1, 2.0, 0.0)", {"name": "calc_price"}) is True


def test_plain_constructor_call_counts_as_exercising_init():
    func = {"name": "__init__", "class_name": "InventoryItem"}
    code = "import m\n\n\ndef test_it():\n    item = m.InventoryItem(1, 'x', 1.0, 1)\n"
    assert main._test_references_function(code, func) is True


def test_unrelated_suite_is_still_rejected():
    func = {"name": "__init__", "class_name": "InventoryItem"}
    code = "import m\n\n\ndef test_it():\n    assert True\n"
    assert main._test_references_function(code, func) is False
