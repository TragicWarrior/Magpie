import requests
from bs4 import BeautifulSoup
import re
from prettytable import PrettyTable
from termcolor import colored
import time
import argparse
import os
import sys
import threading
import tty
import termios

# Argument parser to accept -t for time interval and -n for number of inverters
parser = argparse.ArgumentParser(description="Fetch and display DC power data.")
parser.add_argument('-t', type=int, default=10, help='Time interval in seconds for refreshing data')
parser.add_argument('-n', '--num-inverters', type=int, default=3, help='Number of inverters in the stacked configuration (default: 3)')
parser.add_argument('-c', '--capacity', type=float, default=None, help='Total battery capacity in Ah (e.g., 400 for 400Ah). Enables estimated SoC display (experimental voltage-based estimation)')
parser.add_argument('--resistance', type=float, default=0.020, help='Pack internal resistance in ohms (default: 0.020)')
parser.add_argument('-r', '--report', action='store_true', help='Run once and exit immediately (report mode)')
args = parser.parse_args()

# LiFePO4 16S voltage-to-SoC mapping (voltage, SoC%)
# Based on typical LiFePO4 discharge curve
SOC_TABLE = [
    (58.4, 100),
    (54.4, 90),
    (53.6, 80),
    (52.8, 50),
    (51.2, 20),
    (49.6, 10),
    (44.8, 0),
]

def estimate_soc(voltage, current, resistance, is_discharging):
    """
    Estimate State of Charge based on voltage with load compensation.

    Args:
        voltage: Measured pack voltage
        current: Measured current (positive = discharge)
        resistance: Pack internal resistance in ohms
        is_discharging: True if system is inverting/discharging

    Returns:
        Tuple of (soc_percentage, is_compensated)
    """
    # Determine if we need load compensation
    # If current is very low (< 5A), consider it resting voltage
    is_compensated = False
    compensated_voltage = voltage

    if is_discharging and abs(current) > 5:
        # Compensate for voltage sag: V_rest = V_measured + (I × R_internal)
        compensated_voltage = voltage + (abs(current) * resistance)
        is_compensated = True

    # Clamp voltage to table range
    if compensated_voltage >= SOC_TABLE[0][0]:
        return (100, is_compensated)
    if compensated_voltage <= SOC_TABLE[-1][0]:
        return (0, is_compensated)

    # Linear interpolation between table points
    for i in range(len(SOC_TABLE) - 1):
        v_high, soc_high = SOC_TABLE[i]
        v_low, soc_low = SOC_TABLE[i + 1]

        if v_low <= compensated_voltage <= v_high:
            # Linear interpolation
            ratio = (compensated_voltage - v_low) / (v_high - v_low)
            soc = soc_low + ratio * (soc_high - soc_low)
            return (soc, is_compensated)

    return (0, is_compensated)


class ParseError(Exception):
    """Raised when expected data cannot be parsed from the inverter page."""
    pass


