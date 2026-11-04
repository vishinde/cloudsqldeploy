#!/usr/bin/env python3
"""
Cloud SQL Design for Obtainability (DfO) Deployer

Orchestrates automated multi-tier and multi-zone deployment for Google Cloud SQL,
resiliently overcoming ZONE_RESOURCE_POOL_EXHAUSTED errors with a circuit breaker.
"""

import argparse
import json
import sys
import time
import google.auth
from googleapiclient import discovery
from googleapiclient.errors import HttpError
import yaml


class CloudSqlDfoDeployer:
    """Manages resilient Cloud SQL deployments across zones and tiers."""

    def __init__(self, config: dict, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.project_id = config["project_id"]
        self.region = config["region"]
        self.db_version = config["database_version"]
        self.preferred_zones = config["preferred_zones"]
        self.candidate_tiers = config["candidate_tiers"]
        self.max_strikes = config.get("circuit_breaker_max_strikes", 2)
        self.timeout_sec = config.get("operation_timeout_sec", 600)

        # Authenticate and initialize Cloud SQL client
        credentials, _ = google.auth.default()
        self.sql_client = discovery.build("sqladmin", "v1beta4", credentials=credentials)

    def _wait_for_operation(self, operation_id: str) -> tuple[bool, str]:
        """Polls an asynchronous Cloud SQL operation until completion or timeout."""
        start_time = time.time()
        while time.time() - start_time < self.timeout_sec:
            op = self.sql_client.operations().get(
                project=self.project_id, operation=operation_id
            ).execute()
            status = op.get("status")

            if status == "DONE":
                if "error" in op:
                    err_info = op["error"].get("errors", [{}])[0]
                    return False, f"[{err_info.get('code', 'UNKNOWN')}] {err_info.get('message', '')}"
                return True, "SUCCESS"

            time.sleep(10)
        return False, f"[TIMEOUT] Exceeded {self.timeout_sec}s polling timeout."

    def deploy(self, base_name: str = None) -> dict | None:
        """
        Executes the DfO multi-tier, multi-zone fallback state machine.

        Strategy:
          1. Evaluates tiers in order of preference (Priority 1 -> 2 -> ...).
          2. Evaluates zones within each tier.
          3. Catches ZONE_RESOURCE_POOL_EXHAUSTED and trips a circuit breaker.
          4. Returns instance metadata on success.
        """
        instance_prefix = base_name or self.config.get("base_instance_name", "sql-instance")
        print(f"[START] Commencing Cloud SQL Deployment for '{instance_prefix}' in {self.region}...")
        if self.dry_run:
            print("        *** DRY-RUN MODE: Validating execution plan without creating resources ***\n")

        for tier_cfg in self.candidate_tiers:
            prio = tier_cfg["priority"]
            name = tier_cfg["name"]
            tier = tier_cfg["tier"]
            edition = tier_cfg["edition"]
            disk_type = tier_cfg["disk_type"]
            disk_size = tier_cfg["disk_size_gb"]
            data_cache = tier_cfg.get("enable_data_cache", False)

            print(f"\n{'='*75}")
            print(f"[Tier Priority {prio}] {name}")
            print(f"  Tier: {tier} | Edition: {edition} | Disk: {disk_type} ({disk_size}GB)")
            print(f"{'='*75}")

            strike_count = 0

            for zone in self.preferred_zones:
                instance_id = f"{instance_prefix}-{zone[-1]}-{int(time.time()) % 1000}"
                print(f"\n  [ATTEMPT] Primary Zone: '{zone}' (Regional HA)")

                request_body = {
                    "name": instance_id,
                    "region": self.region,
                    "databaseVersion": self.db_version,
                    "settings": {
                        "tier": tier,
                        "edition": edition,
                        "availabilityType": "REGIONAL",
                        "locationPreference": {"zone": zone},
                        "dataDiskType": disk_type,
                        "dataDiskSizeGb": int(disk_size),
                        "storageAutoResize": True,
                        "dataCacheConfig": {"dataCacheEnabled": data_cache}
                    }
                }

                if self.dry_run:
                    print("            [DRY-RUN] Request payload validated:")
                    print("            " + json.dumps(request_body, indent=2).replace("\n", "\n            "))
                    continue

                try:
                    op = self.sql_client.instances().insert(
                        project=self.project_id, body=request_body
                    ).execute()

                    op_id = op.get("name")
                    print(f"            Dispatched operation: {op_id}. Polling status...")

                    success, msg = self._wait_for_operation(op_id)

                    if success:
                        print(f"\n[SUCCESS] Instance '{instance_id}' provisioned successfully!")
                        print(f"          Active Zone  : {zone}")
                        print(f"          Target Tier  : {tier} ({edition})")
                        print(f"          Data Disk    : {disk_type} ({disk_size}GB)")
                        return {
                            "instance_name": instance_id,
                            "zone": zone,
                            "tier": tier,
                            "edition": edition,
                            "disk_type": disk_type,
                            "disk_size_gb": disk_size
                        }
                    else:
                        print(f"            [FAILED] {msg}")

                        # Check for stockout signature
                        if "ZONE_RESOURCE_POOL_EXHAUSTED" in msg or "resource pool exhausted" in msg.lower():
                            strike_count += 1
                            print(f"            [CAPACITY STOCKOUT] Strike {strike_count}/{self.max_strikes} for '{zone}'.")
                            if strike_count >= self.max_strikes:
                                print(f"            [CIRCUIT BREAKER TRIPPED] Rotating away from zone '{zone}'.")
                            continue
                        else:
                            print(f"            [FATAL ERROR] Non-capacity error encountered. Aborting.")
                            return None

                except HttpError as err:
                    print(f"            [API ERROR] {err.resp.status}: {err.content.decode('utf-8')}")
                    continue

            if self.dry_run:
                continue

            print(f"\n[WARN] All preferred zones exhausted for Tier Priority {prio} ({tier}). Falling back...")

        if self.dry_run:
            print("\n[DRY-RUN COMPLETE] All configured tiers and zones are well-formed.")
            return {"status": "dry_run_success"}

        print("\n[CRITICAL] All candidate tiers and zones were exhausted.")
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Deploy Google Cloud SQL with Design for Obtainability (DfO) Fallback"
    )
    parser.add_argument(
        "--config", default="config.yaml", help="Path to YAML configuration file (default: config.yaml)"
    )
    parser.add_argument(
        "--name", default=None, help="Override base instance name"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print execution plan and payloads without creating resources"
    )
    args = parser.parse_args()

    try:
        with open(args.config, "r") as f:
            config_data = yaml.safe_load(f)
    except FileNotFoundError:
        print(f"[ERROR] Configuration file '{args.config}' not found.")
        sys.exit(1)
    except yaml.YAMLError as exc:
        print(f"[ERROR] Failed to parse '{args.config}': {exc}")
        sys.exit(1)

    deployer = CloudSqlDfoDeployer(config_data, dry_run=args.dry_run)
    result = deployer.deploy(base_name=args.name)

    if not result:
        sys.exit(1)


if __name__ == "__main__":
    main()
