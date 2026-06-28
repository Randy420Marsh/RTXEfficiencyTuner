# Ubuntu 24.04: Single-Rail PSU Mode & CPU Performance Governor

This guide covers two system-level optimisations that improve stability and measurement accuracy for high-TDP GPU workloads:

1. Lock a Corsair HX1200i into **single-rail OCP mode** via a systemd service so transient GPU power spikes don't trip per-rail current limits.
2. Pin all CPU cores to the **performance governor** so the CPU boosts immediately rather than lagging behind the GPU workload.

## Hardware Reference

| Component | Model |
|---|---|
| PSU | Corsair HX1200i (1200W) |
| Motherboard | ASUS TUF GAMING Z890-PLUS WIFI |
| CPU | Intel Core Ultra 7 270K |
| GPU | Gigabyte GeForce RTX 5090 AORUS XTREME WATERFORCE |
| RAM | G.Skill Trident Z5 Neo RGB 64GB (2×32GB) DDR5-6000 CL30 1.40V |

## System

| | |
|---|---|
| OS | Ubuntu 24.04.4 LTS (Noble Numbat) |
| Kernel | Linux 6.17.0-35-generic x86_64 |
| GNOME Shell | 46.0 |
| Firmware | ASUS 3012 |

---

## Part 1: Corsair HX1200i Single-Rail Mode (systemd service)

The HX1200i ships in **multi-rail OCP mode** by default, splitting 12V output across several virtual rails each with a lower per-rail current limit. Under rapid GPU transients (e.g. the RTX 5090 spiking to 600W instantaneously) a per-rail OCP trip can cause a momentary power cut to the GPU — visible as a driver reset, black screen, or compute job failure — even when total draw is within the PSU's rated capacity.

This systemd service switches the PSU to single-rail mode on every boot: one high-capacity 12V rail with no per-rail limiting.

### Step 1 — Create the service file

```bash
sudo nano /etc/systemd/system/hx1200i-singlerail.service
```

### Step 2 — Paste the configuration

```ini
[Unit]
Description=Set Corsair HX1200i to Single-Rail OCP
After=multi-user.target

[Service]
Type=oneshot

# 1. Unload the kernel driver to unlock the USB interface for liquidctl
ExecStartPre=-/usr/sbin/modprobe -r corsair-psu

# 2. Apply the single-rail hardware setting
ExecStart=/opt/liquidctl/bin/liquidctl --vendor 1b1c --product 1c27 initialize --single-12v-ocp

# 3. Reload the kernel driver so sensors and Astra Monitor continue to work
ExecStartPost=-/usr/sbin/modprobe corsair-psu

RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

Save and exit: `Ctrl+O` → `Enter` → `Ctrl+X`.

> **Note:** The service uses the full path `/opt/liquidctl/bin/liquidctl` to target the virtualenv installation. Adjust the path if you installed liquidctl differently (`which liquidctl` to check).

### Step 3 — Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hx1200i-singlerail.service
```

### Step 4 — Verify the service ran cleanly

```bash
systemctl status hx1200i-singlerail.service
```

### Manually verifying single-rail state

After the service runs, `liquidctl status` will show a warning and hide the OCP/fan-control lines. This is expected — the `ExecStartPost` step reloads the `corsair-psu` kernel driver, which reclaims the USB interface for sensor telemetry and blocks liquidctl from reading the deeper OCP state.

To confirm the single-rail setting at any time, temporarily evict the kernel driver:

```bash
sudo rmmod corsair-psu
liquidctl --match corsair status   # OCP Mode should show "Single rail"
sudo modprobe corsair-psu          # restore kernel driver and sensor monitoring
```

---

## Part 2: apply_performance.sh

This script handles both the CPU governor and PSU validation in one command. Run it before starting a tuning sweep.

```bash
sudo bash apply_performance.sh
```

**What it does:**

1. Checks whether `hx1200i-singlerail.service` is active and reads live PSU status via liquidctl
2. Locks GNOME's power profile to `performance` (via `powerprofilesctl` over DBus as the original user)
3. Writes `performance` to every core's `scaling_governor`
4. Waits 1 second and re-reads all governors to confirm nothing reverted them

```bash
#!/bin/bash

# Ensure the script is run with root privileges
if [ "$EUID" -ne 0 ]; then
  echo "Please run this script with sudo."
  exit 1
fi

# =====================================================================
# REFERENCE: Persistent Configuration via /etc/default/cpufrequtils
# =====================================================================
# To make your profile choices permanent across system boots, edit:
#   sudo nano /etc/default/cpufrequtils
#
# Common values:
#   GOVERNOR="performance" -> Locks CPU into high-performance boost states.
#   GOVERNOR="powersave"   -> Default; aggressively downclocks on idle.
#
# Apply with:
#   sudo systemctl restart cpufrequtils
# =====================================================================

# ── PSU status ────────────────────────────────────────────────────────
echo "Checking Corsair HX1200i Status..."
if systemctl is-active --quiet hx1200i-singlerail.service; then
    echo " -> hx1200i-singlerail service is active."
else
    echo " [!] Warning: hx1200i-singlerail service is NOT active."
fi

# Temporarily drop kernel driver for a clean hardware read via liquidctl.
rmmod corsair-psu 2>/dev/null

if command -v liquidctl &> /dev/null; then
    liquidctl --match corsair status
    LIQUID_EXIT=$?
    if [ "$LIQUID_EXIT" -ne 0 ]; then
        echo " [!] liquidctl exited with code $LIQUID_EXIT — PSU status read may have failed."
    fi
else
    echo " [!] liquidctl not found — skipping PSU status read."
fi

# ALWAYS reload the driver, regardless of whether liquidctl succeeded.
modprobe corsair-psu
echo " -> Restored sensor monitoring kernel driver."
echo "----------------------------------------"

# ── CPU governor ──────────────────────────────────────────────────────
echo "1. Locking GNOME Power Profile..."
if command -v powerprofilesctl &> /dev/null; then
    sudo -u "$SUDO_USER" powerprofilesctl set performance
    echo " -> GNOME profile locked to 'performance'."
else
    echo " -> powerprofilesctl not found, skipping."
fi

echo "2. Setting all CPU cores to 'performance'..."
for cpu_gov in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo "performance" > "$cpu_gov"
done
echo " -> Applied to all cores."

echo "3. Verifying the settings stuck..."
sleep 1

ALL_PERF=true
for cpu_gov in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    CURRENT_GOV=$(cat "$cpu_gov")
    if [ "$CURRENT_GOV" != "performance" ]; then
        CORE_NUM=$(echo "$cpu_gov" | grep -o -E 'cpu[0-9]+' | sed 's/cpu//')
        echo " [!] Core $CORE_NUM reverted to: $CURRENT_GOV"
        ALL_PERF=false
    fi
done

echo "----------------------------------------"
if [ "$ALL_PERF" = true ]; then
    echo "[✔] SUCCESS: All CPU threads are perfectly locked to 'performance'."
else
    echo "[✘] ERROR: Some cores failed to stick. Another background service is interfering."
fi
```

### Making the CPU governor permanent

To survive reboots without running the script every time:

```bash
sudo nano /etc/default/cpufrequtils
# Set: GOVERNOR="performance"

sudo systemctl restart cpufrequtils
```

> **Note:** Some background services (e.g. `power-profiles-daemon`, `thermald`) actively fight the governor setting back to `powersave`. If the verification step reports reverted cores, check which service is winning with `systemctl list-units --type=service | grep -i power`.
