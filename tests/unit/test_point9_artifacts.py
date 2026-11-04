"""Guards for the deployment and manager cleanup requirements in point 9."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_deployment_scripts_and_systemd_unit_are_not_empty():
    artifacts = [*ROOT.joinpath("scripts").glob("*.sh"),
                 ROOT / "deploy" / "somnguard-device.service"]
    assert artifacts
    assert all(path.is_file() and path.stat().st_size > 0 for path in artifacts)


def test_device_manager_has_no_silent_except_pass_handlers():
    manager_path = ROOT / "app" / "device" / "manager.py"
    tree = ast.parse(manager_path.read_text(encoding="utf-8"))
    silent_handlers = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Pass)
    ]
    assert not silent_handlers


