"""AWS EKS 인프라 저장 Plan과 민감정보 제거 Preview를 생성한다."""

import argparse
import json
from pathlib import Path

from cmp_contracts.provisioning import ProvisioningInputError
from cmp_providers.aws_preview import AwsPlanInput, plan_aws_infrastructure
from pydantic import ValidationError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="검토된 AWS 인프라 입력 JSON")
    parser.add_argument("output", type=Path, help="접근이 제한된 로컬 Plan 산출물 디렉터리")
    parser.add_argument("--aws-profile", required=True, help="이름이 지정된 AWS CLI 프로필")
    parser.add_argument("--terraform-bin", default="terraform")
    parser.add_argument(
        "--local-validation",
        action="store_true",
        help="임시 로컬 State로 검증하며 Preview를 승인 불가로 표시",
    )
    args = parser.parse_args()
    try:
        request = AwsPlanInput.model_validate_json(args.input.read_bytes())
        manifest, preview = plan_aws_infrastructure(
            request,
            output_dir=args.output,
            aws_profile=args.aws_profile,
            terraform_bin=args.terraform_bin,
            backend_mode="LOCAL_VALIDATION" if args.local_validation else "S3",
        )
    except ValidationError:
        parser.exit(2, "AWS Plan 입력 형식이 올바르지 않습니다.\n")
    except ProvisioningInputError as error:
        parser.exit(2, f"AWS Plan 거절: {error}\n")
    # Preview에는 허용된 필드만 담는다. Terraform JSON과 바이너리 Plan은 출력하지 않는다.
    print(
        json.dumps(
            {
                "plan_id": manifest.plan.plan_id,
                "revision": manifest.plan.revision,
                "preview": preview.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
