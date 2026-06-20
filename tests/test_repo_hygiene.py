from pathlib import Path


def test_mn_api_does_not_import_blueprint_support_skill():
    root = Path(__file__).resolve().parents[1] / "mn_api"
    forbidden = ("mn_blueprint_support", "blueprint_support_skill", "mn-skills/blueprint_support_skill")
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(value in text for value in forbidden):
            offenders.append(str(path.relative_to(root.parent)))
    assert offenders == []
