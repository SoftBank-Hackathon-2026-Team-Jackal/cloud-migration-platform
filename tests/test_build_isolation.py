"""Ticket 54 (#189): user-app builds must not reach the EC2 instance metadata service."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "infra/demo"
GUARD = DEMO / "build-imds-guard.sh"
SPEC = "-i docker0 -d 169.254.169.254/32 -m comment --comment cmp-build-imds-guard -j DROP"


def test_guard_drops_only_default_bridge_traffic_to_imds():
    text = GUARD.read_text()
    assert f'spec="{SPEC}"' in text
    assert 'iptables -w -C "$chain" $spec 2>/dev/null || iptables -w -I "$chain" 1 $spec' in text
    assert "chain=DOCKER-USER" in text
    # Only the IMDS address: the VPC resolver (169.254.169.253) must stay reachable for builds.
    assert "169.254.0.0/16" not in text and "169.254.169.253" not in text


def test_guard_install_reapplies_after_docker_restarts_and_reboots():
    text = GUARD.read_text()
    unit = text.split("<<UNIT", 1)[1].split("\nUNIT", 1)[0]
    assert "After=docker.service" in unit and "BindsTo=docker.service" in unit
    assert "WantedBy=docker.service" in unit and "ExecStart=$target apply" in unit
    assert "systemctl enable cmp-build-imds-guard.service" in text
    assert text.rstrip().endswith("esac")
    assert '"$target" check' in text


def test_start_installs_guard_before_worker_can_build():
    lines = (DEMO / "start.sh").read_text().splitlines()
    guard = lines.index("sh infra/demo/build-imds-guard.sh install")
    worker = next(i for i, line in enumerate(lines) if "up -d --build api worker" in line)
    assert guard < worker


def test_probe_dockerfile_succeeds_only_when_imds_is_unreachable():
    text = (DEMO / "imds-probe/Dockerfile").read_text()
    assert "http://169.254.169.254/latest/api/token" in text
    assert "-X PUT" in text
    # Status codes only: response bodies (token, credentials) go to /dev/null.
    assert text.count("-o /dev/null") == text.count("curl ") == 2
    assert 'test "${token_status:-000}" = 000 && test "${role_status:-000}" = 000' in text


def test_instance_metadata_is_ipv4_only_and_v2():
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    template = yaml.load((DEMO / "cloudformation.yaml").read_text(), Loader=Loader)
    options = template["Resources"]["Instance"]["Properties"]["MetadataOptions"]
    assert options == {
        "HttpTokens": "required",
        "HttpEndpoint": "enabled",
        "HttpPutResponseHopLimit": 2,
        # The guard is an IPv4 rule; the IPv6 IMDS endpoint (fd00:ec2::254) stays off.
        "HttpProtocolIpv6": "disabled",
    }


def test_smoke_uses_the_build_module_command():
    text = (ROOT / "scripts/smoke_build_imds_guard.py").read_text()
    assert "from cmp_worker.image_build import _buildx_command, _docker_env" in text
    assert re.search(r"--network=host", text)
