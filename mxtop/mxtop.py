import sys
import tty
import time
import select
import termios
import argparse
import threading
from collections import deque

from dashing import VSplit, HSplit, HGauge, HChart, VGauge

from .utils import (
    get_soc_info,
    get_ram_metrics_dict,
    run_powermetrics_process,
    parse_powermetrics,
    clear_console,
)


# ---------------------------------------------------------------------------
# Keyboard listener
# ---------------------------------------------------------------------------

def _keyboard_listener(stop_event: threading.Event) -> None:
    """Run in a background thread; sets stop_event on ESC / q / Q."""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while not stop_event.is_set():
            if select.select([sys.stdin], [], [], 0.1)[0]:
                ch = sys.stdin.read(1)
                if ch in ("\x1b", "q", "Q"):
                    stop_event.set()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


# ---------------------------------------------------------------------------
# UI construction
# ---------------------------------------------------------------------------

def _build_ui(args, soc_info: dict):
    """Create and return all dashing widgets and the top-level UI split."""
    color = args.color
    e_core_count = soc_info["e_core_count"]
    p_core_count = soc_info["p_core_count"]

    # Processor gauges
    cpu1_gauge = HGauge(title="E-CPU Usage", val=0, color=color)
    cpu2_gauge = HGauge(title="P-CPU Usage", val=0, color=color)
    gpu_gauge  = HGauge(title="GPU Usage",   val=0, color=color)
    ane_gauge  = HGauge(title="ANE",         val=0, color=color)

    e_core_gauges = [
        VGauge(val=0, color=color, border_color=color)
        for _ in range(e_core_count)
    ]
    p_core_gauges = [
        VGauge(val=0, color=color, border_color=color)
        for _ in range(min(p_core_count, 8))
    ]
    p_core_split = [HSplit(*p_core_gauges)]

    p_core_gauges_ext = []
    if p_core_count > 8:
        p_core_gauges_ext = [
            VGauge(val=0, color=color, border_color=color)
            for _ in range(p_core_count - 8)
        ]
        p_core_split.append(HSplit(*p_core_gauges_ext))

    if args.show_cores:
        processor_gauges = [
            cpu1_gauge, HSplit(*e_core_gauges),
            cpu2_gauge, *p_core_split,
            gpu_gauge, ane_gauge,
        ]
    else:
        processor_gauges = [
            HSplit(cpu1_gauge, cpu2_gauge),
            HSplit(gpu_gauge, ane_gauge),
        ]

    processor_split = VSplit(
        *processor_gauges,
        title="Processor Utilization",
        border_color=color,
    )

    # Memory
    ram_gauge = HGauge(title="RAM Usage", val=0, color=color)
    memory_gauges = VSplit(ram_gauge, border_color=color, title="Memory")

    # Power charts
    cpu_power_chart = HChart(title="CPU Power", color=color)
    gpu_power_chart = HChart(title="GPU Power", color=color)

    if args.show_cores:
        power_charts = VSplit(
            cpu_power_chart, gpu_power_chart,
            title="Power Chart", border_color=color,
        )
    else:
        power_charts = HSplit(
            cpu_power_chart, gpu_power_chart,
            title="Power Chart", border_color=color,
        )

    # Top-level layout
    if args.show_cores:
        ui = HSplit(processor_split, VSplit(memory_gauges, power_charts))
    else:
        ui = VSplit(processor_split, memory_gauges, power_charts)

    widgets = {
        "cpu1_gauge":        cpu1_gauge,
        "cpu2_gauge":        cpu2_gauge,
        "gpu_gauge":         gpu_gauge,
        "ane_gauge":         ane_gauge,
        "e_core_gauges":     e_core_gauges,
        "p_core_gauges":     p_core_gauges,
        "p_core_gauges_ext": p_core_gauges_ext,
        "ram_gauge":         ram_gauge,
        "cpu_power_chart":   cpu_power_chart,
        "gpu_power_chart":   gpu_power_chart,
        "power_charts":      power_charts,
        "processor_split":   processor_split,
    }
    return ui, widgets




# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="mxtop: Performance monitoring CLI tool for Apple Silicon"
    )
    parser.add_argument(
        "--interval", type=int, default=1,
        help="Display interval and sampling interval for powermetrics (seconds)",
    )
    parser.add_argument(
        "--color", type=int, default=2,
        help="Choose display color (0-8)",
    )
    parser.add_argument(
        "--avg", type=int, default=30,
        help="Interval for averaged values (seconds)",
    )
    parser.add_argument(
        "--show_cores", type=bool, default=False,
        help="Show individual core utilization",
    )
    parser.add_argument(
        "--max_count", type=int, default=0,
        help="Max sample count before restarting powermetrics (0 = unlimited)",
    )
    args = parser.parse_args()

    print("\nmxtop - Performance monitoring CLI tool for Apple Silicon")
    print("Press  q  or  ESC  to quit.")
    print("Run with `sudo mxtop` for full metrics.\n")
    print("\n[1/3] Loading mxtop\n")
    print("\033[?25l")  # hide cursor

    soc_info = get_soc_info()
    ui, w = _build_ui(args, soc_info)

    w["processor_split"].title = (
        f"{soc_info['name']} "
        f"(cores: {soc_info['e_core_count']}E+"
        f"{soc_info['p_core_count']}P+"
        f"{soc_info['gpu_core_count']}GPU)"
    )

    cpu_max_power = soc_info["cpu_max_power"]
    gpu_max_power = soc_info["gpu_max_power"]
    ane_max_power = 8.0

    print("\n[2/3] Starting powermetrics process\n")
    timecode = str(int(time.time()))
    powermetrics_process = run_powermetrics_process(timecode, interval=args.interval * 1000)

    print("\n[3/3] Waiting for first reading...\n")

    def get_reading(wait=0.1):
        result = parse_powermetrics(timecode=timecode)
        while not result:
            time.sleep(wait)
            result = parse_powermetrics(timecode=timecode)
        return result

    ready = get_reading()
    last_timestamp = ready[-1]

    # Rolling averages
    maxlen = max(1, int(args.avg / args.interval))
    avg_package_power_list = deque(maxlen=maxlen)
    avg_cpu_power_list     = deque(maxlen=maxlen)
    avg_gpu_power_list     = deque(maxlen=maxlen)

    cpu_peak_power     = 0.0
    gpu_peak_power     = 0.0
    package_peak_power = 0.0

    # Start keyboard listener thread
    stop_event = threading.Event()
    kb_thread = threading.Thread(target=_keyboard_listener, args=(stop_event,), daemon=True)
    kb_thread.start()

    clear_console()
    count = 0

    try:
        while not stop_event.is_set():
            # Optionally restart powermetrics periodically
            if args.max_count > 0:
                if count >= args.max_count:
                    count = 0
                    powermetrics_process.terminate()
                    timecode = str(int(time.time()))
                    powermetrics_process = run_powermetrics_process(
                        timecode, interval=args.interval * 1000
                    )
                count += 1

            ready = parse_powermetrics(timecode=timecode)
            if ready:
                cpu_metrics, gpu_metrics, thermal_pressure, _, timestamp = ready

                if timestamp <= last_timestamp:
                    time.sleep(args.interval)
                    continue
                last_timestamp = timestamp

                thermal_throttle = "no" if thermal_pressure == "Nominal" else "yes"

                # CPU cluster gauges
                w["cpu1_gauge"].title = (
                    f"E-CPU Usage: {cpu_metrics['E-Cluster_active']}%"
                    f" @ {cpu_metrics['E-Cluster_freq_Mhz']} MHz"
                )
                w["cpu1_gauge"].value = cpu_metrics["E-Cluster_active"]

                w["cpu2_gauge"].title = (
                    f"P-CPU Usage: {cpu_metrics['P-Cluster_active']}%"
                    f" @ {cpu_metrics['P-Cluster_freq_Mhz']} MHz"
                )
                w["cpu2_gauge"].value = cpu_metrics["P-Cluster_active"]

                # Per-core gauges (show_cores mode only)
                if args.show_cores:
                    for idx, i in enumerate(cpu_metrics["e_core"]):
                        g = w["e_core_gauges"][idx % 4]
                        g.title = f"Core-{i+1} {cpu_metrics[f'E-Cluster{i}_active']}%"
                        g.value = cpu_metrics[f"E-Cluster{i}_active"]

                    for idx, i in enumerate(cpu_metrics["p_core"]):
                        gauges = w["p_core_gauges"] if idx < 8 else w["p_core_gauges_ext"]
                        prefix = "Core-" if soc_info["p_core_count"] < 6 else "C-"
                        gauges[idx % 8].title = f"{prefix}{i+1} {cpu_metrics[f'P-Cluster{i}_active']}%"
                        gauges[idx % 8].value = cpu_metrics[f"P-Cluster{i}_active"]

                # GPU gauge
                w["gpu_gauge"].title = (
                    f"GPU Usage: {gpu_metrics['active']}%"
                    f" @ {gpu_metrics['freq_MHz']} MHz"
                )
                w["gpu_gauge"].value = gpu_metrics["active"]

                # ANE gauge
                ane_util = int(cpu_metrics["ane_W"] / args.interval / ane_max_power * 100)
                ane_w = cpu_metrics["ane_W"] / args.interval
                w["ane_gauge"].title = f"ANE Usage: {ane_util}% @ {ane_w:.1f} W"
                w["ane_gauge"].value = ane_util

                # RAM gauge
                ram = get_ram_metrics_dict()
                if ram["swap_total_GB"] < 0.1:
                    swap_str = "swap inactive"
                else:
                    swap_str = f"swap: {ram['swap_used_GB']}/{ram['swap_total_GB']} GB"
                w["ram_gauge"].title = f"RAM Usage: {ram['used_GB']}/{ram['total_GB']} GB — {swap_str}"
                w["ram_gauge"].value = ram["free_percent"]

                # Power charts
                pkg_w = cpu_metrics["package_W"] / args.interval
                package_peak_power = max(package_peak_power, pkg_w)
                avg_package_power_list.append(pkg_w)
                avg_pkg = sum(avg_package_power_list) / len(avg_package_power_list)
                w["power_charts"].title = (
                    f"CPU+GPU+ANE Power: {pkg_w:.2f} W"
                    f" (avg: {avg_pkg:.2f} W  peak: {package_peak_power:.2f} W)"
                    f"  throttle: {thermal_throttle}"
                )

                cpu_w = cpu_metrics["cpu_W"] / args.interval
                cpu_peak_power = max(cpu_peak_power, cpu_w)
                avg_cpu_power_list.append(cpu_w)
                avg_cpu = sum(avg_cpu_power_list) / len(avg_cpu_power_list)
                w["cpu_power_chart"].title = (
                    f"CPU: {cpu_w:.2f} W (avg: {avg_cpu:.2f} W  peak: {cpu_peak_power:.2f} W)"
                )
                w["cpu_power_chart"].append(int(cpu_w / cpu_max_power * 100))

                gpu_w = cpu_metrics["gpu_W"] / args.interval
                gpu_peak_power = max(gpu_peak_power, gpu_w)
                avg_gpu_power_list.append(gpu_w)
                avg_gpu = sum(avg_gpu_power_list) / len(avg_gpu_power_list)
                w["gpu_power_chart"].title = (
                    f"GPU: {gpu_w:.2f} W (avg: {avg_gpu:.2f} W  peak: {gpu_peak_power:.2f} W)"
                )
                w["gpu_power_chart"].append(int(gpu_w / gpu_max_power * 100))

                ui.display()

            time.sleep(args.interval)

    finally:
        stop_event.set()
        print("\033[?25h")  # restore cursor
        print("\nStopped.")

    return powermetrics_process


if __name__ == "__main__":
    proc = main()
    try:
        proc.terminate()
    except Exception:
        pass
