# GPU Power / Clock Efficiency Tuner

Automated clock-sweep and efficiency characterisation tool for NVIDIA GPUs.
Locks the GPU to each clock frequency in sequence, runs a real compute workload (matrix multiplication), measures power and throughput, and plots the **MHz/W** and **ops/W** efficiency curves so you can find the exact clock limit that maximises work-per-watt for your card.

Built and tested on a **Gigabyte Aorus GeForce RTX 5090 Xtreme Waterforce 32G **, tunable to any NVIDIA GPU supported by `nvidia-smi`.

---

## Features

- **Auto-detects** clock range and stock power limit from `nvidia-smi` — no manual config needed
- **Two efficiency metrics** plotted side-by-side: MHz/W (clock proxy) and ops/W (real throughput)
- **Physics-based thermal model** derived from baseline data; estimates required cooldown time per step
- **Two-phase cooldown protocol** — waits for die temperature *and* heatsink bulk stabilisation before each step, eliminating measurement contamination from accumulated heat
- **Inverted sweep mode** (`--inverted`) — sweeps low→high to visualise cumulative thermal warmup
- Prints ready-to-paste `nvidia-smi` and `nvidia-settings` commands for the recommended settings at the end of every run
- Safe: fans are set to 100% before any measurement, stock settings are always restored in the `finally` block

---

## Requirements

| Dependency | Purpose |
|---|---|
| NVIDIA GPU with `nvidia-smi` | Clock locking (`-lgc`), power limits (`-pl`), stats |
| PyTorch with CUDA | Compute workload (8192×8192 FP32 matmul) |
| `matplotlib` | Result plots |
| `nvidia-settings` + Coolbits=4 | Fan speed override (optional but strongly recommended) |
| `sudo` access | `nvidia-smi -lgc` and `-pl` require root |

```bash
create venv:
uv venv venv --python 3.14
source ./venv/bin/activate
uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu132
uv pip install matplotlib
```

### Coolbits fan control (one-time setup)

Without Coolbits the fans stay on the auto-curve during the sweep, which can cause thermal throttling and corrupt readings at high power levels.

```bash
sudo nvidia-xconfig --cool-bits=4
sudo systemctl restart gdm3   # closes all open apps — save your work first
```

The script will detect whether Coolbits is active and warn you if it isn't.

---

## Quick Start

**1 — List available GPUs**

```bash
python tune_and_graph.py --list-gpus
```

```
ID  Name                          Max Clock   Default PL   Max PL
 0  NVIDIA GeForce RTX 5090       3000 MHz      600.0 W    600.0 W
```

**2 — Run a full sweep on GPU 0**

```bash
python tune_and_graph.py --target 0
```

The script auto-detects the clock range and power limit. Everything else defaults to safe values.

**3 — Full sweep with recommended thermal parameters for a high-TDP card**

```bash
python tune_and_graph.py --target 0 \
  --clock-step 100 --clock-start 3000 --clock-min 1000 \
  --test-duration 10 --power-limit 600 \
  --cool-period 90 --cool-temp 35 --stable-window 30
```

---

## All Flags

| Flag | Default | Description |
|---|---|---|
| `--list-gpus` | — | Print GPU table and exit |
| `--target ID` | — | GPU index to tune (required) |
| `--target-power W` | `0` | Stop early when measured power ≤ this value. `0` = full sweep |
| `--clock-start MHz` | auto | Starting clock. Defaults to GPU max boost from nvidia-smi |
| `--clock-min MHz` | auto | Lowest clock to test. Defaults to GPU minimum from nvidia-smi |
| `--clock-step MHz` | `100` | Step size between clock points |
| `--test-duration SEC` | `5` | Seconds to run at each clock before sampling |
| `--power-limit W` | auto | Power limit applied for the sweep. Defaults to stock value |
| `--cool-period SEC` | `30` | Minimum wait after each step before polling temperature |
| `--cool-temp °C` | `40` | Temperature ceiling — next step won't start above this |
| `--stable-window SEC` | `30` | Seconds of <0.5°C drift required to confirm heatsink equilibration |
| `--inverted` | off | Sweep low→high instead of high→low |

