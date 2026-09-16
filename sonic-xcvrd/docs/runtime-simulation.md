# Runtime Transceiver Simulation

> Drive the full `xcvrd` insert/remove/DOM/CMIS pipelines at runtime on a
> switch or VS image — without real optics — by replacing the vendor
> `sonic_platform` plugin with a mock that listens to a Unix-domain-socket
> control channel.

---

## Why

When developing or debugging `xcvrd`, the SFP state-machine paths
(`SfpStateUpdateTask`, `DomInfoUpdateTask`, `CmisManagerTask`, the soak
debouncer in `_wrapper_soak_sfp_insert_event`) are reached only when a real
transceiver is inserted or removed. That is impractical for CI, for VS
images, and for reproducing field bugs.

`xcvrd` is already plugin-driven: at startup it does

```python
import sonic_platform.platform
platform_chassis = sonic_platform.platform.Platform().get_chassis()
```

and from then on it only calls the documented `Chassis` / `Sfp` API.
That means any package that exports `sonic_platform.platform.Platform`
satisfies the contract — including a mock that fakes hardware events on
demand.

## High-level architecture

```
+-------------------+        UDS         +---------------------------+
| xcvrd_simctl CLI  | ─── JSON cmd ────▶ | sonic_platform.MockChassis|
|  (insert/remove)  |                    |   - get_change_event()    |
+-------------------+                    |   - get_all_sfps()        |
                                         +---------------┬-----------+
                                                         │
                                                         ▼
+---------------------------------------------------------------------+
| xcvrd  (unchanged)                                                  |
|   _wrapper_get_transceiver_change_event() ─▶ SfpStateUpdateTask     |
|     ─▶ update_port_transceiver_status_table_sw / TRANSCEIVER_INFO   |
+---------------------------------------------------------------------+
```

## Package layout

The production deliverable lives in a standalone repo, `sonic-platform-mock`,
that ships the `sonic_platform` namespace as a wheel installed in the `pmon`
container:

| File                                | Purpose                                       |
| ----------------------------------- | --------------------------------------------- |
| `sonic_platform/__init__.py`        | Empty namespace package                       |
| `sonic_platform/platform.py`        | `Platform().get_chassis()` factory            |
| `sonic_platform/chassis.py`         | `MockChassis` with `get_change_event`         |
| `sonic_platform/sfp.py`             | `MockSfp` proxying to `SimState`              |
| `sonic_platform/sim_state.py`       | Singleton state + EEPROM blobs + event queue  |
| `sonic_platform/control_server.py`  | JSON-over-UDS dispatcher                      |
| `sonic_platform/profiles/*.bin`     | EEPROM templates per cable type               |
| `scripts/xcvrd_simctl`              | Console-script entry point                    |
| `setup.py`                          | Wheel build                                   |

A working reference implementation lives in this repo under
[`sonic-xcvrd/tests/runtime_sim/`](../tests/runtime_sim/) and is exercised
by [`sonic-xcvrd/tests/test_mock_runtime_simulation.py`](../tests/test_mock_runtime_simulation.py).
The files there are intentionally laid out so they can be lifted verbatim
into the external package.

## Installation

On a SONiC build:

```bash
# Build the mock wheel
cd sonic-platform-mock
python3 setup.py bdist_wheel

# Copy it into the pmon container
docker cp dist/sonic_platform_mock-*.whl pmon:/tmp/
docker exec pmon pip3 install --force-reinstall /tmp/sonic_platform_mock-*.whl
docker exec pmon supervisorctl restart xcvrd
```

For developers running `xcvrd` locally against the mock:

```bash
PYTHONPATH=$(pwd)/sonic-platform-mock MOCK_SFP_COUNT=32 \
    python3 -m xcvrd.xcvrd
```

## CLI usage

`xcvrd_simctl` is a thin client over the UDS:

```text
xcvrd_simctl insert <port> [--profile NAME]
xcvrd_simctl remove <port>
xcvrd_simctl error  <port> <code>
xcvrd_simctl dom    <port> <field> <value>
xcvrd_simctl list
xcvrd_simctl ping
```

Examples:

```bash
# Insert a 100G CWDM4 module into port 5
xcvrd_simctl insert 5 --profile qsfp28-100g-cwdm4

# Bump simulated temperature so the thermal monitor reacts
xcvrd_simctl dom 5 temperature 78.0

# Surface a hardware error
xcvrd_simctl error 5 blocking

# Pull the module
xcvrd_simctl remove 5
```

