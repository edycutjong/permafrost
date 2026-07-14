# Permafrost — edge wiring (Raspberry Pi rig)

> The Pi is **showmanship, not a dependency**. Judges reproduce the whole loop with
> zero hardware via `permafrost replay` (see [`../DEMO.md`](../DEMO.md)). This file is
> the hardware-story credibility for the video, and the pin map the daemon expects.
>
> **Status:** `GpioSource` / buzzer-GPIO are written with **guarded imports** and match
> this plan, but no physical rig has been exercised in this build (README Status). On a
> non-Pi machine the GPIO libraries are never imported.

## Bill of materials (SPEC §4) — ≈ $60 vs $1–3k/yr commercial monitors

| Qty | Part | Notes | ~USD |
|---|---|---|---|
| 1 | Raspberry Pi 3B+/4/5 (any on hand) | commodity, widely-available hardware (rules exempt commodity HW from the physical-access clause — stated in README) | on hand / 35 |
| 2 | DS18B20 waterproof temperature probes | 1-Wire, one in the cabinet, one as ambient reference; median-of-N rejects sensor noise | 8 |
| 1 | 4.7 kΩ resistor | 1-Wire pull-up, DATA→3V3 | 0.10 |
| 1 | Magnetic reed switch | door open/closed sense | 3 |
| 1 | Piezo buzzer **or** relay module | the local siren | 2 |
| 1 | USB power bank / UPS HAT | holds the Pi through a mains blackout (the power-cut demo) | 12 |
| 1 | Any mini fridge | the prop; not instrumented beyond the probes | on hand |

## Pin map (BCM numbering — matches `permafrost daemon` defaults)

| Signal | BCM pin | Physical | Wiring |
|---|---|---|---|
| DS18B20 data (both probes, shared bus) | **GPIO4** | pin 7 | DATA line, with the 4.7 kΩ pull-up to 3V3 |
| DS18B20 VDD | 3V3 | pin 1 | both probes |
| DS18B20 GND | GND | pin 9 | both probes |
| Door reed switch | **GPIO17** | pin 11 | `Button(17, pull_up=True)`; other leg to GND |
| Mains-present sense | **GPIO27** | pin 13 | opto/relay from a USB mains adapter; `power_ok = not pressed` |
| Piezo buzzer | **GPIO18** | pin 12 | `Buzzer(18)`; buzzer + to pin, − to GND |

Defaults live in `permafrost daemon --door-pin 17 --power-pin 27 --buzzer-pin 18`
(`src/permafrost/cli.py`) and `GpioSource(door_pin=17, power_pin=27)`
(`src/permafrost/sampler.py`).

## Assembly notes

1. **Enable 1-Wire**: `sudo raspi-config` → Interface Options → 1-Wire → reboot. Confirm
   both probes enumerate: `ls /sys/bus/w1/devices/28-*`.
2. **Pull-up matters**: the single 4.7 kΩ from DATA (GPIO4) to 3V3 serves both probes on
   the shared bus. Missing it = intermittent `-127 °C` reads.
3. **Probe placement**: cabinet probe sits **centrally among the vials** (per cold-chain
   guidance, not against a wall or coil); the reed switch bridges door and frame.
4. **Mains sense**: a small 5 V USB adapter into an opto-isolated input on GPIO27 lets
   the Pi (held up by the power bank) *know* mains dropped — that's the `power` reflex
   rule and the `power_loss` diagnosis signal, distinct from the telemetry gap.
5. **Run it**: `permafrost daemon --db audit.db --cloud-url https://<fc-endpoint>`. With
   no `--cloud-url` the daemon still samples, reflexes, and hash-chains fully offline.

## Software mapping

| Hardware event | Reading field | Reflex rule (rules_v1) | Cloud cause |
|---|---|---|---|
| door reed opens | `door_open=True` | `door_timer` (>120 s) | `door_ajar` |
| cabinet warms fast | `temp_c` slope | `fast_rise` (≥0.5 °C/min) | door / defrost split |
| mains adapter drops | `power_ok=False` | `power_out` | `power_loss` |
| Pi dark then back | telemetry gap | `sample_gap` | `power_loss` |
| slow multi-day rise | daily-mean drift | `slow_drift` (≥0.25 °C/day) | `compressor_degradation` |

Sensor spoofing at the physical layer (a heat gun on a probe) is **out of scope** and
stated in-product; diagnosis infers from curves and cannot see refrigerant pressure.
