# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

This repository is a YANG model library for an OpenROADM NETCONF Zero Touch Provisioning (ZTP) tool. It contains ~770 YANG model files organized by standards body. There is currently no application code — the repository is a curated collection of YANG data models.

## Working with YANG Models

The recommended tool for validating and manipulating YANG models is [`pyang`](https://github.com/mbj4668/pyang). The project has a Python 3.14 virtual environment at `.venv/`.

```bash
# Activate virtual environment
source .venv/bin/activate

# Validate a YANG module
pyang models/openroadm/Common/org-openroadm-alarm.yang

# Generate a tree view
pyang -f tree models/openroadm/Common/org-openroadm-alarm.yang

# Validate with a search path (needed for imports)
pyang -p models/ietf -p models/openconfig -p models/openroadm/Common models/openroadm/Device/org-openroadm-bgp.yang
```

## Model Organization

```
models/
  ietf/           IANA and IETF standard YANG modules
  openconfig/     OpenConfig modules, one subdirectory per feature/protocol
  openroadm/
    Common/       Shared OpenROADM types, alarms, PM, resources
    Device/       OpenROADM device-specific models + telemetry proto
```

### File naming convention

Each module exists in two forms:
- `module-name.yang` — unversioned (latest) copy
- `module-name@YYYY-MM-DD.yang` — revision-date-stamped copy

Both files are kept in sync; the stamped version is what YANG `import` statements reference via `revision-date`.

### OpenConfig `.spec.yml`

Each OpenConfig feature directory contains a `.spec.yml` that declares the module's primary build targets and CI participation:

```yaml
- name: openconfig-bgp
  docs:
    - yang/bgp/openconfig-bgp-types.yang
    - yang/bgp/openconfig-bgp.yang
  build:
    - yang/bgp/openconfig-bgp.yang
  run-ci: true
```

### OpenROADM tree views

Pre-generated `pyang` tree output is checked in alongside the models:
- `models/openroadm/Common/tree-view-common.txt`
- `models/openroadm/Device/tree-view-device.txt`

Update these after modifying OpenROADM models:
```bash
pyang -f tree -p models/ietf -p models/openroadm/Common \
  models/openroadm/Common/*.yang > models/openroadm/Common/tree-view-common.txt
```
