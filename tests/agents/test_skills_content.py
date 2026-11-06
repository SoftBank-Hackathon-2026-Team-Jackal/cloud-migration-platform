from pathlib import Path

import pytest

SKILLS = Path(__file__).parent.parent.parent / "skills"
SECTIONS = [
    "## 언제 쓰는지",
    "## 조사 절차",
    "## 판단 기준",
    "## 대상별 차이",
    "## 수정 패턴",
    "## 테스트 패턴",
]
NAMES = ["k8s-workload", "db-postgres", "network", "config-env", "test-authoring"]


@pytest.mark.parametrize("name", NAMES)
def test_skill_has_all_sections_with_real_content(name):
    text = (SKILLS / name / "SKILL.md").read_text()
    for section in SECTIONS:
        assert section in text, f"{name} lacks {section}"
        body = text.split(section, 1)[1].split("\n## ", 1)[0].strip()
        assert len(body) > 20, f"{name}: {section} is empty"
    assert "골격" not in text


@pytest.mark.parametrize("name", NAMES)
def test_skill_has_a_worked_example_with_input_and_expected_output(name):
    files = sorted((SKILLS / name / "examples").glob("*.md"))
    assert files, f"{name} has no examples"
    body = "\n".join(f.read_text() for f in files)
    assert "## 입력" in body and "## 기대 출력" in body
