#!/bin/bash
#
# Bluez Upgrade Script for Odroid XU4
# Upgrades bluez to latest version for better BLE support
#

set -e  # Exit on error

echo "========================================"
echo "Bluez Upgrade Script"
echo "========================================"
echo ""

# Check current version
echo "[1/6] Checking current bluez version..."
CURRENT_VERSION=$(bluetoothctl --version 2>/dev/null || echo "unknown")
echo "Current version: $CURRENT_VERSION"
echo ""

# Backup current bluetooth config
echo "[2/6] Backing up bluetooth config..."
sudo cp /etc/bluetooth/main.conf /etc/bluetooth/main.conf.backup.$(date +%Y%m%d) 2>/dev/null || true
echo "✓ Backup complete"
echo ""

# Add PPA
echo "[3/6] Adding bluez PPA..."
sudo add-apt-repository -y ppa:bluetooth/bluez
echo "✓ PPA added"
echo ""

# Update package list
echo "[4/6] Updating package list..."
sudo apt update
echo "✓ Package list updated"
echo ""

# Install/upgrade bluez
echo "[5/6] Installing/upgrading bluez..."
sudo apt install -y bluez
echo "✓ Bluez installed"
echo ""

# Restart bluetooth service
echo "[6/6] Restarting bluetooth service..."
sudo systemctl restart bluetooth
sleep 2
echo "✓ Bluetooth service restarted"
echo ""

# Check new version
NEW_VERSION=$(bluetoothctl --version 2>/dev/null || echo "unknown")
echo "========================================"
echo "Upgrade Complete!"
echo "========================================"
echo "Old version: $CURRENT_VERSION"
echo "New version: $NEW_VERSION"
echo ""

# Test bluetooth
echo "Testing bluetooth adapter..."
if hciconfig hci0 > /dev/null 2>&1; then
    echo "✓ Bluetooth adapter detected"
    hciconfig hci0
else
    echo "✗ No bluetooth adapter detected"
    echo "You may need to replug your USB bluetooth adapter"
fi

echo ""
echo "========================================"
echo "Next Steps:"
echo "========================================"
echo "1. Unplug and replug your TP-Link bluetooth adapter"
echo "2. Run: bluetoothctl"
echo "3. Run: scan on"
echo "4. Wait 30 seconds - look for your JK BMS"
echo ""
echo "If JK BMS still doesn't appear:"
echo "  - Try: sudo systemctl restart bluetooth"
echo "  - Or reboot: sudo reboot"
echo ""
