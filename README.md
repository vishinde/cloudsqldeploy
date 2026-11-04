# Cloud SQL Resilient Deployer (Design for Obtainability)

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

An automated deployment state machine for **Google Cloud SQL** that implements Google Cloud's **Design for Obtainability (DfO)** architectural principles to proactively handle zonal capacity constraints and `ZONE_RESOURCE_POOL_EXHAUSTED` stockouts.

---

## The Challenge

In high-demand cloud regions, launching or scaling managed database instances can occasionally encounter zonal capacity constraints:

```text
400: The resource pool exhausted in zone us-central1-a (ZONE_RESOURCE_POOL_EXHAUSTED)
```

Because Cloud SQL instances run in Google-managed tenant service projects, customers cannot attach consumer-project Compute Engine Zonal Reservations directly to Cloud SQL instances. Relying on blind, single-zone retries often leads to repeated failures and pipeline timeouts.

---

## Architectural Solution

This tool replaces blind retries with a declarative, multi-tiered resilience engine:

1. **Strategic Modernization Hierarchy**:
   - **Priority 1 (Strategic Default)**: Gen 4 N4 (`db-custom-N4-2-8192`) on **Hyperdisk Balanced** (free 3,000 IOPS baseline, deep hardware pools).
   - **Priority 2 (High Performance)**: Gen 4 C4 (`db-perf-optimized-C4-2`) on **Hyperdisk Balanced** with local NVMe SSD Data Cache.
   - **Priority 3 (Tactical Upgrade)**: Gen 2 N2 (`db-perf-optimized-N-2`) on **PD_SSD**.
   - **Priority 4 (Legacy Fallback)**: Gen 2 Custom (`db-custom-2-8192`) on **PD_SSD**.
2. **Coupled Storage Validation**: Automatically ensures Gen 4 shapes use `HYPERDISK_BALANCED` and Gen 2 shapes use `PD_SSD`, preventing `ERROR_PUBLIC_INVALID_TIER_FOR_EDITION` errors.
3. **2-Strike Zonal Circuit Breaker**: Trips after 2 consecutive stockout attempts in a zone and rotates to sibling zones across the region.
4. **Declarative Configuration**: All tiers, regions, and settings are decoupled into `config.yaml` for GitOps and CI/CD pipelines.

---

## Project Structure

```text
.
├── config.yaml          # Declarative tier hierarchy, zones, and project settings
├── deployer.py          # State-machine orchestrator with circuit breaker
├── requirements.txt     # Python dependencies
├── .gitignore           # Git ignore rules
├── LICENSE              # Apache 2.0 License
└── README.md            # Documentation and usage guide
```

---

## Quickstart

### 1. Prerequisites

- Python 3.10 or higher.
- Google Cloud SDK (`gcloud`) installed and authenticated:
  ```bash
  gcloud auth application-default login
  ```
- IAM Permissions: `Cloud SQL Admin` (`roles/cloudsql.admin`) on the target project.

### 2. Installation

Clone the repository and install dependencies:

```bash
git clone https://github.com/vishinde/cloudsqldeploy.git
cd cloudsqldeploy

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configuration

Edit `config.yaml` with your target project, region, and candidate tiers:

```yaml
project_id: "your-gcp-project-id"
region: "us-central1"
database_version: "POSTGRES_14"
base_instance_name: "prod-db"

preferred_zones:
  - "us-central1-a"
  - "us-central1-b"
  - "us-central1-c"
  - "us-central1-f"

circuit_breaker_max_strikes: 2
operation_timeout_sec: 600

candidate_tiers:
  - priority: 1
    name: "Gen 4 N4 (Recommended Strategic Default)"
    edition: "ENTERPRISE"
    tier: "db-custom-N4-2-8192"
    disk_type: "HYPERDISK_BALANCED"
    disk_size_gb: 50
    enable_data_cache: false

  - priority: 2
    name: "Gen 4 C4 Enterprise Plus (High Performance)"
    edition: "ENTERPRISE_PLUS"
    tier: "db-perf-optimized-C4-2"
    disk_type: "HYPERDISK_BALANCED"
    disk_size_gb: 50
    enable_data_cache: true
```

### 4. Running Deployments

#### Dry-Run Mode (Validation Only)
Validate configuration and print the execution plan without provisioning any cloud resources:

```bash
python3 deployer.py --dry-run
```

#### Production Deployment
Execute the deployment state machine:

```bash
python3 deployer.py
```

#### Custom Instance Name or Alternate Config
```bash
python3 deployer.py --config config.staging.yaml --name staging-db
```

---

## CI/CD Pipeline Integration

The deployer returns standard POSIX exit codes:
- `0`: Instance successfully provisioned.
- `1`: Deployment failed after all candidate tiers and zones were exhausted.

Example GitHub Actions step:

```yaml
- name: Deploy Resilient Cloud SQL
  run: |
    python3 deployer.py --config config.prod.yaml --name analytics-db
```

---

## License

This project is licensed under the Apache 2.0 License - see the [LICENSE](LICENSE) file for details.
