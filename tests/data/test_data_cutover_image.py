"""Provisioning 72 (#298): cmp-cutover 이미지 빌드 스크립트. docker·git은 가짜로 바꿔 명령만 본다
(실제 빌드·push는 하지 않는다)."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra/data-migration/cutover/build-image.sh"


def run_script(
    tmp_path: Path, *args: str, dirty: bool = False
) -> tuple[subprocess.CompletedProcess, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    stubs = {
        "docker": f'echo "$*" >> {log}\n',
        "git": (
            'case "$*" in\n'
            '  *"rev-parse --show-toplevel"*) echo "' + str(ROOT) + '" ;;\n'
            '  *"status --porcelain"*) '
            + ('echo " M switchover.py"' if dirty else "true")
            + " ;;\n"
            '  *"log -1"*) echo 0123456789ab ;;\n'
            '  *) echo "unexpected git $*" >&2; exit 9 ;;\n'
            "esac\n"
        ),
    }
    for name, body in stubs.items():
        path = bin_dir / name
        path.write_text("#!/usr/bin/env bash\n" + body)
        path.chmod(0o755)
    result = subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    return result, log.read_text() if log.exists() else ""


def test_builds_amd64_locally_with_a_commit_tag_and_never_pushes(tmp_path) -> None:
    repo = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/cmp-platform-builds"
    result, docker = run_script(tmp_path, repo)
    assert result.returncode == 0, result.stderr
    assert "buildx build --platform linux/amd64 --load" in docker
    assert f"-t {repo}:cmp-cutover-0123456789ab" in docker
    assert docker.rstrip().endswith("infra/data-migration/cutover")
    assert "push" not in docker
    # push는 승인 뒤 사람이 실행한다: 명령과 설정에 넣을 이미지 값만 알려 준다
    assert f"docker push {repo}:cmp-cutover-0123456789ab" in result.stdout
    assert f"image={repo}:cmp-cutover-0123456789ab" in result.stdout


def test_refuses_uncommitted_cutover_files(tmp_path) -> None:
    result, docker = run_script(tmp_path, "cmp-cutover", dirty=True)
    assert result.returncode != 0 and "CUTOVER_SOURCE_DIRTY" in result.stderr
    assert docker == ""


def test_requires_a_repository(tmp_path) -> None:
    result, docker = run_script(tmp_path)
    assert result.returncode != 0 and docker == ""
