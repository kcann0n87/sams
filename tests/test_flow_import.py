"""Smoke test: the flow module imports and its selector lists are well-formed."""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_sams_flow_imports():
    try:
        import playwright  # noqa: F401
    except Exception:
        print("SKIP: playwright not installed in this env")
        return
    mod = importlib.import_module("sams_automation.sams_flow")
    assert hasattr(mod, "SamsFlow")
    assert isinstance(mod.SamsFlow.EMAIL_CANDIDATES, list)
    assert isinstance(mod.SamsFlow.PASSWORD_CANDIDATES, list)
    assert all(isinstance(s, str) and s for s in mod.SamsFlow.EMAIL_CANDIDATES)


if __name__ == "__main__":
    test_sams_flow_imports()
    print("ok")
