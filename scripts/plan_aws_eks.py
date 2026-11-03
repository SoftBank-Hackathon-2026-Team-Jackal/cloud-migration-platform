"""Create an infrastructure-only AWS EKS saved Plan and redacted Preview."""

import argparse
import json
from pathlib import Path

from cmp_contracts.provisioning import ProvisioningInputError
from cmp_providers.aws_preview import AwsPlanInput, plan_aws_infrastructure
from pydantic import ValidationError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Reviewed AWS infrastructure input JSON")
    parser.add_argument("output", type=Path, help="Protected local Plan artifact directory")
    parser.add_argument("--aws-profile", required=True, help="Explicit named AWS CLI profile")
    parser.add_argument("--terraform-bin", default="terraform")
    parser.add_argument(
        "--local-validation",
        action="store_true",
        help="Use temporary local State; Preview is explicitly ineligible for approval",
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
        parser.exit(2, "Invalid AWS Plan input schema.\n")
    except ProvisioningInputError as error:
        parser.exit(2, f"AWS Plan rejected: {error}\n")
    # Preview contains only allowlisted fields; never print Terraform JSON or the binary Plan.
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
