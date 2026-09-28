from __future__ import annotations

import hashlib
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from mypy_boto3_ec2.client import EC2Client
from mypy_boto3_ec2.type_defs import ImageTypeDef

from . import config

BUILD_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "create-from-rhel10-ami.sh"

BUILT_FROM_TAG = "built-from"
LAST_USED_TAG = "last-used"

_BOOTSTRAP_SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=2",
    "-i", str(Path.home() / ".ssh" / "vraiti-ed25519.pem"),
]


def build_script_hash() -> str:
    return hashlib.sha256(BUILD_SCRIPT_PATH.read_bytes()).hexdigest()


def _poll_bootstrap(host: str, check_args: Sequence[str], *, attempts: int, interval: float, label: str) -> bool:
    for i in range(1, attempts + 1):
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", *_BOOTSTRAP_SSH_OPTS, host, *check_args],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return True
        print(f"Attempt {i}/{attempts} — {label}, retrying in {interval:.0f}s...")
        time.sleep(interval)
    return False


def provision_instance(public_ip: str) -> None:
    bootstrap_host = f"ec2-user@{public_ip}"

    print("Polling SSH readiness...")
    if not _poll_bootstrap(bootstrap_host, ["true"], attempts=60, interval=5, label="SSH not ready"):
        raise RuntimeError("SSH did not become ready after 300s")

    def upload_provision_script() -> None:
        subprocess.run(
            ["scp", *_BOOTSTRAP_SSH_OPTS, str(BUILD_SCRIPT_PATH), f"{bootstrap_host}:/tmp/"], check=True
        )

    print("Uploading create-from-rhel10-ami.sh...")
    upload_provision_script()

    print("Running phase 1 (driver install + reboot)...")
    subprocess.run(
        ["ssh", *_BOOTSTRAP_SSH_OPTS, bootstrap_host, "sudo bash /tmp/create-from-rhel10-ami.sh 1"], check=False
    )

    print("Waiting for reboot...")
    time.sleep(15)
    if not _poll_bootstrap(bootstrap_host, ["true"], attempts=60, interval=5, label="SSH not ready"):
        raise RuntimeError("SSH did not come back after reboot after 300s")

    print("Re-uploading create-from-rhel10-ami.sh...")
    upload_provision_script()

    print("Running phase 2 (remaining packages, verify driver)...")
    result = subprocess.run(
        ["ssh", *_BOOTSTRAP_SSH_OPTS, bootstrap_host, "sudo bash /tmp/create-from-rhel10-ami.sh 2"], check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"phase 2 failed on {bootstrap_host} (exit {result.returncode}); check it manually")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def register_ami(client: EC2Client, region: str, instance_id: str, script_hash: str) -> str:
    """Fires create_image and returns immediately -- AWS snapshots the
    volume and finishes registering the AMI on its own, in the background,
    without needing the source instance kept idle or blocked on it. The
    image is "pending" until some later poller invocation's warm-scan
    notices it turned "available"."""
    name = f"{config.AMI_NAME}-{region}-{int(time.time())}"
    print(f"[{region}] creating AMI '{name}' from {instance_id} (finishes in the background)...")
    image_id = client.create_image(
        InstanceId=instance_id,
        Name=name,
        Description=f"{config.AMI_NAME}, built {_today()}",
        # The instance goes straight on to run the job that triggered the
        # build; the default reboot for a clean snapshot would kill it.
        NoReboot=True,
        TagSpecifications=[
            {
                "ResourceType": "image",
                "Tags": [
                    {"Key": "Name", "Value": config.AMI_NAME},
                    {"Key": "Project", "Value": config.PROJECT_TAG},
                    {"Key": BUILT_FROM_TAG, "Value": script_hash},
                    {"Key": LAST_USED_TAG, "Value": _today()},
                ],
            }
        ],
    )["ImageId"]
    return image_id


def touch_last_used(client: EC2Client, image_id: str) -> None:
    client.create_tags(Resources=[image_id], Tags=[{"Key": LAST_USED_TAG, "Value": _today()}])


def _tag_value(image: ImageTypeDef, key: str) -> "str | None":
    return next((t["Value"] for t in image.get("Tags", []) if t["Key"] == key), None)


def is_stale(image: ImageTypeDef, script_hash: str) -> bool:
    built_from = _tag_value(image, BUILT_FROM_TAG)
    last_used = _tag_value(image, LAST_USED_TAG)
    if built_from != script_hash:
        return True
    if last_used is None:
        return True
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=config.WARM_MAX_AGE_DAYS)
    return datetime.fromisoformat(last_used).date() < cutoff


def _deregister(client: EC2Client, region: str, image: ImageTypeDef, reason: str) -> None:
    image_id = image["ImageId"]
    print(f"[{region}] deregistering {reason} AMI {image_id}...")
    client.deregister_image(ImageId=image_id)
    for mapping in image.get("BlockDeviceMappings", []):
        snapshot_id = mapping.get("Ebs", {}).get("SnapshotId")
        if snapshot_id:
            client.delete_snapshot(SnapshotId=snapshot_id)


def scan_and_gc(client: EC2Client, region: str, script_hash: str) -> "ImageTypeDef | None":
    """A "pending" image (create_image still snapshotting in the
    background) is neither warm nor stale yet -- left alone, to be picked
    up by a later invocation once it resolves to "available" or "failed"."""
    images = client.describe_images(Owners=["self"], Filters=[{"Name": "tag:Name", "Values": [config.AMI_NAME]}])[
        "Images"
    ]
    warm_images: list[ImageTypeDef] = []
    for image in images:
        if image["State"] == "pending":
            continue
        if image["State"] == "failed":
            _deregister(client, region, image, "failed")
        elif is_stale(image, script_hash):
            _deregister(client, region, image, "stale")
        else:
            warm_images.append(image)
    if not warm_images:
        return None
    return max(warm_images, key=lambda i: i["CreationDate"])
