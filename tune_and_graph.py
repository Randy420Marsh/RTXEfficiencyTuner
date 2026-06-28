#!/usr/bin/env python3
import subprocess
import time
import multiprocessing
import sys
import os
import importlib.util
import re
import argparse
import math

try:
    import matplotlib.pyplot as plt
except ImportError:
    print("[Error] Matplotlib is required. Install it using: pip install matplotlib")
    sys.exit(1)

# Fallback defaults used when CLI flags are not provided.
# Clock range and power limit are auto-detected from nvidia-smi at runtime.
TEST_DURATION = 5    # seconds per step
CLOCK_STEP    = 100  # MHz per step

# Ensure we have a display variable for nvidia-settings to hook into X11
if "DISPLAY" not in os.environ:
    os.environ["DISPLAY"] = ":0"


def set_fan_speed(gpu_id, speed_percent):
    """Takes control of the fan curve and forces a static speed."""
    print(f"-> Overriding GPU fan curve... forcing fans to {speed_percent}%")
    subprocess.run(["sudo", "nvidia-settings", "-a", f"[gpu:{gpu_id}]/GPUFanControlState=1"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["sudo", "nvidia-settings", "-a", f"[fan:0]/GPUTargetFanSpeed={speed_percent}"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reset_fan_speed(gpu_id):
    """Returns the fans to their default hardware auto-curve."""
    print("-> Releasing GPU fan control to default auto-curve...")
    subprocess.run(["sudo", "nvidia-settings", "-a", f"[gpu:{gpu_id}]/GPUFanControlState=0"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def gpu_stress_test(ready_event, iteration_counter):
    """Generates continuous GPU load using PyTorch.

    Signals `ready_event` once the GPU is actually crunching, so the parent can
    confirm the load is live before it starts trusting any measurements.
    Increments `iteration_counter` after each completed kernel so the parent can
    compute throughput (ops/sec) during the measurement windows.
    """
    try:
        import torch
    except ImportError:
        print("[Stress] PyTorch not found in the worker; GPU will stay idle.", file=sys.stderr)
        return

    try:
        if not torch.cuda.is_available():
            print("[Stress] torch.cuda.is_available() is False; no CUDA device visible.", file=sys.stderr)
            return

        device = torch.device("cuda:0")
        size = 8192
        a = torch.rand((size, size), device=device)
        b = torch.rand((size, size), device=device)

        # Prime once and confirm the kernel actually executed before signalling.
        torch.matmul(a, b)
        torch.cuda.synchronize()
        ready_event.set()

        while True:
            torch.matmul(a, b)
            torch.cuda.synchronize()
            with iteration_counter.get_lock():
                iteration_counter.value += 1
    except Exception as e:
        # Catch CUDA OOM / driver / device errors too, not just ImportError,
        # so a dead worker is visible instead of silently leaving the GPU idle.
        print(f"[Stress] GPU stress worker crashed: {e}", file=sys.stderr)


def get_gpu_stats(gpu_id):
    """Returns (power_w, temp_c, clock_mhz). Returns zeros on a failed read."""
    result = subprocess.run(
        ["nvidia-smi", "-i", str(gpu_id),
         "--query-gpu=power.draw,temperature.gpu,clocks.current.graphics",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True
    )
    try:
        parts = result.stdout.strip().split(',')
        return float(parts[0]), float(parts[1]), float(parts[2])
    except Exception:
        return 0.0, 0.0, 0.0


def run_step(gpu_id, duration_sec, iteration_counter=None):
    """Waits for stabilization, then averages valid samples over the last 5s.

    Returns (avg_power_w, avg_temp_c, avg_clock_mhz, ops_per_sec, ops_per_watt).
    ops_per_sec is the 8192×8192 FP32 matmul throughput measured over the sampling
    window; ops_per_watt is throughput divided by average power — the work/W metric.
    Both are 0.0 when no iteration_counter is provided.
    """
    # Always wait at least 3 s for the clock lock to propagate and stabilise
    # before sampling. With very short test durations (<=5 s) the old formula
    # produced 0 s of stabilisation, so the first step was measured mid-transition.
    time.sleep(max(3, duration_sec - 5))

    powers, temps, clocks = [], [], []

    with iteration_counter.get_lock():
        iter0 = iteration_counter.value
    t0 = time.monotonic()

    for _ in range(5):
        p, t, c = get_gpu_stats(gpu_id)
        # Discard failed/invalid reads so a transient nvidia-smi hiccup can't
        # drag the averages down (or, worse, falsely trip the target check).
        if p > 0:
            powers.append(p)
            temps.append(t)
            clocks.append(c)
        time.sleep(1)

    with iteration_counter.get_lock():
        iter1 = iteration_counter.value
    elapsed = time.monotonic() - t0

    if not powers:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    n = len(powers)
    avg_p = sum(powers) / n
    ops_per_sec = (iter1 - iter0) / elapsed if elapsed > 0 else 0.0
    ops_per_watt = ops_per_sec / avg_p if avg_p > 0 else 0.0
    return avg_p, sum(temps) / n, sum(clocks) / n, ops_per_sec, ops_per_watt


def plot_results(baseline, steps, target_power=0.0, inverted=False):
    """Generates a 4-panel graph: power, temperature, MHz/W, and ops/W."""
    # Sort ascending so lines plot cleanly left-to-right regardless of sweep direction.
    valid_steps = sorted([s for s in steps if s['power'] > 0], key=lambda s: s['target_clock'])

    target_clocks = [s['target_clock'] for s in valid_steps]
    powers       = [s['power']       for s in valid_steps]
    temps        = [s['temp']        for s in valid_steps]
    mhz_per_w    = [s['mhz_per_w']   for s in valid_steps]
    ops_per_w    = [s['ops_per_w']   for s in valid_steps]

    direction = "Low→High (Inverted)" if inverted else "High→Low"
    fig, (ax1, ax2, ax3, ax4) = plt.subplots(4, 1, figsize=(10, 16), sharex=True)
    fig.suptitle(f'RTX 5090 Power / Clock Efficiency Curve  [{direction}]', fontsize=16)

    ax1.plot(target_clocks, powers, marker='o', color='red', label='Power (W)')
    ax1.axhline(y=baseline['power'], color='darkred', linestyle='--', label=f"Baseline ({baseline['power']:.0f}W)")
    if target_power > 0:
        ax1.axhline(y=target_power, color='green', linestyle=':', label=f"Target ({target_power}W)")
    ax1.set_ylabel('Power (Watts)')
    ax1.legend()
    ax1.grid(True)

    ax2.plot(target_clocks, temps, marker='s', color='orange', label='Temp (°C)')
    ax2.axhline(y=baseline['temp'], color='darkorange', linestyle='--', label=f"Baseline ({baseline['temp']:.1f}°C)")
    ax2.set_ylabel('Temperature (°C)')
    ax2.legend()
    ax2.grid(True)

    ax3.plot(target_clocks, mhz_per_w, marker='^', color='blue', label='MHz/W')
    ax3.axhline(y=baseline['mhz_per_w'], color='darkblue', linestyle='--',
                label=f"Baseline ({baseline['mhz_per_w']:.2f} MHz/W)")
    ax3.set_ylabel('Clock Efficiency (MHz/W)')
    ax3.legend()
    ax3.grid(True)

    ax4.plot(target_clocks, ops_per_w, marker='D', color='purple', label='ops/W (matmuls/W)')
    ax4.axhline(y=baseline['ops_per_w'], color='indigo', linestyle='--',
                label=f"Baseline ({baseline['ops_per_w']:.4f} ops/W)")
    ax4.set_xlabel('Target Clock Limit (MHz)')
    ax4.set_ylabel('Throughput Efficiency (ops/W)')
    ax4.legend()
    ax4.grid(True)

    plt.tight_layout()
    fname = 'rtx5090_efficiency_curve_inverted.png' if inverted else 'rtx5090_efficiency_curve.png'
    plt.savefig(fname)
    print(f"\nGraph saved as '{fname}'.")

    # The PNG is the real deliverable. Don't crash after a long tuning run just
    # because there's no reachable display (e.g. running over SSH).
    try:
        plt.show()
    except Exception:
        pass


def list_gpus():
    """Print a table of all detected NVIDIA GPUs and exit."""
    result = subprocess.run(
        ["nvidia-smi",
         "--query-gpu=index,name,clocks.max.graphics,power.default_limit,power.max_limit",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        print("[Error] nvidia-smi returned no GPU data.")
        sys.exit(1)
    print(f"\n{'ID':<4}  {'Name':<40}  {'Max Clock':>10}  {'Default PL':>10}  {'Max PL':>8}")
    print("-" * 78)
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(',')]
        if len(parts) < 5:
            continue
        idx, name, max_clk, def_pl, max_pl = parts
        print(f"{idx:<4}  {name:<40}  {max_clk:>8} MHz  {def_pl:>8} W    {max_pl:>6} W")
    print()


def get_gpu_limits(gpu_id):
    """Auto-detect max clock, lowest supported clock, and default power limit.

    Returns (max_clock_mhz, min_clock_mhz, default_power_limit_w).
    """
    result = subprocess.run(
        ["nvidia-smi", "-i", str(gpu_id),
         "--query-gpu=clocks.max.graphics,power.default_limit",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True
    )
    try:
        parts = [p.strip() for p in result.stdout.strip().split(',')]
        max_clock = int(float(parts[0]))
        default_pl = float(parts[1])
    except (ValueError, IndexError):
        print(f"[Error] Could not read clock/power limits for GPU {gpu_id}.")
        sys.exit(1)

    # Lowest supported graphics clock — take the last entry from the supported list.
    result2 = subprocess.run(
        ["nvidia-smi", "-i", str(gpu_id),
         "--query-supported-clocks=gr",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True
    )
    clocks = []
    for line in result2.stdout.strip().splitlines():
        line = line.strip()
        if line.isdigit():
            clocks.append(int(line))
    min_clock = min(clocks) if clocks else 200

    return max_clock, min_clock, default_pl


def parse_args():
    parser = argparse.ArgumentParser(
        prog="tune_and_graph.py",
        description=(
            "GPU power/clock efficiency sweeper.\n"
            "Locks clocks in steps from max to min, measures power and throughput\n"
            "at each point, and plots the MHz/W and ops/W efficiency curves."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "workflow:\n"
            "  1. List available GPUs:\n"
            "       python tune_and_graph.py --list-gpus\n\n"
            "  2. Sweep GPU 0, stop when power drops to 250 W:\n"
            "       python tune_and_graph.py --target 0 --target-power 250\n\n"
            "  3. Full sweep with coarser steps and custom clock range:\n"
            "       python tune_and_graph.py --target 0 --clock-step 100 \\\n"
            "           --clock-start 2500 --clock-min 800\n\n"
            "  Clock range and power limit default to values reported by nvidia-smi\n"
            "  for the selected GPU when not specified explicitly.\n"
        ),
    )
    parser.add_argument(
        "--list-gpus", action="store_true",
        help="List all detected NVIDIA GPUs with their IDs and exit.",
    )
    parser.add_argument(
        "--target", type=int, default=None, metavar="ID",
        help="GPU index to tune (from --list-gpus). Required unless --list-gpus.",
    )
    parser.add_argument(
        "--target-power", type=float, default=0.0, metavar="W",
        help=(
            "Stop the sweep early when measured power drops to or below this value (W). "
            "0 = sweep the full range without an early stop. (default: 0)"
        ),
    )
    parser.add_argument(
        "--clock-start", type=int, default=None, metavar="MHz",
        help="Starting clock in MHz. Defaults to the GPU's max boost clock from nvidia-smi.",
    )
    parser.add_argument(
        "--clock-min", type=int, default=None, metavar="MHz",
        help="Lowest clock to test in MHz. Defaults to the GPU's minimum supported clock.",
    )
    parser.add_argument(
        "--clock-step", type=int, default=CLOCK_STEP, metavar="MHz",
        help=f"Clock step size in MHz per iteration. (default: {CLOCK_STEP})",
    )
    parser.add_argument(
        "--test-duration", type=int, default=TEST_DURATION, metavar="SEC",
        help=f"Seconds to run at each clock step. (default: {TEST_DURATION})",
    )
    parser.add_argument(
        "--power-limit", type=float, default=None, metavar="W",
        help=(
            "Power limit to apply during the baseline and sweep phases (W). "
            "Defaults to the GPU's stock power limit from nvidia-smi."
        ),
    )
    parser.add_argument(
        "--cool-period", type=int, default=30, metavar="SEC",
        help=(
            "Seconds to wait after each step (and after baseline) before starting "
            "the next run. The script also polls until temp drops below --cool-temp "
            "before proceeding. (default: 30)"
        ),
    )
    parser.add_argument(
        "--cool-temp", type=float, default=40.0, metavar="C",
        help=(
            "GPU temperature ceiling in °C. The script will not start the next step "
            "until the GPU is at or below this temperature. (default: 40.0)"
        ),
    )
    parser.add_argument(
        "--stable-window", type=int, default=30, metavar="SEC",
        help=(
            "Seconds of temperature stability required before proceeding after each "
            "cooldown. The GPU die sensor drops fast, but the heatsink bulk keeps "
            "releasing stored heat — requiring stability (< 0.5°C drift over this "
            "window) confirms actual thermal equilibration. 0 = disable. (default: 30)"
        ),
    )

    parser.add_argument(
        "--inverted", action="store_true",
        help=(
            "Sweep from lowest clock to highest instead of highest to lowest. "
            "Useful for observing cumulative thermal warmup as clocks increase. "
            "The baseline is always taken at --clock-start (the high reference), "
            "then the sweep starts cold at --clock-min and steps up. "
            "A hard 120s post-baseline cooldown is enforced to fully expel heat "
            "before the ascending sweep begins."
        ),
    )

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()

    if not args.list_gpus and args.target is None:
        parser.error("--target is required. Use --list-gpus to see available GPU IDs.")

    return args


def wait_for_cool(gpu_id, target_temp=40.0, cool_period=0,
                  stable_window=30, poll_interval=5):
    """Wait cool_period s, then poll until GPU temp ≤ target_temp AND stable.

    Two-phase approach:
      Phase 1 — wait for the die sensor to drop below target_temp.
      Phase 2 — require the reading to drift < 0.5°C over stable_window seconds.

    The stability gate matters because the GPU die sensor drops in seconds (small
    thermal mass), but the heatsink bulk — vapor chamber, fins, heatpipes — keeps
    releasing stored heat back into the die.  A still-declining reading means the
    system hasn't equilibrated and the next step would start from a hotter baseline.
    """
    if cool_period > 0:
        print(f"-> Cooling down ({cool_period}s minimum)...", flush=True)
        time.sleep(cool_period)

    # Phase 1: wait below threshold
    while True:
        _, temp, _ = get_gpu_stats(gpu_id)
        if temp <= target_temp:
            break
        print(f"   {temp:.1f}°C > {target_temp:.0f}°C — waiting {poll_interval}s...", flush=True)
        time.sleep(poll_interval)

    if stable_window <= 0:
        _, temp, _ = get_gpu_stats(gpu_id)
        print(f"-> GPU {temp:.1f}°C — ready.")
        return

    # Phase 2: stability gate
    n_required = max(2, stable_window // poll_interval)
    history = []
    print(f"-> Below {target_temp:.0f}°C — checking heatsink stability "
          f"({stable_window}s window, need < 0.5°C drift)...", flush=True)

    while True:
        _, temp, _ = get_gpu_stats(gpu_id)

        if temp > target_temp:
            history.clear()
            print(f"   {temp:.1f}°C crept above target — resetting window.", flush=True)
        else:
            history.append(temp)

        if len(history) >= n_required:
            window = history[-n_required:]
            drift = window[0] - window[-1]   # positive = still dropping
            if abs(drift) < 0.5:
                print(f"-> GPU {temp:.1f}°C — heatsink stable. Proceeding.")
                return
            history.pop(0)                   # slide window forward
            print(f"   {temp:.1f}°C — still settling "
                  f"({drift:+.1f}°C over {stable_window}s)...", flush=True)

        time.sleep(poll_interval)


def launch_stress(iteration_counter):
    """Start a fresh stress worker and wait up to 30 s for the GPU load to confirm.

    Returns the Process on success, or None if the worker never signalled ready
    (which means the GPU stayed idle and measurements would be meaningless).
    """
    ready_event = multiprocessing.Event()
    process = multiprocessing.Process(target=gpu_stress_test, args=(ready_event, iteration_counter))
    process.start()
    if not ready_event.wait(timeout=30):
        print("[Error] GPU load never started (worker failed to signal).")
        process.terminate()
        process.join(timeout=5)
        return None
    return process


def stop_stress(process):
    """Terminate the stress worker and reap it."""
    if process is not None and process.is_alive():
        process.terminate()
        process.join(timeout=5)


def check_coolbits_enabled():
    """Returns True if the Xorg fan-control bit (Coolbits & 4) is active.

    Scans the standard Xorg config locations. A missing or wrong Coolbits
    value means nvidia-settings cannot take manual fan control, so the GPU
    runs on its auto-curve and may thermal-throttle during the sweep.
    """
    coolbits_re = re.compile(r'Option\s+"Coolbits"\s+"(\d+)"', re.IGNORECASE)

    def scan_file(path):
        try:
            m = coolbits_re.search(open(path).read())
            return bool(m and int(m.group(1)) & 4)
        except OSError:
            return False

    roots = [
        "/etc/X11/xorg.conf",
        "/etc/X11/xorg.conf.d",
        "/usr/share/X11/xorg.conf.d",
    ]
    for root in roots:
        if os.path.isfile(root):
            if scan_file(root):
                return True
        elif os.path.isdir(root):
            try:
                for fname in sorted(os.listdir(root)):
                    if fname.endswith(".conf") and scan_file(os.path.join(root, fname)):
                        return True
            except OSError:
                pass
    return False


def print_recommended_commands(best_mhz_step, best_opw_step, gpu_id, default_power_limit):
    g = str(gpu_id)
    mhz_clk = best_mhz_step['target_clock']
    opw_clk = best_opw_step['target_clock']
    sep = "=" * 58

    print(f"\n{sep}")
    print("  RECOMMENDED SETTINGS")
    print(sep)
    print(f"  Best MHz/W  →  {mhz_clk} MHz  "
          f"({best_mhz_step['mhz_per_w']:.2f} MHz/W, measured {best_mhz_step['power']:.1f}W)")
    print(f"  Best ops/W  →  {opw_clk} MHz  "
          f"({best_opw_step['ops_per_w']:.4f} ops/W, measured {best_opw_step['power']:.1f}W)")

    print(f"\n  -- Fan control prerequisite (one-time setup) --")
    print(f"  nvidia-settings fan override requires Coolbits=4 in Xorg.")
    print(f"  If set_fan_speed() did nothing, run these once then reboot:")
    print(f"    sudo nvidia-xconfig --cool-bits=4")
    print(f"    sudo systemctl restart gdm3   # or lightdm/sddm depending on DE")
    print(f"  Warning: restarting the display manager closes all open apps.")

    print(f"\n  -- Apply settings --")
    print(f"\n  Apply best MHz/W sweet spot (+ 400W cap):")
    print(f"    sudo nvidia-smi -i {g} -pl 400")
    print(f"    sudo nvidia-smi -i {g} -lgc {mhz_clk},{mhz_clk}")

    print(f"\n  Apply best ops/W sweet spot (+ 400W cap):")
    print(f"    sudo nvidia-smi -i {g} -pl 400")
    print(f"    sudo nvidia-smi -i {g} -lgc {opw_clk},{opw_clk}")

    print(f"\n  Reset to stock:")
    print(f"    sudo nvidia-smi -i {g} -rgc")
    print(f"    sudo nvidia-smi -i {g} -pl {int(default_power_limit)}")

    print(f"\n  Note: these reset on reboot. To persist, add to a")
    print(f"  systemd service or /etc/rc.local.")
    print(sep)


def main():
    args = parse_args()

    if args.list_gpus:
        list_gpus()
        sys.exit(0)

    gpu_id = args.target

    detected_max, detected_min, detected_pl = get_gpu_limits(gpu_id)
    clock_start   = args.clock_start   if args.clock_start   is not None else detected_max
    clock_min     = args.clock_min     if args.clock_min     is not None else detected_min
    clock_step    = args.clock_step
    test_duration = args.test_duration
    power_limit   = args.power_limit   if args.power_limit   is not None else detected_pl
    target_power  = args.target_power
    cool_period   = args.cool_period
    cool_temp     = args.cool_temp
    stable_window = args.stable_window
    inverted      = args.inverted

    sweep_lo, sweep_hi = (clock_min, clock_start) if inverted else (clock_start, clock_min)
    direction_str = f"{sweep_lo} → {sweep_hi} MHz" if inverted else f"{sweep_hi} → {sweep_lo} MHz"

    print(f"\n--- GPU {gpu_id} Tuning {'[INVERTED]' if inverted else ''} ---")
    print(f"  Sweep order  : {direction_str}  (step {clock_step} MHz)")
    print(f"  Baseline at  : {clock_start} MHz")
    print(f"  Power limit  : {power_limit:.0f} W  (stock {detected_pl:.0f} W)")
    print(f"  Target power : {'disabled' if target_power == 0 else f'{target_power} W'}")
    print(f"  Step duration: {test_duration} s")
    print(f"  Cool period  : {cool_period} s minimum, then ≤{cool_temp:.0f}°C "
          f"+ {stable_window}s stability")
    if inverted:
        post_baseline_cool = max(120, cool_period)
        print(f"  Post-baseline: {post_baseline_cool}s forced cool (inverted mode — full heat expulsion)")
    else:
        post_baseline_cool = cool_period

    if importlib.util.find_spec("torch") is None:
        print("[Error] PyTorch is required to load the GPU. Install it and retry.")
        sys.exit(1)

    subprocess.run(["sudo", "-v"])

    if not check_coolbits_enabled():
        print("\n" + "!" * 58)
        print("  WARNING: Coolbits fan-control not detected in Xorg config")
        print("!" * 58)
        print("  nvidia-settings cannot override the fan curve.")
        print("  The GPU will use its default auto-curve for the entire sweep.")
        print("  Under sustained full load this risks thermal throttling,")
        print("  which corrupts clock readings and efficiency numbers.")
        print()
        print("  To fix (one-time setup):")
        print("    sudo nvidia-xconfig --cool-bits=4")
        print("    sudo systemctl restart gdm3   # closes all open apps!")
        print()
        print("  Press Enter to continue at your own risk, or Ctrl+C to abort.")
        try:
            input()
        except KeyboardInterrupt:
            print("\nAborted.")
            sys.exit(0)

    g = str(gpu_id)

    # --- Fan ramp-up ---
    # Set fans to 100% first, then wait a full minute. Fan motors have inertia
    # and can take 30-60 s to reach full RPM from idle — measuring before they
    # spin up gives falsely warm temps.
    set_fan_speed(gpu_id, 100)
    print("\n-> Waiting 60 s for fans to reach full speed...")
    time.sleep(60)

    # Ensure the card is cool before anything starts.
    print(f"-> Waiting for GPU temp ≤ {cool_temp:.0f}°C before baseline...")
    wait_for_cool(gpu_id, target_temp=cool_temp, cool_period=0,
                  stable_window=stable_window)

    # Record idle temperature as our ambient proxy for the thermal model.
    _, T_idle, _ = get_gpu_stats(gpu_id)

    # Apply power limit and reset any leftover clock lock.
    subprocess.run(["sudo", "nvidia-smi", "-i", g, "-pl", str(int(power_limit))],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["sudo", "nvidia-smi", "-i", g, "-rgc"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    iteration_counter = multiprocessing.Value('q', 0)
    baseline_data = None
    steps_data = []
    current_stress = None  # always points to the running process so finally can clean up

    try:
        # ── Phase 1: baseline at clock_start ──────────────────────────────────
        print(f"\n[Phase 1] Baseline at {clock_start} MHz ({power_limit:.0f}W PL)...")
        subprocess.run(["sudo", "nvidia-smi", "-i", g, "-lgc", f"{clock_start},{clock_start}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        current_stress = launch_stress(iteration_counter)
        if current_stress is None:
            print("[Error] Could not start GPU load for baseline. Aborting.")
            sys.exit(1)

        base_p, base_t, base_c, base_ops, base_opw = run_step(gpu_id, test_duration, iteration_counter)
        stop_stress(current_stress)
        current_stress = None

        baseline_data = {
            'power': base_p, 'temp': base_t, 'actual_clock': base_c,
            'mhz_per_w': (base_c / base_p) if base_p > 0 else 0.0,
            'ops_per_sec': base_ops,
            'ops_per_w': base_opw,
        }
        print(f"Baseline -> Power: {base_p:.1f}W | Temp: {base_t:.1f}°C | Clock: {base_c:.0f}MHz"
              f" | MHz/W: {baseline_data['mhz_per_w']:.2f} | ops/W: {baseline_data['ops_per_w']:.4f}")

        # ── Thermal model derived from baseline ───────────────────────────────
        # R_thermal = (T_load - T_idle) / P_load   [°C/W]
        # C_heatsink ≈ 950 J/K  (large triple-fan GPU cooler — Al/Cu vapour chamber)
        # τ = R × C                                [seconds — first-order time constant]
        # t_cool(P) = τ × ln(ΔT_load / ΔT_target) [to cool from T_load to cool_temp]
        R_thermal = (base_t - T_idle) / base_p if base_p > 0 else 0.05
        C_heatsink = 950          # J/K — conservative for AORUS-class large cooler
        tau = R_thermal * C_heatsink
        print(f"\n   [Thermal model]")
        print(f"   R_thermal ≈ {R_thermal:.4f} °C/W  "
              f"(baseline ΔT {base_t - T_idle:.1f}°C at {base_p:.0f}W)")
        print(f"   τ ≈ {tau:.0f}s   "
              f"(95% equilibration ≈ {3*tau:.0f}s = 3τ)")
        dT_load   = max(base_t   - T_idle, 0.1)
        dT_target = max(cool_temp - T_idle, 0.1)
        t_cool_base = tau * math.log(dT_load / dT_target) if dT_load > dT_target else 0
        print(f"   Estimated cool-period for {base_p:.0f}W steps: {t_cool_base:.0f}s "
              f"(to reach {cool_temp:.0f}°C)")
        if cool_period < t_cool_base * 0.5:
            print(f"   ⚠ Your --cool-period {cool_period}s is less than half the estimated "
                  f"need ({t_cool_base:.0f}s). Consider --cool-period {int(t_cool_base)}")

        # Cool down after baseline before sweep starts.
        # In inverted mode, enforce minimum 120s to fully expel baseline heat
        # before stepping up from cold so warmup is clearly visible.
        if inverted:
            print(f"\n-> Inverted mode: enforcing {post_baseline_cool}s post-baseline cool "
                  f"(full heat expulsion before ascending sweep)...")
        wait_for_cool(gpu_id, target_temp=cool_temp, cool_period=post_baseline_cool,
                      stable_window=stable_window)

        # ── Phase 2: clock sweep ───────────────────────────────────────────────
        direction_label = "up" if inverted else "down"
        print(f"\n[Phase 2] Stepping {direction_label} through clocks...")

        # Sweep direction controlled by --inverted.
        if inverted:
            current_clock = clock_min
            clock_advance = +clock_step
            in_range = lambda c: c <= clock_start
        else:
            current_clock = clock_start
            clock_advance = -clock_step
            in_range = lambda c: c >= clock_min

        while in_range(current_clock):
            print(f"\nLocking clock to {current_clock} MHz...")
            subprocess.run(["sudo", "nvidia-smi", "-i", g, "-lgc", f"{current_clock},{current_clock}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            current_stress = launch_stress(iteration_counter)
            if current_stress is None:
                print(f"[Warning] Could not start load at {current_clock} MHz — skipping step.")
                current_clock += clock_advance
                continue

            p, t, c, ops, opw = run_step(gpu_id, test_duration, iteration_counter)
            stop_stress(current_stress)
            current_stress = None

            mhz_w = (c / p) if p > 0 else 0.0
            print(f"Result -> Power: {p:.1f}W | Temp: {t:.1f}°C | Actual Clock: {c:.0f}MHz"
                  f" | MHz/W: {mhz_w:.2f} | ops/W: {opw:.4f}")

            # Physics-based cooldown estimate for this step.
            energy_j = p * test_duration
            dT_step        = max(t        - T_idle, 0.1)
            dT_target_step = max(cool_temp - T_idle, 0.1)
            t_cool_est = (tau * math.log(dT_step / dT_target_step)
                          if dT_step > dT_target_step else 0)
            print(f"   [Thermal] {energy_j:.0f}J this step | "
                  f"Est. cool to {cool_temp:.0f}°C: {t_cool_est:.0f}s "
                  f"({'OK' if cool_period >= t_cool_est else f'⚠ need ≥{int(t_cool_est)}s'})")

            steps_data.append({
                'target_clock': current_clock, 'actual_clock': c,
                'power': p, 'temp': t,
                'mhz_per_w': mhz_w,
                'ops_per_sec': ops,
                'ops_per_w': opw,
            })

            if target_power > 0 and 0 < p <= target_power:
                print(f"\n✅ Target power {target_power}W reached at {current_clock} MHz!")
                break

            current_clock += clock_advance

            # Cool down between steps — skip final cooldown after the last one.
            if in_range(current_clock):
                wait_for_cool(gpu_id, target_temp=cool_temp, cool_period=cool_period,
                              stable_window=stable_window)

    except KeyboardInterrupt:
        print("\nTest interrupted.")
    finally:
        stop_stress(current_stress)
        print("\n--- Cleaning up ---")
        print("-> Resetting core clocks to default...")
        subprocess.run(["sudo", "nvidia-smi", "-i", g, "-rgc"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("-> Restoring stock power limit...")
        subprocess.run(["sudo", "nvidia-smi", "-i", g, "-pl", str(int(detected_pl))],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        reset_fan_speed(gpu_id)

        if steps_data and baseline_data:
            best_mhz = max(steps_data, key=lambda x: x['mhz_per_w'])
            best_opw = max(steps_data, key=lambda x: x['ops_per_w'])
            print(f"\n🏆 Best MHz/W:  {best_mhz['target_clock']} MHz → {best_mhz['mhz_per_w']:.2f} MHz/W at {best_mhz['power']:.1f}W")
            print(f"🏆 Best ops/W:  {best_opw['target_clock']} MHz → {best_opw['ops_per_w']:.4f} ops/W at {best_opw['power']:.1f}W")
            plot_results(baseline_data, steps_data, target_power, inverted=inverted)
            print_recommended_commands(best_mhz, best_opw, gpu_id, detected_pl)


if __name__ == "__main__":
    main()