def fetch_inverter_data(url):
    """
    Fetch and parse inverter data from the Magnum Energy web interface.

    Args:
        url: URL of the inverter data page

    Returns:
        Dict with keys: dc_volts_min, dc_volts_max, dc_volts_avg,
                        dc_volts, dc_amps, dc_watts,
                        ac_out, ac_in, system_state

    Raises:
        ParseError: If any expected data field cannot be extracted
        requests.RequestException: On network errors
    """
    response = requests.get(url, timeout=10)
    response.raise_for_status()

    soup = BeautifulSoup(response.content, 'html.parser')

    # Parse DC volts min/avg/max
    try:
        dc_volts_row = soup.find('th', string='DC volts:')
        tds = dc_volts_row.find_next_siblings('td')
        dc_volts_min = float(re.findall(r"[-+]?[0-9]*\.?[0-9]+", tds[0].text.strip())[0])
        dc_volts_max = float(re.findall(r"[-+]?[0-9]*\.?[0-9]+", tds[1].text.strip())[0])
        dc_volts_avg = float(re.findall(r"[-+]?[0-9]*\.?[0-9]+", tds[2].text.strip())[0])
    except (AttributeError, IndexError, ValueError) as e:
        raise ParseError(f"Failed to parse DC volts min/avg/max: {e}")

    # Parse current DC volts/amps/watts
    try:
        dc_volts_amps_watts = soup.find('span', id='i_dc_watts').text.strip()
        dc_values = dc_volts_amps_watts.split('@')
        dc_volts = float(re.findall(r"[-+]?[0-9]*\.?[0-9]+", dc_values[0].strip())[0])
        dc_amps_watts = dc_values[1].strip().split('(')
        dc_amps = float(re.findall(r"[-+]?[0-9]*\.?[0-9]+", dc_amps_watts[0])[0])
        dc_watts = float(re.findall(r"[-+]?[0-9]*\.?[0-9]+", dc_amps_watts[1])[0])
    except (AttributeError, IndexError, ValueError) as e:
        raise ParseError(f"Failed to parse DC volts/amps/watts: {e}")

    # Parse AC Out
    try:
        ac_out_data = soup.find('th', string='AC Out:').find_next_sibling('td').text.strip()
        ac_out_match = re.search(r"@\s*([-+]?[0-9]*\.?[0-9]+)\s*amps", ac_out_data, re.IGNORECASE)
        ac_out = ac_out_match.group(1) if ac_out_match else "Unknown"
    except AttributeError:
        ac_out = "Unknown"

    # Parse AC In
    try:
        ac_in_data = soup.find('th', string='AC In:').find_next_sibling('td').text.strip()
        ac_in_match = re.search(r"([-+]?[0-9]*\.?[0-9]+)\s*amps", ac_in_data, re.IGNORECASE)
        ac_in = ac_in_match.group(1) if ac_in_match else "Unknown"
    except AttributeError:
        ac_in = "Unknown"

    # Parse system state
    try:
        system_status = soup.find('td', id='iStatus').text.strip().lower()
    except AttributeError as e:
        raise ParseError(f"Failed to parse system status: {e}")

    if 'inverting' in system_status:
        system_state = "Inverting"
    elif 'absorb' in system_status or 'charging' in system_status:
        system_state = "Charging"
    else:
        system_state = "Unknown"

    return {
        'dc_volts_min': dc_volts_min,
        'dc_volts_max': dc_volts_max,
        'dc_volts_avg': dc_volts_avg,
        'dc_volts': dc_volts,
        'dc_amps': dc_amps,
        'dc_watts': dc_watts,
        'ac_out': ac_out,
        'ac_in': ac_in,
        'system_state': system_state,
    }


def scale_for_stacked(data, master_percentage):
    """
    Scale DC amps and watts for a stacked inverter configuration.

    When the system is inverting, the master unit only reports its own share
    of the total load. This scales up to the full system value.

    Args:
        data: Dict from fetch_inverter_data()
        master_percentage: Fraction representing the master's share (1/num_inverters)

    Returns:
        New dict with scaled dc_amps and dc_watts if inverting
    """
    result = dict(data)
    if result['system_state'] == "Inverting":
        result['dc_amps'] = data['dc_amps'] / master_percentage
        result['dc_watts'] = data['dc_watts'] / master_percentage
    return result


