"""instanceFilters is a prioritized list of filter tiers -- each tier one
InstanceFilter or a list of them. "Permissiveness" starts at 0 (only the
first tier active) and increases by 1 every minute polled, widening which
tiers' candidates are tried. A candidate whose region is already warm (a
same-build-script vraiti-rhel10-cuda AMI used in the last week) is tried
immediately regardless of permissiveness, as long as it matches any tier at
all -- only a cold-region launch has to pay for a from-scratch AMI build.
"""
from __future__ import annotations

import fnmatch
import re
import time
from typing import Any

from mypy_boto3_ec2.type_defs import BlockDeviceMappingTypeDef, ImageTypeDef

from models import InstanceFilter

from . import build, config, ec2, gpu_instance_types

AUTOMANAGED_ALIAS_RE = re.compile(r"^automanaged-(\d+)$")
POLL_ROUND_INTERVAL = 5.0
PERMISSIVENESS_INTERVAL_SECONDS = 60.0

GpuTypes = dict[str, dict[str, Any]]


def _normalize_tiers(instance_filters: list[Any]) -> list[list[InstanceFilter]]:
    return [tier if isinstance(tier, list) else [tier] for tier in instance_filters]


def _flatten(tiers: list[list[InstanceFilter]]) -> list[InstanceFilter]:
    return [f for tier in tiers for f in tier]


def _filter_regions(filt: InstanceFilter, all_regions: list[str]) -> list[str]:
    if filt.regions is None:
        return all_regions
    return [r for r in all_regions if any(fnmatch.fnmatch(r, pattern) for pattern in filt.regions)]


def _matches_filter(
    filt: InstanceFilter, region: str, instance_type: str, gpu_types: GpuTypes, all_regions: list[str]
) -> bool:
    if filt.instance_types is not None and instance_type not in filt.instance_types:
        return False
    if region not in _filter_regions(filt, all_regions):
        return False
    if filt.max_hourly_cost is not None:
        cost = gpu_types.get(instance_type, {}).get("cost_per_hour_usd")
        if cost is None or cost > filt.max_hourly_cost:
            return False
    return True


def _matches_any(
    filters: list[InstanceFilter], region: str, instance_type: str, gpu_types: GpuTypes, all_regions: list[str]
) -> bool:
    return any(_matches_filter(f, region, instance_type, gpu_types, all_regions) for f in filters)


def _candidates_for_filters(
    filters: list[InstanceFilter], gpu_types: GpuTypes, all_regions: list[str]
) -> set[tuple[str, str]]:
    candidates: set[tuple[str, str]] = set()
    for filt in filters:
        instance_types = filt.instance_types if filt.instance_types is not None else sorted(gpu_types)
        for instance_type in instance_types:
            offered_in = gpu_types.get(instance_type, {}).get("regions", [])
            for region in offered_in:
                if _matches_filter(filt, region, instance_type, gpu_types, all_regions):
                    candidates.add((region, instance_type))
    return candidates


def _find_existing_match(
    filters: list[InstanceFilter], gpu_types: GpuTypes, all_regions: list[str]
) -> "tuple[str, str, str, str] | None":
    for region, instance in ec2.all_managed_instances(all_regions):
        if instance["State"]["Name"] != "running":
            continue
        alias = next((t["Value"] for t in instance.get("Tags", []) if t["Key"] == "ssh-alias"), None)
        if not alias or not AUTOMANAGED_ALIAS_RE.match(alias):
            continue
        instance_type = instance["InstanceType"]
        public_ip = instance.get("PublicIpAddress")
        if public_ip and _matches_any(filters, region, instance_type, gpu_types, all_regions):
            return alias, region, instance["ImageId"], public_ip
    return None


def _next_automanaged_alias(all_regions: list[str]) -> str:
    indices = [int(m.group(1)) for m in map(AUTOMANAGED_ALIAS_RE.match, config.configured_aliases()) if m]
    for _region, instance in ec2.all_managed_instances(all_regions):
        alias = next((t["Value"] for t in instance.get("Tags", []) if t["Key"] == "ssh-alias"), None)
        match = AUTOMANAGED_ALIAS_RE.match(alias) if alias else None
        if match:
            indices.append(int(match.group(1)))
    return f"automanaged-{max(indices, default=-1) + 1}"


# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def _try_launch(
    region: str, instance_type: str, alias: str, tag_name: str, public_key: str, warm_ami: "ImageTypeDef | None"
) -> "tuple[str, ImageTypeDef | None] | None":
    client = ec2.client_for(region)
    group_id = ec2.ensure_security_group(client, region)
    ec2.ensure_key_pair(client, region, public_key)

    if warm_ami is not None:
        ami_id = warm_ami["ImageId"]
        root_device_name = warm_ami["RootDeviceName"]
    else:
        ami_id, root_device_name = ec2.find_rhel10_ami(client)

    mappings: list[BlockDeviceMappingTypeDef] = [
        {"DeviceName": root_device_name, "Ebs": {"VolumeSize": config.ROOT_VOLUME_SIZE, "VolumeType": "gp3"}}
    ]
    instance_id = ec2.try_run_instance(region, ami_id, instance_type, group_id, alias, tag_name, mappings)
    if instance_id is None:
        return None
    return instance_id, warm_ami


def _scan_warm_regions(regions: list[str], script_hash: str) -> dict[str, ImageTypeDef]:
    print(f"Scanning {len(regions)} region(s) for a warm '{config.AMI_NAME}'...")
    warm: dict[str, ImageTypeDef] = {}
    for region in regions:
        warm_image = build.scan_and_gc(ec2.client_for(region), region, script_hash)
        if warm_image is not None:
            warm[region] = warm_image
    print(f"Warm regions: {', '.join(sorted(warm)) or '(none)'}")
    return warm


def _warm_candidates(
    warm_regions: dict[str, ImageTypeDef], filters: list[InstanceFilter], gpu_types: GpuTypes, all_regions: list[str]
) -> set[tuple[str, str]]:
    return {
        (region, instance_type)
        for region in warm_regions
        for instance_type in gpu_types
        if region in gpu_types.get(instance_type, {}).get("regions", [])
        and _matches_any(filters, region, instance_type, gpu_types, all_regions)
    }


# pylint: disable-next=too-many-locals
def request_instance(instance_filters: list[Any]) -> str:
    tiers = _normalize_tiers(instance_filters)
    full_filters = _flatten(tiers)
    if not full_filters:
        raise RuntimeError("instanceFilters is empty")

    gpu_types = gpu_instance_types.ensure_fresh()
    all_regions = ec2.list_regions()

    existing = _find_existing_match(full_filters, gpu_types, all_regions)
    if existing is not None:
        alias, region, image_id, public_ip = existing
        print(f"Reusing existing instance aliased '{alias}'")
        config.write_alias(alias, public_ip, region)
        try:
            build.touch_last_used(ec2.client_for(region), image_id)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            print(f"WARNING: could not refresh last-used on {image_id}: {exc}")
        return alias

    script_hash = build.build_script_hash()
    candidate_regions = sorted({r for f in full_filters for r in _filter_regions(f, all_regions)})
    warm_amis = _scan_warm_regions(candidate_regions, script_hash)

    alias = _next_automanaged_alias(all_regions)
    tag_name = f"vraiti-automanaged-{time.strftime('%Y%m%d')}-{alias}"
    public_key = ec2.local_public_key()
    start = time.monotonic()
    round_num = 0

    while True:
        round_num += 1
        permissiveness = min(int((time.monotonic() - start) // PERMISSIVENESS_INTERVAL_SECONDS), len(tiers) - 1)
        active_filters = _flatten(tiers[: permissiveness + 1])

        tier_candidates = _candidates_for_filters(active_filters, gpu_types, all_regions)
        warm_candidates = _warm_candidates(warm_amis, full_filters, gpu_types, all_regions)
        candidates = sorted(warm_candidates) + sorted(tier_candidates - warm_candidates)

        for region, instance_type in candidates:
            print(
                f"\r\x1b[2K[round {round_num}, permissiveness {permissiveness}] trying {region} {instance_type}...",
                end="", flush=True,
            )
            result = _try_launch(region, instance_type, alias, tag_name, public_key, warm_amis.get(region))
            if result is None:
                continue
            print("\r\x1b[2K", end="", flush=True)
            instance_id, warm_ami_used = result
            print(f"Winner: {region} {instance_type} ({instance_id})")

            _instance_id, public_ip, _region = ec2.wait_for_running(region, instance_id)

            if warm_ami_used is not None:
                build.touch_last_used(ec2.client_for(region), warm_ami_used["ImageId"])
            else:
                print(f"{region} is cold -- building {config.AMI_NAME} on this instance...")
                build.provision_instance(public_ip)
                build.register_ami(ec2.client_for(region), region, instance_id, script_hash)

            config.write_alias(alias, public_ip, region)
            return alias

        print("\r\x1b[2K", end="", flush=True)
        print(f"No capacity in any of {len(candidates)} candidates (round {round_num}); retrying...")
        time.sleep(POLL_ROUND_INTERVAL)
