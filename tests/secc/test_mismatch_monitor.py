import time

import pytest

from iso15118.secc.mismatch_monitor import PowerMismatchMonitor


def test_precharge_ok_within_tolerance():
    mon = PowerMismatchMonitor(precharge_tol_v=20.0, precharge_timeout_s=5.0)
    t0 = 100.0
    mon.begin_precharge(400.0, now_mono=t0)
    ok, reason = mon.check_precharge(385.0, measured_current_a=1.0, now_mono=t0 + 0.2)
    assert ok is True
    assert reason == ""


def test_precharge_timeout_mismatch():
    mon = PowerMismatchMonitor(precharge_tol_v=10.0, precharge_timeout_s=0.5)
    t0 = 200.0
    mon.begin_precharge(400.0, now_mono=t0)
    # Still far after timeout
    ok, reason = mon.check_precharge(360.0, measured_current_a=0.1, now_mono=t0 + 0.6)
    assert ok is False
    assert "timeout" in reason.lower()


def test_steady_ok_and_warn_and_abort_sequence():
    mon = PowerMismatchMonitor(
        steady_v_tol_frac=0.05,
        steady_i_tol_frac=0.05,
        mismatch_grace_s=0.2,
        mismatch_abort_s=0.5,
        min_current_for_check_a=2.0,
    )
    t0 = 1000.0
    # Exact match initially
    r1 = mon.check_steady(400.0, 100.0, 400.0, 100.0, now_mono=t0)
    assert r1.ok and r1.action == "continue"
    # Small deviation under tolerance
    r2 = mon.check_steady(410.0, 104.0, 400.0, 100.0, now_mono=t0 + 0.1)
    assert r2.ok and r2.action == "continue"
    # Big deviation starts mismatch window
    r3 = mon.check_steady(500.0, 30.0, 400.0, 100.0, now_mono=t0 + 0.15)
    assert r3.ok and r3.action == "continue"
    # After grace -> warn
    r4 = mon.check_steady(500.0, 30.0, 400.0, 100.0, now_mono=t0 + 0.35)
    assert r4.ok and r4.action == "warn"
    # After abort window -> abort
    r5 = mon.check_steady(500.0, 30.0, 400.0, 100.0, now_mono=t0 + 0.7)
    assert (not r5.ok) and r5.action == "abort"

