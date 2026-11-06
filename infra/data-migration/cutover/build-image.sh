#!/usr/bin/env bash
# cmp-cutover 이미지(PgBouncer + 전환 실행기)를 원본 EKS 노드용 linux/amd64로 로컬에 빌드한다
# (Provisioning 72, #298). push하지 않는다: push는 승인 뒤 사람이 아래 출력 명령으로 한다.
#
#   bash infra/data-migration/cutover/build-image.sh <REPOSITORY>
#
# 태그 규칙: <REPOSITORY>:cmp-cutover-<이 디렉터리를 마지막으로 바꾼 커밋 12자리>.
# 같은 커밋이면 같은 태그이고, 커밋하지 않은 변경이 있으면 빌드하지 않는다(CUTOVER_SOURCE_DIRTY).
set -euo pipefail

repo="${1:-}"
if [[ -z "$repo" ]]; then
  echo "usage: build-image.sh <REPOSITORY>   (예: <account>.dkr.ecr.<region>.amazonaws.com/cmp-platform-builds)" >&2
  exit 2
fi

root="$(git rev-parse --show-toplevel)"
dir="infra/data-migration/cutover"
if [[ -n "$(git -C "$root" status --porcelain -- "$dir")" ]]; then
  echo "CUTOVER_SOURCE_DIRTY: commit $dir first (the tag names a commit)" >&2
  exit 3
fi
commit="$(git -C "$root" log -1 --format=%h --abbrev=12 -- "$dir")"
image="$repo:cmp-cutover-$commit"

docker buildx build --platform linux/amd64 --load -t "$image" "$root/$dir"

echo "built $image (linux/amd64, not pushed)"
echo "push (승인 뒤): docker push $image"
echo "image=$image"