def build_table(data, args):
    """
    Build a PrettyTable displaying inverter metrics with color-coded values.

    Args:
        data: Dict from fetch_inverter_data() (after scaling)
        args: Parsed command-line arguments

    Returns:
        PrettyTable ready to print
    """
    dc_volts = data['dc_volts']

    # Color-code current DC volts
    if dc_volts < 51:
        current_dc_volts = colored(f"{dc_volts}", "red")
    elif dc_volts < 52:
        current_dc_volts = colored(f"{dc_volts}", "yellow")
    else:
        current_dc_volts = colored(f"{dc_volts}", "green")

    # Compute and color-code avg cell volts
    avg_cell_volts = dc_volts / 16
    if avg_cell_volts >= 3.3:
        avg_cell_volts_display = colored(f"{avg_cell_volts:.2f}", "green")
    elif avg_cell_volts >= 3.2:
        avg_cell_volts_display = colored(f"{avg_cell_volts:.2f}", "yellow")
    else:
        avg_cell_volts_display = colored(f"{avg_cell_volts:.2f}", "red")

    table = PrettyTable()
    table.field_names = ["Parameter", "Value"]
    table.add_row(["Min DC Volts (24h)", f"{data['dc_volts_min']}"])
    table.add_row(["Avg DC Volts (24h)", f"{data['dc_volts_avg']}"])
    table.add_row(["Max DC Volts (24h)", f"{data['dc_volts_max']}"])
    table.add_row(["Current DC Volts", current_dc_volts])
    table.add_row(["Current DC Amps", f"{data['dc_amps']:.2f}"])
    table.add_row(["Current DC Watts", f"{data['dc_watts']:.2f}"])
    table.add_row(["Avg Cell Volts", avg_cell_volts_display])
    table.add_row(["AC Out (amps)", data['ac_out']])
    table.add_row(["AC In (amps)", data['ac_in']])
    table.add_row(["System State", data['system_state']])

    # SoC estimation (only if capacity was specified)
    if args.capacity is not None:
        is_discharging = data['system_state'] == "Inverting"
        soc, is_compensated = estimate_soc(dc_volts, data['dc_amps'], args.resistance, is_discharging)

        if soc >= 50:
            soc_display = colored(f"{soc:.0f}%", "green")
        elif soc >= 20:
            soc_display = colored(f"{soc:.0f}%", "yellow")
        else:
            soc_display = colored(f"{soc:.0f}%", "red")

        if is_compensated:
            soc_display += " (load-compensated)"
        elif is_discharging:
            soc_display += " (resting)"

        table.add_row(["Estimated SoC", soc_display])

    return table


# Calculate master percentage based on number of inverters
master_percentage = 1.0 / args.num_inverters

# URL to fetch data from
url = "http://data.magnumenergy.com/MW6181/"

MAX_CONSECUTIVE_FAILURES = 5

# Flag to control the loop
stop_thread = False
original_settings = None

# Only set up quit detection if not in report mode
if not args.report:
    original_settings = termios.tcgetattr(sys.stdin.fileno())

    def check_quit():
        global stop_thread
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while True:
                if sys.stdin.read(1) == 'q':
                    stop_thread = True
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    # Start a separate thread to listen for 'q' key press
    quit_thread = threading.Thread(target=check_quit)
    quit_thread.daemon = True
    quit_thread.start()

try:
    consecutive_failures = 0

    while not stop_thread:
        try:
            data = fetch_inverter_data(url)
            data = scale_for_stacked(data, master_percentage)
            table = build_table(data, args)

            consecutive_failures = 0

            if not args.report:
                os.system('cls' if os.name == 'nt' else 'clear')
                print("Press the [q] key to quit at anytime.\n")

            print(table)

            if args.report:
                break

            # Wait for the specified interval or until stop_thread is True
            for _ in range(args.t):
                if stop_thread:
                    break
                time.sleep(1)

        except (ParseError, requests.RequestException) as e:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"Too many consecutive failures ({consecutive_failures}). Exiting.")
                break
            delay = min(5 * (2 ** (consecutive_failures - 1)), 60)
            print(f"Error ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {e}. Retrying in {delay}s...")
            for _ in range(delay):
                if stop_thread:
                    break
                time.sleep(1)

except KeyboardInterrupt:
    print("Exiting program.")
finally:
    # Restore terminal settings (only if we modified them)
    if original_settings is not None:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, original_settings)
        os.system('stty sane')