---

## Sweep Modes: Default vs Inverted

The tool supports two opposite sweep directions, each useful for different things.

### Default (High → Low)

```bash
python tune_and_graph.py --target 0 \
  --clock-step 100 --clock-start 3000 --clock-min 1000 \
  --test-duration 10 --power-limit 600 \
  --cool-period 90 --cool-temp 35 --stable-window 30
```

- Starts at max clock (highest power), steps down
- Early steps accumulate heat; good cooldown parameters are essential
- Output: `rtx5090_efficiency_curve.png`

### Inverted (Low → High)

```bash
python tune_and_graph.py --target 0 \
  --clock-step 100 --clock-start 3000 --clock-min 1000 \
  --test-duration 10 --power-limit 600 \
  --cool-period 90 --cool-temp 35 --stable-window 30 \
  --inverted
```

- Starts at min clock (lowest power), steps up
- **Enforces 120s post-baseline cooldown** (regardless of `--cool-period`) to fully expel baseline heat before the ascending sweep begins
- Shows cumulative thermal warmup as clocks increase — useful for diagnosing whether your cooldown parameters are adequate
- Output: `rtx5090_efficiency_curve_inverted.png`

Both graphs use the same 4-panel layout with the x-axis sorted low→high, so they overlay visually for direct comparison.

> **Interpretation:** If the temperature panel in the inverted graph still trends upward across the sweep despite 90s cool periods, the heatsink bulk τ is longer than 90s for your cooler and ambient — increase `--cool-period` or reduce `--test-duration`.

---

## The Math

### Efficiency Metrics

The tool computes two independent efficiency figures at each clock step:

**MHz/W** — clock efficiency (proxy metric)

$$\text{MHz/W} = \frac{\text{actual\_clock (MHz)}}{\text{avg\_power (W)}}$$

This tells you how many MHz you get per watt. It's a fast, stable measurement but doesn't account for how efficiently those MHz translate into real work — at high clocks the GPU runs its memory subsystem and fixed-function units at full power even if the compute throughput doesn't scale linearly.

**ops/W** — throughput efficiency (true metric)

$$\text{ops/W} = \frac{\text{matmuls/sec}}{\text{avg\_power (W)}}$$

This measures actual compute throughput (8192×8192 FP32 matrix multiplications per second) divided by wall power. It captures real-world efficiency, including the overhead of the memory controller, voltage regulators, and idle logic that doesn't scale with clock frequency.

The two metrics often disagree — **ops/W peaks at a lower clock than MHz/W** because the marginal watt cost of pushing from a mid-range clock to the boost ceiling is high while the throughput gain is small.

---

### Thermal Model

The script derives a first-order thermal model from the **baseline measurement** (full-clock, full-power run before the sweep). This model is used to:

1. Estimate how long each step needs to cool before the next one
2. Warn you if your `--cool-period` is too short
3. Explain why the heatsink bulk is slower than the die sensor

#### Thermal resistance

$$R_{\text{thermal}} = \frac{T_{\text{load}} - T_{\text{idle}}}{P_{\text{load}}} \quad \left[\frac{°C}{W}\right]$$

For the RTX 5090 AORUS at 567W with fans at 100%:

$$R_{\text{thermal}} = \frac{46.6 - 30}{567} \approx 0.0293 \; °C/W$$

#### Time constant

$$\tau = R_{\text{thermal}} \times C_{\text{heatsink}}$$

Where $C_{\text{heatsink}} \approx 950 \; J/K$ is the thermal mass of a large triple-fan vapour-chamber cooler (Al fins + Cu heatpipes, empirically derived).

$$\tau \approx 0.0293 \times 950 \approx 28 \; s$$

This is the **fast** time constant of the GPU die and cold-plate junction. The die sensor (what `nvidia-smi` reports) follows this closely.

