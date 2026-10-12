#!/usr/bin/env python3

import argparse
import curses
import locale
import os
import platform
import socket
import sys
import time

try:
    import psutil
except ImportError:
    sys.exit("psutil is required: pip install psutil")


def human_bytes(n):
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024.0:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}P"


def human_duration(seconds):
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def text_bar(pct, width):
    width = max(width, 1)
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100.0))
    return "█" * filled + "░" * (width - filled)


class ProcessSampler:

    def __init__(self):
        self._procs = {}

    def sample(self):
        rows = []
        live = set(psutil.pids())

        for pid in list(self._procs):
            if pid not in live:
                del self._procs[pid]

        for pid in live:
            proc = self._procs.get(pid)
            if proc is None:
                try:
                    proc = psutil.Process(pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                self._procs[pid] = proc

            try:
                with proc.oneshot():
                    try:
                        user = proc.username()
                    except (psutil.AccessDenied, KeyError):
                        user = "?"
                    rows.append({
                        "pid": pid,
                        "name": proc.name(),
                        "user": user,
                        "cpu": proc.cpu_percent(None),
                        "mem": proc.memory_percent(),
                        "rss": proc.memory_info().rss,
                        "threads": proc.num_threads(),
                        "status": proc.status(),
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                self._procs.pop(pid, None)
                continue
        return rows


def collect_system():
    try:
        load = os.getloadavg()
    except (OSError, AttributeError):
        load = (0.0, 0.0, 0.0)

    freq = None
    try:
        f = psutil.cpu_freq()
        freq = f.current if f else None
    except Exception:
        pass

    return {
        "cpu_total": psutil.cpu_percent(None),
        "cpu_per_core": psutil.cpu_percent(None, percpu=True),
        "load": load,
        "freq": freq,
        "mem": psutil.virtual_memory(),
        "swap": psutil.swap_memory(),
        "uptime": time.time() - psutil.boot_time(),
    }


def sort_rows(rows, key, reverse):
    keyfuncs = {
        "cpu": lambda r: r["cpu"],
        "mem": lambda r: r["mem"],
        "pid": lambda r: r["pid"],
        "name": lambda r: r["name"].lower(),
    }
    natural_desc = key in ("cpu", "mem")
    return sorted(rows, key=keyfuncs[key], reverse=natural_desc != reverse)


def status_counts(rows):
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return counts


def run_once(top_n):
    sampler = ProcessSampler()
    sampler.sample()
    psutil.cpu_percent(None, percpu=True)
    time.sleep(1.0)
    sysinfo = collect_system()
    rows = sort_rows(sampler.sample(), "cpu", False)

    mem, swap = sysinfo["mem"], sysinfo["swap"]
    print(f"{socket.gethostname()}  |  {platform.system()} {platform.release()}  |  "
          f"up {human_duration(sysinfo['uptime'])}")
    print(f"Load avg: {sysinfo['load'][0]:.2f} {sysinfo['load'][1]:.2f} {sysinfo['load'][2]:.2f}")
    print()
    print(f"CPU total  [{text_bar(sysinfo['cpu_total'], 30)}] {sysinfo['cpu_total']:5.1f}%")
    for i, pct in enumerate(sysinfo["cpu_per_core"]):
        print(f"  core {i:<3} [{text_bar(pct, 30)}] {pct:5.1f}%")
    print()
    print(f"Memory     [{text_bar(mem.percent, 30)}] {mem.percent:5.1f}%  "
          f"{human_bytes(mem.used)} / {human_bytes(mem.total)}")
    print(f"Swap       [{text_bar(swap.percent, 30)}] {swap.percent:5.1f}%  "
          f"{human_bytes(swap.used)} / {human_bytes(swap.total)}")
    print()
    counts = status_counts(rows)
    print(f"Processes: {len(rows)} total  (" +
          ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + ")")
    print()
    print(f"{'PID':>7} {'USER':<12} {'CPU%':>6} {'MEM%':>6} {'RSS':>8} {'THR':>4} {'STATE':<9} NAME")
    for r in rows[:top_n]:
        print(f"{r['pid']:>7} {r['user'][:12]:<12} {r['cpu']:>6.1f} {r['mem']:>6.1f} "
              f"{human_bytes(r['rss']):>8} {r['threads']:>4} {r['status']:<9} {r['name']}")


COLOR_OK, COLOR_WARN, COLOR_CRIT, COLOR_HEAD, COLOR_SEL = 1, 2, 3, 4, 5


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(COLOR_OK, curses.COLOR_GREEN, -1)
    curses.init_pair(COLOR_WARN, curses.COLOR_YELLOW, -1)
    curses.init_pair(COLOR_CRIT, curses.COLOR_RED, -1)
    curses.init_pair(COLOR_HEAD, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(COLOR_SEL, curses.COLOR_CYAN, -1)


def level_color(pct):
    if pct >= 80:
        return curses.color_pair(COLOR_CRIT)
    if pct >= 50:
        return curses.color_pair(COLOR_WARN)
    return curses.color_pair(COLOR_OK)


def put(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    try:
        win.addstr(y, x, text[: max(w - x, 0)], attr)
    except curses.error:
        pass


def draw_meter(win, y, x, label, pct, width, suffix=""):
    label_w = len(label)
    bar_w = max(width - label_w - len(suffix) - 11, 4)
    put(win, y, x, label, curses.A_BOLD)
    put(win, y, x + label_w + 1, "[")
    put(win, y, x + label_w + 2, text_bar(pct, bar_w), level_color(pct))
    put(win, y, x + label_w + 2 + bar_w, "]")
    put(win, y, x + label_w + 4 + bar_w, f"{pct:5.1f}%")
    if suffix:
        put(win, y, x + label_w + 11 + bar_w, suffix)


def draw(win, sysinfo, rows, state):
    win.erase()
    h, w = win.getmaxyx()
    y = 0

    title = (f" {socket.gethostname()}  {platform.system()} {platform.release()}"
             f"  up {human_duration(sysinfo['uptime'])}")
    load = sysinfo["load"]
    right = f"load {load[0]:.2f} {load[1]:.2f} {load[2]:.2f} "
    put(win, y, 0, title.ljust(w), curses.color_pair(COLOR_HEAD))
    put(win, y, max(w - len(right), len(title) + 1), right, curses.color_pair(COLOR_HEAD))
    y += 2

    freq = f"{sysinfo['freq']:.0f} MHz" if sysinfo["freq"] else ""
    draw_meter(win, y, 1, "CPU", sysinfo["cpu_total"], w - 2, freq)
    y += 1

    cores = sysinfo["cpu_per_core"]
    cols = 1 if w < 80 else (2 if w < 140 else 4)
    colw = (w - 2) // cols
    max_core_rows = max((h - 14) // 1, 1)
    shown = min(len(cores), max_core_rows * cols)
    for i in range(shown):
        cy = y + i // cols
        cx = 1 + (i % cols) * colw
        draw_meter(win, cy, cx, f"{i:>2}", cores[i], colw - 1)
    y += (shown + cols - 1) // cols
    if shown < len(cores):
        put(win, y, 1, f"... {len(cores) - shown} more cores hidden", curses.A_DIM)
        y += 1
    y += 1

    mem, swap = sysinfo["mem"], sysinfo["swap"]
    draw_meter(win, y, 1, "Mem ", mem.percent, w - 2,
               f"{human_bytes(mem.used)}/{human_bytes(mem.total)}")
    y += 1
    draw_meter(win, y, 1, "Swap", swap.percent, w - 2,
               f"{human_bytes(swap.used)}/{human_bytes(swap.total)}")
    y += 2

    counts = status_counts(rows)
    summary = f"Processes: {len(rows)}   " + "  ".join(
        f"{k}:{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
    put(win, y, 1, summary, curses.A_BOLD)
    y += 1

    cols_hdr = f"{'PID':>7} {'USER':<12} {'CPU%':>6} {'MEM%':>6} {'RSS':>8} {'THR':>4} {'STATE':<9} NAME"
    put(win, y, 0, cols_hdr.ljust(w), curses.color_pair(COLOR_HEAD))
    y += 1

    visible = max(h - y - 1, 0)
    state["visible"] = visible
    max_off = max(len(rows) - visible, 0)
    state["offset"] = max(0, min(state["offset"], max_off))
    for i, r in enumerate(rows[state["offset"]: state["offset"] + visible]):
        line = (f"{r['pid']:>7} {r['user'][:12]:<12} {r['cpu']:>6.1f} {r['mem']:>6.1f} "
                f"{human_bytes(r['rss']):>8} {r['threads']:>4} {r['status']:<9} {r['name']}")
        attr = level_color(r["cpu"]) if r["cpu"] >= 50 else 0
        put(win, y + i, 0, line, attr)

    arrow = "asc" if state["reverse"] else "desc"
    if state["sort"] in ("pid", "name"):
        arrow = "desc" if state["reverse"] else "asc"
    paused = "  [PAUSED]" if state["paused"] else ""
    footer = (f" sort:{state['sort']} ({arrow})  c:cpu m:mem p:pid n:name r:reverse "
              f"space:pause q:quit{paused}")
    put(win, h - 1, 0, footer.ljust(w), curses.color_pair(COLOR_HEAD))
    win.refresh()


def curses_main(stdscr, interval):
    locale.setlocale(locale.LC_ALL, "")
    curses.curs_set(0)
    init_colors()
    stdscr.timeout(100)

    sampler = ProcessSampler()
    sampler.sample()
    psutil.cpu_percent(None, percpu=True)
    time.sleep(0.3)

    state = {"sort": "cpu", "reverse": False, "paused": False,
             "offset": 0, "visible": 10}
    sysinfo, rows = collect_system(), sampler.sample()
    last = time.monotonic()

    while True:
        now = time.monotonic()
        if not state["paused"] and now - last >= interval:
            sysinfo, rows = collect_system(), sampler.sample()
            last = now

        draw(stdscr, sysinfo, sort_rows(rows, state["sort"], state["reverse"]), state)

        key = stdscr.getch()
        if key in (ord("q"), ord("Q"), 27):
            return
        elif key == ord("c"):
            state.update(sort="cpu", reverse=False, offset=0)
        elif key == ord("m"):
            state.update(sort="mem", reverse=False, offset=0)
        elif key == ord("p"):
            state.update(sort="pid", reverse=False, offset=0)
        elif key == ord("n"):
            state.update(sort="name", reverse=False, offset=0)
        elif key == ord("r"):
            state["reverse"] = not state["reverse"]
        elif key == ord(" "):
            state["paused"] = not state["paused"]
        elif key == curses.KEY_DOWN:
            state["offset"] += 1
        elif key == curses.KEY_UP:
            state["offset"] -= 1
        elif key == curses.KEY_NPAGE:
            state["offset"] += state["visible"]
        elif key == curses.KEY_PPAGE:
            state["offset"] -= state["visible"]
        elif key == curses.KEY_HOME:
            state["offset"] = 0


def main():
    parser = argparse.ArgumentParser(description="Simple Linux system monitor (psutil).")
    parser.add_argument("-i", "--interval", type=float, default=1.0,
                        help="refresh interval in seconds (default: 1.0, min 0.5)")
    parser.add_argument("--once", action="store_true",
                        help="print a single text snapshot and exit")
    parser.add_argument("-n", "--top", type=int, default=15,
                        help="number of processes to show with --once (default: 15)")
    args = parser.parse_args()

    if args.once or not sys.stdout.isatty():
        run_once(args.top)
        return

    try:
        curses.wrapper(curses_main, max(args.interval, 0.5))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()