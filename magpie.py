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

# Calculate master percentage based on number of inverters
master_percentage = 1.0 / args.num_inverters

# URL to fetch data from
url = "http://data.magnumenergy.com/MW6181/"

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
    while not stop_thread:
        try:
            # Fetch page
            response = requests.get(url, timeout=10)
            response.raise_for_status()

            # Parse the page using BeautifulSoup
            soup = BeautifulSoup(response.content, 'html.parser')

            # Find relevant elements for DC volts
            try:
                dc_volts_min = float(re.findall(r"[-+]?[0-9]*\.?[0-9]+", soup.find('th', string='DC volts:').find_next_sibling('td').text.strip())[0])
                dc_volts_max = float(re.findall(r"[-+]?[0-9]*\.?[0-9]+", soup.find('th', string='DC volts:').find_next_sibling('td').find_next_sibling('td').text.strip())[0])
                dc_volts_avg = float(re.findall(r"[-+]?[0-9]*\.?[0-9]+", soup.find('th', string='DC volts:').find_next_sibling('td').find_next_sibling('td').find_next_sibling('td').text.strip())[0])
            except AttributeError:
                print("Error finding the DC volts data on the page.")
                continue

            # Find relevant elements for DC volts/amps/watts (current values)
            try:
                dc_volts_amps_watts = soup.find('span', id='i_dc_watts').text.strip()
                dc_values = dc_volts_amps_watts.split('@')
                dc_volts = float(re.findall(r"[-+]?[0-9]*\.?[0-9]+", dc_values[0].strip())[0])
                dc_amps_watts = dc_values[1].strip().split('(')

                # Extract numeric values for amps and watts
                dc_amps = float(re.findall(r"[-+]?[0-9]*\.?[0-9]+", dc_amps_watts[0])[0])
                dc_watts = float(re.findall(r"[-+]?[0-9]*\.?[0-9]+", dc_amps_watts[1])[0])
            except (AttributeError, IndexError, ValueError):
                print("Error finding the DC volts/amps/watts data on the page.")
                continue

            # Find AC Out and AC In values (amps only)
            try:
                ac_out_data = soup.find('th', string='AC Out:').find_next_sibling('td').text.strip()
                ac_out_match = re.search(r"@\s*([-+]?[0-9]*\.?[0-9]+)\s*amps", ac_out_data, re.IGNORECASE)
                ac_out = ac_out_match.group(1) if ac_out_match else "Unknown"
            except AttributeError:
                ac_out = "Unknown"

            try:
                ac_in_data = soup.find('th', string='AC In:').find_next_sibling('td').text.strip()
                ac_in_match = re.search(r"([-+]?[0-9]*\.?[0-9]+)\s*amps", ac_in_data, re.IGNORECASE)
                ac_in = ac_in_match.group(1) if ac_in_match else "Unknown"
            except AttributeError:
                ac_in = "Unknown"

            # Determine system state (Charging or Inverting)
            system_status = soup.find('td', id='iStatus').text.strip().lower()
            if 'inverting' in system_status:
                system_state = "Inverting"
            elif 'absorb' in system_status or 'charging' in system_status:
                system_state = "Charging"
            else:
                system_state = "Unknown"

            # Adjust amps and watts if the system is inverting
            if system_state == "Inverting":
                dc_amps /= master_percentage
                dc_watts /= master_percentage

            # Compute Avg Cell Volts
            avg_cell_volts = dc_volts / 16
            if avg_cell_volts >= 3.3:
                avg_cell_volts_display = colored(f"{avg_cell_volts:.2f}", "green")
            elif avg_cell_volts >= 3.2:
                avg_cell_volts_display = colored(f"{avg_cell_volts:.2f}", "yellow")
            else:
                avg_cell_volts_display = colored(f"{avg_cell_volts:.2f}", "red")

            # Clear the screen (only in continuous mode)
            if not args.report:
                os.system('cls' if os.name == 'nt' else 'clear')

                # Display message to quit
                print("Press the [q] key to quit at anytime.\n")

            # Display results in a unicode friendly table for terminal
            table = PrettyTable()
            table.field_names = ["Parameter", "Value"]
            table.add_row(["Min DC Volts", f"{dc_volts_min}"])
            table.add_row(["Avg DC Volts", f"{dc_volts_avg}"])
            table.add_row(["Max DC Volts", f"{dc_volts_max}"])
            if dc_volts < 51:
                current_dc_volts = colored(f"{dc_volts}", "red")
            elif dc_volts < 52:
                current_dc_volts = colored(f"{dc_volts}", "yellow")
            else:
                current_dc_volts = colored(f"{dc_volts}", "green")
            table.add_row(["Current DC Volts", current_dc_volts])
            table.add_row(["Current DC Amps", f"{dc_amps:.2f}"])
            table.add_row(["Current DC Watts", f"{dc_watts:.2f}"])
            table.add_row(["Avg Cell Volts", avg_cell_volts_display])
            table.add_row(["AC Out (amps)", ac_out])
            table.add_row(["AC In (amps)", ac_in])
            table.add_row(["System State", system_state])

            # Calculate and display estimated SoC (only if capacity was specified)
            if args.capacity is not None:
                is_discharging = system_state == "Inverting"
                soc, is_compensated = estimate_soc(dc_volts, dc_amps, args.resistance, is_discharging)

                # Color code SoC based on level
                if soc >= 50:
                    soc_display = colored(f"{soc:.0f}%", "green")
                elif soc >= 20:
                    soc_display = colored(f"{soc:.0f}%", "yellow")
                else:
                    soc_display = colored(f"{soc:.0f}%", "red")

                # Add indicator if load-compensated
                if is_compensated:
                    soc_display += " (load-compensated)"
                elif is_discharging:
                    soc_display += " (resting)"

                table.add_row(["Estimated SoC", soc_display])

            print(table)

            # Exit immediately if in report mode
            if args.report:
                break

            # Wait for the specified interval or until stop_thread is True
            for _ in range(args.t):
                if stop_thread:
                    break
                time.sleep(1)

        except requests.RequestException as e:
            print(f"Error fetching the webpage: {e}")
            time.sleep(5)

except KeyboardInterrupt:
    print("Exiting program.")
finally:
    # Restore terminal settings (only if we modified them)
    if original_settings is not None:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, original_settings)
        os.system('stty sane')