#### Two time constants

The cooler behaves as two thermal masses in series:

| Component | τ | What it represents |
|---|---|---|
| Die + cold plate | **~3–5 s** | GPU die + vapour chamber evaporator; what `nvidia-smi` reads |
| Bulk heatsink fins | **~90–150 s** | Al fin stack + heatpipes; accumulates heat across steps |

The die sensor drops to near-idle in under 20 seconds after load stops — but the heatsink bulk can hold stored heat for 2–3 minutes. If the next step starts before the bulk has equilibrated, its residual temperature raises the die's thermal floor, causing artificially elevated readings at later steps.

This was directly observed in a run with `--cool-period 10`: between-step temperatures climbed from 30°C to 34°C over the first nine 550W steps (~180 s total), fitting $\tau_{\text{slow}} = 180 / \ln(5) \approx 112 \; s$.

#### Cooldown time estimate

Given a step temperature $T_{\text{step}}$, the estimated time to cool to $T_{\text{target}}$ is:

$$t_{\text{cool}} = \tau \cdot \ln \left( \frac{T_{\text{step}} - T_{\text{idle}}}{T_{\text{target}} - T_{\text{idle}}} \right)$$

The script prints this estimate after every step and flags a warning if `--cool-period` is less than half the estimated need.

---

### Two-Phase Cooldown Protocol

After each step the script runs two sequential gates before starting the next one:

```
Step finishes → stop stress process
     │
     ▼
Phase 1: sleep(cool_period), then poll until temp ≤ cool_temp
     │   Die sensor drops here. Fast.
     ▼
Phase 2: collect stable_window / poll_interval readings
         require |first − last| < 0.5°C
         if still drifting → slide window and keep waiting
     │   Heatsink bulk is still releasing heat here.
     ▼
Start next step
```

**Why the stability gate handles variable `--test-duration`:**

Longer test durations dump more energy into the heatsink bulk ($E = P \times t_{\text{test}}$). After stopping, the bulk takes proportionally longer to equilibrate. The stability gate automatically extends the wait until the temperature derivative falls below threshold — no manual calculation needed.

---

## Interpreting the Graph

The output is a 4-panel PNG with a shared clock x-axis (ascending, low→high):

| Panel | What to look for |
|---|---|
| **Power (W)** | Should be flat at the power limit cap for high clocks, then slope down as you go below the GPU's preferred boost range |
| **Temp (°C)** | Should be roughly flat if cooldowns are adequate. A rising trend across the sweep indicates heatsink bulk warmup contaminating readings |
| **MHz/W** | Usually peaks at the lowest tested clock — marginal power cost of the last few hundred MHz is disproportionate |
| **ops/W** | Often peaks slightly higher than MHz/W but still well below max boost — this is the most useful number for setting a power limit |

The baseline measurement (taken at `--clock-start`) is plotted as a horizontal dashed reference line on each panel.

---

## Applying the Recommended Settings

At the end of each run the script prints ready-to-paste commands, for example:

```
  Best MHz/W  →  1100 MHz  (4.61 MHz/W, measured 239.0W)
  Best ops/W  →  1200 MHz  (0.1277 ops/W, measured 259.5W)

  Apply best MHz/W setting:
    sudo nvidia-smi -i 0 -lgc 1100,1100
    sudo nvidia-smi -i 0 -pl 239

  Apply best ops/W setting:
    sudo nvidia-smi -i 0 -lgc 1200,1200
    sudo nvidia-smi -i 0 -pl 260

  Reset to stock:
    sudo nvidia-smi -i 0 -rgc
    sudo nvidia-smi -i 0 -pl 600
```

> **Note:** `-lgc min,max` requires both values even when locking to a single clock. The script always writes `clock,clock`.

---

## Acknowledgements

Thermal model and dual-metric approach developed through iterative testing on the RTX 5090 AORUS. The two-time-constant heatsink model (fast die junction + slow bulk fins) and physics-based cooldown estimation are derived from measured data from actual sweep runs.
