from __future__ import annotations

import importlib.util
from pathlib import Path

logic_path = Path(__file__).parent / "custom_components" / "1fuseloader" / "logic.py"
spec = importlib.util.spec_from_file_location("onefuse_logic", logic_path)
logic = importlib.util.module_from_spec(spec)
assert spec and spec.loader
import sys
sys.modules[spec.name] = logic
spec.loader.exec_module(logic)

OneFuseRegulator = logic.OneFuseRegulator
RegulatorInput = logic.RegulatorInput
RegulatorSettings = logic.RegulatorSettings


def run_case(name, regulator, now, status, l1, charger_current=0):
    d = regulator.evaluate(RegulatorInput(now_ts=now, status=status, grid_l1=l1, charger_current=charger_current))
    print(f"{name:34s} mode={d.mode:18s} command={str(d.command):9s} call={str(d.should_call_service):5s} p1={d.current_p1:2d} avail={d.available_l1:2d} house={d.house_load_l1:5.1f} reason={d.reason}")
    # This script simulates a successful API submission; evaluating a proposal
    # alone must never record a limit, pause ownership, or a completed command.
    regulator.acknowledge(d, now)
    return d


def assert_eq(actual, expected, msg):
    if actual != expected:
        raise AssertionError(f"{msg}: expected {expected!r}, got {actual!r}")


def main():
    print("1FuseLoader logic test\n")

    # Dry run off here so we can test whether it WOULD call services.
    settings = RegulatorSettings(active_control=True, dry_run=False)
    r = OneFuseRegulator(settings)

    # Old template equivalent:
    # main 25 - house_load(16 - 8 = 8) - buffer 2 = 15A
    d = run_case("normal available", r, 0, "charging", l1=16, charger_current=8)
    assert_eq(d.current_p1, 15, "normal available current")
    assert_eq(d.command, "set_limit", "normal command")

    # Same target immediately afterwards should not spam service calls.
    d = run_case("no immediate spam", r, 1, "charging", l1=16, charger_current=8)
    assert_eq(d.should_call_service, False, "should not resend same target immediately")

    # Less than 6 A of headroom never forces an extra 6 A onto the house load.
    d = run_case("keep min under safe", r, 10, "charging", l1=22, charger_current=0)
    assert_eq(d.current_p1, 0, "insufficient headroom remains zero")

    # Avail < 6 and L1 not safe -> 0A.
    d = run_case("block when not safe", r, 20, "charging", l1=24, charger_current=0)
    assert_eq(d.current_p1, 0, "0A when too little room and not safe")

    # Soft guard: it may cap at 6 A, but must not increase a zero target.
    r = OneFuseRegulator(settings)
    run_case("soft start", r, 100, "charging", l1=29, charger_current=6)
    d = run_case("soft reached", r, 121, "charging", l1=29, charger_current=6)
    assert_eq(d.mode, "soft_limit", "soft guard mode")
    assert_eq(d.current_p1, 0, "soft guard cannot increase zero headroom")

    # Pause guard: L1 > 28A for 120s -> Pause.
    r = OneFuseRegulator(settings)
    run_case("pause start", r, 200, "charging", l1=29, charger_current=6)
    d = run_case("pause reached", r, 321, "charging", l1=29, charger_current=6)
    assert_eq(d.command, "pause", "pause command")
    # Model the charger's feedback promptly; an unobserved pause submission
    # must not acquire ownership of a much later manual pause.
    run_case("pause observed", r, 326, "awaiting_start", l1=24, charger_current=0)

    # Only our accepted pause resumes, with sufficient headroom and a fresh cap.
    run_case("resume start", r, 400, "awaiting_start", l1=10, charger_current=0)
    d = run_case("refresh before resume", r, 521, "awaiting_start", l1=10, charger_current=0)
    assert_eq(d.command, "set_limit", "refresh the cap before resuming")
    d = run_case("resume reached", r, 527, "awaiting_start", l1=10, charger_current=0)
    assert_eq(d.command, "resume", "resume command")

    # Invalid L1 sensor -> best-effort 0 A, never an increase or a resume.
    d = run_case("bad L1", r, 600, "charging", l1="unavailable", charger_current=0)
    assert_eq(d.command, "set_limit", "bad L1 requests a protective zero cap")
    assert_eq(d.current_p1, 0, "bad L1 target")

    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
