"""Create an infrastructure-only GCP GKE saved Plan and redacted Preview."""

import argparse
import json
from pathlib import Path

from cmp_contracts.provisioning import ProvisioningInputError
from cmp_providers.gcp_preview import GcpPlanInput, plan_gcp_infrastructure
from pydantic import ValidationError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Reviewed GCP infrastructure input JSON")
    parser.add_argument("output", type=Path, help="Protected local Plan artifact directory")
    parser.add_argument("--mode", choices=["S3", "LOCAL_TEST"], default="S3")
    parser.add_argument("--gcloud-configuration")
    parser.add_argument("--expected-adc-email")
    parser.add_argument("--aws-profile", help="Named AWS profile for central S3 State")
    parser.add_argument("--terraform-bin", default="terraform")
    args = parser.parse_args()
    try:
        request = GcpPlanInput.model_validate_json(args.input.read_bytes())
        manifest, preview = plan_gcp_infrastructure(
            request,
            output_dir=args.output,
            mode=args.mode,
            gcloud_configuration=args.gcloud_configuration,
            expected_adc_email=args.expected_adc_email,
            aws_profile=args.aws_profile,
            terraform_bin=args.terraform_bin,
        )
    except ValidationError:
        parser.exit(2, "Invalid GCP Plan input schema.\n")
    except ProvisioningInputError as error:
        parser.exit(2, f"GCP Plan rejected: {error}\n")
    print(
        json.dumps(
            {
                "plan_id": manifest.plan.plan_id,
                "revision": manifest.plan.revision,
                "target": manifest.target.model_dump(mode="json"),
                "artifacts": {
                    role: artifact.model_dump(mode="json")
                    for role, artifact in manifest.artifacts.items()
                },
                "preview": preview.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
