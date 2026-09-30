from __future__ import annotations

import fnmatch
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from mypy_boto3_ec2.client import EC2Client
from mypy_boto3_ec2.type_defs import BlockDeviceMappingTypeDef, InstanceTypeDef

from . import config

CAPACITY_ERROR_CODES = {
    "InsufficientInstanceCapacity",
    "InsufficientHostCapacity",
    "InsufficientCapacity",
}


def list_regions(globs: list[str] | None = None) -> list[str]:
    patterns = globs or ["*"]
    ec2 = client_for(config.PRICING_REGION)
    names = [r["RegionName"] for r in ec2.describe_regions()["Regions"]]
    return sorted(name for name in names if any(fnmatch.fnmatch(name, pattern) for pattern in patterns))


def client_for(region: str) -> EC2Client:
    return boto3.client("ec2", region_name=region)


def local_public_key() -> str:
    pem_path = Path.home() / ".ssh" / "vraiti-ed25519.pem"
    result = subprocess.run(["ssh-keygen", "-y", "-f", str(pem_path)], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def find_rhel10_ami(ec2: EC2Client) -> tuple[str, str]:
    images = ec2.describe_images(
        Owners=[config.RHEL10_AMI_OWNER],
        Filters=[{"Name": "name", "Values": ["RHEL-10*x86_64*"]}, {"Name": "state", "Values": ["available"]}],
    )["Images"]
    if not images:
        raise RuntimeError(f"could not find a RHEL 10 AMI owned by {config.RHEL10_AMI_OWNER}")
    newest = max(images, key=lambda image: image["CreationDate"])
    return newest["ImageId"], newest["RootDeviceName"]


def ensure_security_group(ec2: EC2Client, region: str) -> str:
    existing = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [config.SECURITY_GROUP_NAME]}]
    )["SecurityGroups"]
    if existing:
        return existing[0]["GroupId"]
    print(f"[{region}] security group '{config.SECURITY_GROUP_NAME}' not found, creating...")
    group_id = ec2.create_security_group(
        GroupName=config.SECURITY_GROUP_NAME, Description=config.SECURITY_GROUP_DESCRIPTION
    )["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=group_id, IpPermissions=config.SECURITY_GROUP_INGRESS  # type: ignore[arg-type]
    )
    print(f"[{region}] security group created: {group_id}")
    return group_id


def ensure_key_pair(ec2: EC2Client, region: str, public_key: str) -> None:
    try:
        ec2.describe_key_pairs(KeyNames=[config.KEY_NAME])
        return
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "InvalidKeyPair.NotFound":
            raise
    print(f"[{region}] key pair '{config.KEY_NAME}' not found, importing...")
    ec2.import_key_pair(KeyName=config.KEY_NAME, PublicKeyMaterial=public_key.encode())


def try_run_instance(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    region: str,
    ami_id: str,
    instance_type: str,
    group_id: str,
    ssh_alias: str,
    tag_name: str,
    block_device_mappings: list[BlockDeviceMappingTypeDef],
) -> "str | None":
    """Returns None (not raising) on a capacity error; any other
    ClientError/RuntimeError propagates."""
    no_retry_ec2 = boto3.client("ec2", region_name=region, config=Config(retries={"max_attempts": 1}))
    try:
        instance_id = no_retry_ec2.run_instances(
            ImageId=ami_id,
            InstanceType=instance_type,  # type: ignore[arg-type]
            KeyName=config.KEY_NAME,
            SecurityGroupIds=[group_id],
            BlockDeviceMappings=block_device_mappings,
            # The AMI's idle-ssh hook runs `shutdown -h now`: terminate rather
            # than stop. Packages and venvs are cheap to rebuild on a fresh
            # instance; only the (warm) AMI is worth keeping between jobs.
            InstanceInitiatedShutdownBehavior="terminate",
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": tag_name},
                        {"Key": "Project", "Value": config.PROJECT_TAG},
                        {"Key": "ssh-alias", "Value": ssh_alias},
                    ],
                }
            ],
            MinCount=1,
            MaxCount=1,
        )["Instances"][0]["InstanceId"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in CAPACITY_ERROR_CODES:
            return None
        raise
    print(f"[{region} {instance_type}] launched {instance_id}")
    return instance_id


def wait_for_running(region: str, instance_id: str) -> tuple[str, str, str]:
    ec2 = client_for(region)
    print("Waiting for instance to reach running state...")
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])

    instance = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    public_ip = instance.get("PublicIpAddress")
    if not public_ip:
        raise RuntimeError("instance has no public IP")

    return instance_id, public_ip, region


LIVE_STATES = ["running", "stopped", "pending", "stopping"]


def all_managed_instances(regions: list[str]) -> list[tuple[str, InstanceTypeDef]]:
    def _query(region: str) -> list[tuple[str, InstanceTypeDef]]:
        ec2 = client_for(region)
        reservations = ec2.describe_instances(
            Filters=[
                {"Name": "tag:Project", "Values": [config.PROJECT_TAG]},
                {"Name": "instance-state-name", "Values": LIVE_STATES},
            ]
        )["Reservations"]
        return [(region, instance) for reservation in reservations for instance in reservation["Instances"]]

    results: list[tuple[str, InstanceTypeDef]] = []
    with ThreadPoolExecutor(max_workers=max(1, len(regions))) as pool:
        for batch in pool.map(_query, regions):
            results.extend(batch)
    return results