Exit code is `0` when the server responds `{"ok": true}` and non-zero
otherwise. The full response is printed to stdout as JSON.

### Supported EEPROM profiles

| Profile name              | Form factor | Notes                       |
| ------------------------- | ----------- | --------------------------- |
| `qsfp28-100g-cwdm4`       | QSFP28      | Default if `--profile` omitted |
| `qsfpdd-400g-dr4`         | QSFP-DD     | CMIS, identifier 0x18       |
| `sfp-1g-t`                | SFP         | SFF-8472, identifier 0x03   |
| `qsfp-passive-copper`     | QSFP+       | Identifier 0x0D             |

Vendor name / OUI / part number / serial number are filled with `MOCK …`
strings so they're obvious in `show interfaces transceiver eeprom`.

### Supported error codes

`blocking`, `i2c_stuck`, `bad_eeprom`, `unsupported_cable`, `high_temp`,
`bad_cable`. These map directly onto the `sfp_status_helper.SFP_STATUS_ERR_*`
strings consumed by xcvrd.

## Verifying behavior in Redis

```bash
# On the switch
redis-cli -n 6 keys 'TRANSCEIVER_INFO|*'
redis-cli -n 6 hgetall 'TRANSCEIVER_INFO|Ethernet16'
redis-cli -n 6 hget 'TRANSCEIVER_STATUS_SW|Ethernet16' status
```

After `xcvrd_simctl insert 5`, the corresponding `TRANSCEIVER_INFO|EthernetN`
key should appear within one polling cycle
(`STATE_MACHINE_UPDATE_PERIOD_MSECS`, currently 60 s) and disappear after
`xcvrd_simctl remove 5`.

## Troubleshooting

| Symptom                              | Likely cause / fix                                                       |
| ------------------------------------ | ------------------------------------------------------------------------ |
| `Connection refused` from `xcvrd_simctl` | The mock didn't start the UDS — check `journalctl -u pmon` for tracebacks. |
| `xcvrd` doesn't react to inserts     | A real vendor `sonic_platform` is still on `PYTHONPATH`. `python3 -c 'import sonic_platform.platform as p; print(p.__file__)'` to confirm. |
| Insert events delayed ~2 s           | Expected — `_wrapper_soak_sfp_insert_event` debounces inserts by `MGMT_INIT_TIME_DELAY_SECS`. |
| `xcvrd` reports `SYSTEM_FAIL` retries | Mock returned `(False, …)` from `get_change_event`. The mock must always return `(True, …)`. |
| `MOCK_SFP_COUNT` ignored             | The `SimState` singleton was created before the env var was set. Restart the pmon container. |

## Security notes

* The UDS file is created with mode `0o660` so only members of the configured
  group can connect.
* All input is parsed with `json.loads`; there is no `eval`, `exec`, or
  shell invocation anywhere in the dispatcher. The set of operations is a
  static dict.
* Payloads larger than `MAX_MSG_BYTES` (4 KiB) are rejected before parsing.
* Port indices and profile names are validated against the configured
  `MOCK_SFP_COUNT` and a static whitelist respectively.

## Why no changes to `xcvrd.py`?

The `xcvrd` daemon already consumes exactly the API the mock implements:

* `_wrapper_get_transceiver_change_event`
  ([`xcvrd.py:141-151`](../xcvrd/xcvrd.py)) calls
  `platform_chassis.get_change_event(timeout)` and forwards `events['sfp']`
  / `events['sfp_error']` straight into `SfpStateUpdateTask`.
* The handler loop accepts any `(physical_port, '0'|'1')` pair and runs the
  full insert/remove pipeline (DOM, CMIS, SFF managers).
* The soak / debounce path is also data-driven and unmodified.

So the mock simply has to honor the existing contract — and that's exactly
what the reference implementation under `tests/runtime_sim/` does.

## Running the tests

```bash
cd sonic-xcvrd
pytest tests/test_mock_runtime_simulation.py -v
```

These tests are part of the standard `pytest` suite and run on every CI
build of `sonic-platform-daemons`. They guard the API contract that the
external `sonic-platform-mock` package must continue to satisfy.
