from __future__ import annotations

from pathlib import Path

AMI_NAME = "vraiti-rhel10-cuda"
ROOT_VOLUME_SIZE = 200
KEY_NAME = "vraiti-ed25519"
PROJECT_TAG = "aws_manage_managed"
RHEL10_AMI_OWNER = "309956199498"
# 172.31.0.0/16 is the standard AWS default-VPC CIDR in every region.
SECURITY_GROUP_NAME = "vraiti-run-remote"
SECURITY_GROUP_DESCRIPTION = "run-remote automanaged instances: SSH, vllm serve, misc service ports"
SECURITY_GROUP_INGRESS = [
    {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "172.31.0.0/16"}]},
    {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
    {"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
    {"IpProtocol": "tcp", "FromPort": 9090, "ToPort": 9090, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
]

# AWS's Pricing API only serves requests from us-east-1.
PRICING_REGION = "us-east-1"

WARM_MAX_AGE_DAYS = 7

SSH_CONFIG_FILE = Path.home() / ".ssh" / "config.d" / "awsm"


def _read_blocks() -> list[list[str]]:
    if not SSH_CONFIG_FILE.is_file():
        return []
    blocks: list[list[str]] = []
    for line in SSH_CONFIG_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("Host "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    return blocks


def _write_blocks(blocks: list[list[str]]) -> None:
    SSH_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = [line for block in blocks for line in block]
    SSH_CONFIG_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _block_alias(block: list[str]) -> str:
    return block[0].split(None, 1)[1].strip()


def alias_exists(alias: str) -> bool:
    return any(_block_alias(block) == alias for block in _read_blocks())


def write_alias(alias: str, hostname: str, region: str) -> None:
    blocks = [block for block in _read_blocks() if _block_alias(block) != alias]
    blocks.append(
        [
            f"Host {alias}",
            f"    HostName {hostname}",
            "    User ec2-user",
            "    IdentityFile ~/.ssh/vraiti-ed25519.pem",
            "    StrictHostKeyChecking accept-new",
            "    GSSAPIAuthentication no",
            f"    # aws-region: {region}",
            "",
        ]
    )
    _write_blocks(blocks)


def get_region(alias: str) -> str | None:
    for block in _read_blocks():
        if _block_alias(block) == alias:
            for line in block[1:]:
                stripped = line.strip()
                if stripped.startswith("# aws-region:"):
                    return stripped.split(":", 1)[1].strip()
    return None


def configured_aliases() -> list[str]:
    return [_block_alias(block) for block in _read_blocks()]
