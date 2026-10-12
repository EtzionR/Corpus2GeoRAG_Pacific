"""Continue the approved EventKG V3.2 scan until all scoped files finish.

Usage: python -u scripts/run_wwii_task_force_full_scan.py
Each source has a separate log and resumable state under Data/external/.
"""

from __future__ import annotations

import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from filter_eventkg_related import BASE, DIRECT_SOURCES, REIFIED_SOURCES

SCRIPT = Path(__file__).resolve().parent / "filter_eventkg_related.py"
MAP_SCRIPT = SCRIPT.with_name("filter_eventkg_v3_2.py")
DATA = BASE / "eventkg_v3_2"
PRIORITY = (
    "relations_events_other.nq",
    "relations_entities_other.nq",
    "relations_entities_temporal.nq",
    "relations_events_literals.nq",
    "relations_events_base.nq",
    "relations_entities_base.nq",
)
SOURCES = PRIORITY + tuple(name for name in DIRECT_SOURCES + REIFIED_SOURCES if name not in PRIORITY)


def scan_one(source: str, script: Path) -> tuple[str, bool]:
    out = DATA / source.removesuffix(".nq")
    out.mkdir(parents=True, exist_ok=True)
    log_file = out / "scan.log"
    if (out / "manifest.json").exists():
        return source, True
    for attempt in range(10):
        with log_file.open("a", encoding="utf-8") as log:
            log.write(f"Start attempt {attempt + 1} for {source}\n")
            log.flush()
            result = subprocess.run([sys.executable, "-u", str(script), source], cwd=SCRIPT.parents[1], stdout=log, stderr=subprocess.STDOUT, check=False)
            log.write(f"Exit {result.returncode}\n")
        if result.returncode == 0 and (out / "manifest.json").exists():
            return source, True
        time.sleep(60)
    return source, False


def main() -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        maps = [pool.submit(scan_one, name, MAP_SCRIPT) for name in ("events.nq", "entities.nq")]
        for future in as_completed(maps):
            name, success = future.result()
            print(f"{name}: {'complete' if success else 'FAILED'}", flush=True)
            if not success:
                raise RuntimeError(f"EventKG QID map incomplete: {name}")
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(scan_one, name, SCRIPT): name for name in SOURCES}
        failed = []
        for future in as_completed(futures):
            name, success = future.result()
            print(f"{name}: {'complete' if success else 'FAILED'}", flush=True)
            if not success:
                failed.append(name)
    if failed:
        raise RuntimeError(f"EventKG files incomplete: {', '.join(failed)}")
    print(f"EventKG full scoped scan complete: {len(SOURCES) + 2} source files", flush=True)


if __name__ == "__main__":
    main()
