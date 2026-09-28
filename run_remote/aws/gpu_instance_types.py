from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError
from mypy_boto3_ec2.client import EC2Client
from mypy_boto3_ec2.type_defs import InstanceTypeInfoTypeDef
from mypy_boto3_pricing.client import PricingClient

from . import config, ec2

GPU_TYPES_PATH = Path(__file__).resolve().parent / "gpu_instance_types.json"
GPU_TYPES_MAX_AGE_SECONDS = 4 * 60 * 60


def _offerings_for_region(region: str) -> dict[str, set[str]]:
    client = ec2.client_for(region)
    result: dict[str, set[str]] = {}
    paginator = client.get_paginator("describe_instance_type_offerings")
    for page in paginator.paginate(LocationType="region"):
        for offering in page["InstanceTypeOfferings"]:
            result.setdefault(offering["InstanceType"], set()).add(region)
    return result


def _all_offerings(regions: list[str]) -> dict[str, set[str]]:
    combined: dict[str, set[str]] = {}
    with ThreadPoolExecutor(max_workers=min(10, len(regions))) as pool:
        futures = [pool.submit(_offerings_for_region, region) for region in regions]
        for future in as_completed(futures):
            for instance_type, regions_set in future.result().items():
                combined.setdefault(instance_type, set()).update(regions_set)
    return combined


def _batched(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _describe_batch(client: EC2Client, batch: list[str]) -> list[InstanceTypeInfoTypeDef]:
    while batch:
        try:
            return client.describe_instance_types(InstanceTypes=batch)["InstanceTypes"]  # type: ignore[arg-type]
        except ClientError as exc:
            message = exc.response.get("Error", {}).get("Message", "")
            match = re.search(r"do not exist: \[(.*?)\]", message)
            if not match:
                raise
            invalid = {t.strip() for t in match.group(1).split(",")}
            batch = [t for t in batch if t not in invalid]
    return []


def _gpu_instance_specs(client: EC2Client, instance_types: list[str]) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    for batch in _batched(instance_types, 100):
        for info in _describe_batch(client, batch):
            gpu_info = info.get("GpuInfo")
            if not gpu_info:
                continue
            gpus = gpu_info["Gpus"]
            gpu_partition_size = gpus[0].get("GpuPartitionSize", 1.0)
            is_fractional = gpu_partition_size < 1.0
            gpu_count = sum(g.get("LogicalGpuCount") or g["Count"] for g in gpus) if is_fractional else sum(
                g["Count"] for g in gpus
            )
            total_memory_mib = gpu_info.get("TotalGpuMemoryInMiB")
            if total_memory_mib is None:
                total_memory_mib = sum(g["MemoryInfo"]["SizeInMiB"] * g["Count"] for g in gpus)
            specs[info["InstanceType"]] = {
                "gpu_count": gpu_count,
                "gpu_partition_size": gpu_partition_size if is_fractional else None,
                "gpu_model": gpus[0]["Name"],
                "gpu_vram_gib": round(total_memory_mib / 1024, 1),
            }
    return specs


def _price_for_type(pricing: PricingClient, instance_type: str, region: str) -> "float | None":
    attempts = 0
    while True:
        try:
            response = pricing.get_products(
                ServiceCode="AmazonEC2",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
                    {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
                    {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
                    {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
                    {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
                    {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
                    {"Type": "TERM_MATCH", "Field": "marketoption", "Value": "OnDemand"},
                ],
            )
            break
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ThrottlingException" or attempts >= 5:
                raise
            attempts += 1
            time.sleep(2**attempts)

    for price_str in response["PriceList"]:
        product = json.loads(price_str)
        for term in product["terms"].get("OnDemand", {}).values():
            for dimension in term["priceDimensions"].values():
                return float(dimension["pricePerUnit"]["USD"])
    return None


def _price_all(specs: dict[str, dict[str, Any]], offerings: dict[str, set[str]]) -> None:
    pricing: PricingClient = boto3.client("pricing", region_name=config.PRICING_REGION)

    def _price_one(instance_type: str) -> tuple[str, "float | None"]:
        region = sorted(offerings[instance_type])[0]
        return instance_type, _price_for_type(pricing, instance_type, region)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_price_one, instance_type) for instance_type in specs]
        for future in as_completed(futures):
            instance_type, price = future.result()
            specs[instance_type]["cost_per_hour_usd"] = price


def build_gpu_instance_type_map() -> dict[str, dict[str, Any]]:
    regions = ec2.list_regions()
    offerings = _all_offerings(regions)

    describe_client = ec2.client_for(config.PRICING_REGION)
    specs = _gpu_instance_specs(describe_client, sorted(offerings))

    for instance_type, info in specs.items():
        info["regions"] = sorted(offerings[instance_type])

    _price_all(specs, offerings)
    return specs


def ensure_fresh() -> dict[str, dict[str, Any]]:
    age = None if not GPU_TYPES_PATH.is_file() else time.time() - GPU_TYPES_PATH.stat().st_mtime
    if age is None or age > GPU_TYPES_MAX_AGE_SECONDS:
        print(f"{GPU_TYPES_PATH.name} missing or stale, refreshing...")
        specs = build_gpu_instance_type_map()
        GPU_TYPES_PATH.write_text(json.dumps(specs, indent=2, sort_keys=True), encoding="utf-8")
        return specs
    return json.loads(GPU_TYPES_PATH.read_text(encoding="utf-8"))
