#!/usr/bin/env python3

import argparse
import re
import subprocess
import sys
import time

NUM_CELLS = 5

# Data specific to Dewalt battery
CELL_VOLTAGES = [4.05, 3.6, 3.4, 3.3]
CELL_PERCENTAGES = [100, 66, 33, 0]

# Service mode: spoken warnings at these percentages, shutdown at the last.
WARN_LEVELS = [35, 25, 15]
SHUTDOWN_LEVEL = 10
POLL_SECS = 30
SAY = "/usr/local/bin/say"


# Function to read data from I2C device
def read_from_i2c(i2c, address, read_addr, length):
    # Ensure the I2C bus is not locked before proceeding
    while not i2c.try_lock():
        pass

    try:
        # Writing the register address we want to read from
        i2c.writeto(address, bytes([read_addr]))

        # Reading the specified length of bytes
        result = bytearray(length)
        i2c.readfrom_into(address, result)

        return result
    finally:
        # Release the I2C lock
        i2c.unlock()


def get_battery_voltage(i2c, multiplier=0.0013125):
    i2c_address = 0x48
    mem_read_addr = 0x8C
    bytes_to_read = 2

    data = read_from_i2c(i2c, i2c_address, mem_read_addr, bytes_to_read)
    sensor_value = int.from_bytes(data, byteorder="big")
    return sensor_value * multiplier


def voltage_to_percentage(bat_voltage):
    cell_voltage = bat_voltage / NUM_CELLS
    if cell_voltage < CELL_VOLTAGES[-1]:
        return CELL_PERCENTAGES[-1]
    for i in range(len(CELL_VOLTAGES) - 1):
        if CELL_VOLTAGES[i + 1] < cell_voltage < CELL_VOLTAGES[i]:
            proportion = (cell_voltage - CELL_VOLTAGES[i + 1]) / (
                CELL_VOLTAGES[i] - CELL_VOLTAGES[i + 1] + 1e-6
            )
            return (
                proportion * CELL_PERCENTAGES[i]
                + (1 - proportion) * CELL_PERCENTAGES[i + 1]
            )
    return 100


def open_i2c():
    # Imported lazily so service mode does not touch the I2C bus at all in
    # its normal path (see read_percentage_from_gui_journal).
    import board
    import busio

    try:
        return busio.I2C(board.SCL, board.SDA)
    except (PermissionError, OSError) as e:
        print(f"Error accessing I2C bus: {e}", file=sys.stderr)
        print("Run: sudo usermod -a -G i2c pi && sudo reboot", file=sys.stderr)
        return None


def read_percentage_from_gui_journal(max_age_secs=120):
    """Latest battery percentage logged by pupper-gui, or None if stale/absent.

    The GUI polls the battery ADC on a timer and logs every reading. Using its
    journal as our data source keeps this service off the I2C bus entirely -
    two concurrent readers racing the gt911 touchscreen driver on bus 1 can
    latch the ADC until the battery is physically power-cycled.
    """
    try:
        out = subprocess.run(
            ["journalctl", "-u", "pupper-gui", "-n", "100", "--no-pager", "-o", "short-unix"],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except Exception:
        return None
    latest = None
    for line in out.splitlines():
        m = re.match(r"^(\d+)\.\d+ .*Battery percentage: Some\((\d+)\)", line)
        if m:
            latest = (float(m.group(1)), int(m.group(2)))
    if latest and time.time() - latest[0] <= max_age_secs:
        return latest[1]
    return None


def speak(message):
    """Say the message aloud through the robot's speaker; fall back to wall.

    Runs as user pi so the say script can reach the desktop session's
    PipeWire (this service runs as root).
    """
    try:
        subprocess.run(
            ["sudo", "-u", "pi", SAY, message], timeout=45, check=True
        )
    except Exception:
        try:
            subprocess.run(["wall", message], timeout=10, check=False)
        except Exception:
            pass


def on_wall_power():
    """True if the pack voltage reads like a bench supply (< 5V)."""
    i2c = open_i2c()
    if i2c is None:
        return False
    try:
        return get_battery_voltage(i2c) < 5.0
    except Exception:
        return False


def service_loop():
    announced = set()
    while True:
        pct = read_percentage_from_gui_journal()
        if pct is None:
            # GUI not logging (crashed or early boot): fall back to one
            # direct read this cycle.
            i2c = open_i2c()
            if i2c is not None:
                try:
                    pct = int(voltage_to_percentage(get_battery_voltage(i2c)))
                except Exception:
                    pct = None
        if pct is None:
            time.sleep(POLL_SECS)
            continue

        # Battery swapped or recharged: re-arm announcements above the level.
        announced = {lvl for lvl in announced if pct <= lvl + 10}

        if pct <= SHUTDOWN_LEVEL:
            # A sub-5V reading means bench power, not a dying pack.
            if on_wall_power():
                time.sleep(POLL_SECS)
                continue
            speak(
                f"Battery critically low at {pct} percent. "
                "Shutting down in one minute. Please recharge me!"
            )
            subprocess.run(["shutdown", "-h", "+1"], check=False)
            return

        for lvl in WARN_LEVELS:
            if pct <= lvl and lvl not in announced:
                announced.add(lvl)
                speak(f"Heads up: my battery is at {pct} percent.")
                break

        time.sleep(POLL_SECS)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--service_mode",
        action="store_true",
        help=f"poll battery every {POLL_SECS}s; spoken warnings at "
        f"{WARN_LEVELS}%%, shutdown at {SHUTDOWN_LEVEL}%%",
    )
    parser.add_argument("--percentage_only", action="store_true")
    parser.add_argument(
        "--test_speech",
        action="store_true",
        help="announce the current battery level once and exit",
    )
    args = parser.parse_args()

    if args.service_mode:
        service_loop()
        return

    if args.test_speech:
        pct = read_percentage_from_gui_journal()
        speak(f"Battery test: I am at {pct if pct is not None else 'unknown'} percent.")
        return

    i2c = open_i2c()
    if i2c is None:
        sys.exit(1)
    bat_voltage = get_battery_voltage(i2c)
    cell_percentage = voltage_to_percentage(bat_voltage)

    if args.percentage_only:
        print(f"{int(cell_percentage)}")
    else:
        print(f"Battery:\t{int(cell_percentage)}%\t{bat_voltage:0.2f}V")


if __name__ == "__main__":
    main()
