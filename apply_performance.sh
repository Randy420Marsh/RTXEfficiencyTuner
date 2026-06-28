#!/bin/bash

# Ensure the script is run with root privileges
if [ "$EUID" -ne 0 ]; then
  echo "Please run this script with sudo."
  exit 1
fi

# =====================================================================
# REFERENCE: Persistent Configuration via /etc/default/cpufrequtils
# =====================================================================
# To make your profile choices permanent across system boots, edit the
# system configuration file: 
#   sudo nano /etc/default/cpufrequtils
#
# Common values you can set:
#   GOVERNOR="performance" -> Locks CPU into high-performance boost states.
#   GOVERNOR="powersave"   -> Default behavior; aggressively downclocks on idle.
#
# After modifying that file, apply it with:
#   sudo systemctl restart cpufrequtils
# =====================================================================

# =====================================================================
# Check the PSU mode safely
# =====================================================================
echo "Checking Corsair HX1200i Status..."
if systemctl is-active --quiet hx1200i-singlerail.service; then
    echo " -> hx1200i-singlerail service is active."
else
    echo " [!] Warning: hx1200i-singlerail service is NOT active."
fi

# Temporarily drop driver for a clean hardware read
rmmod corsair-psu 2>/dev/null

# Run status check, capture exit code so we can report failure explicitly.
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
# =====================================================================

echo "1. Locking GNOME Power Profile..."
# Run powerprofilesctl as the standard user to ensure DBus communication with GNOME works
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
# Sleep for 1 second to give any rogue background services a chance to revert the setting
sleep 1

ALL_PERF=true
for cpu_gov in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    CURRENT_GOV=$(cat "$cpu_gov")
    if [ "$CURRENT_GOV" != "performance" ]; then
        # Extract just the core number from the path for cleaner output
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
