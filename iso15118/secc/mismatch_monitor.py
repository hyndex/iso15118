import time
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class SteadyMismatchResult:
    ok: bool
    action: str  # "continue" | "warn" | "abort"
    reason: str = ""


class PowerMismatchMonitor:
    """
    Session-scoped monitor to detect power delivery mismatches.

    - Tracks precharge progress against a target voltage and times out
      if the target is not reached within configured bounds.
    - During steady CurrentDemand, compares EV requested setpoints with
      measured EVSE values and escalates if deviation persists.
    """

    def __init__(
        self,
        *,
        precharge_tol_v: float = 20.0,
        precharge_timeout_s: float = 10.0,
        steady_v_tol_frac: float = 0.05,
        steady_i_tol_frac: float = 0.05,
        mismatch_grace_s: float = 0.5,
        mismatch_abort_s: float = 2.0,
        min_current_for_check_a: float = 2.0,
    ):
        # Configurable thresholds
        self.precharge_tol_v = max(0.0, float(precharge_tol_v))
        self.precharge_timeout_s = max(0.0, float(precharge_timeout_s))
        self.steady_v_tol_frac = max(0.0, float(steady_v_tol_frac))
        self.steady_i_tol_frac = max(0.0, float(steady_i_tol_frac))
        self.mismatch_grace_s = max(0.0, float(mismatch_grace_s))
        self.mismatch_abort_s = max(0.0, float(mismatch_abort_s))
        self.min_current_for_check_a = max(0.0, float(min_current_for_check_a))

        # State
        self._precharge_started_mono: Optional[float] = None
        self._precharge_target_v: float = 0.0
        self._last_command_v: float = 0.0
        self._last_command_i: float = 0.0
        self._mismatch_started_mono: Optional[float] = None

    def reset(self) -> None:
        self._precharge_started_mono = None
        self._precharge_target_v = 0.0
        self._last_command_v = 0.0
        self._last_command_i = 0.0
        self._mismatch_started_mono = None

    # --- Precharge monitoring ---
    def begin_precharge(self, target_voltage_v: float, now_mono: Optional[float] = None) -> None:
        """Mark precharge as started (idempotent)."""
        self._precharge_target_v = float(target_voltage_v or 0.0)
        if self._precharge_started_mono is None:
            self._precharge_started_mono = time.monotonic() if now_mono is None else now_mono

    def check_precharge(
        self,
        measured_voltage_v: float,
        measured_current_a: float,
        now_mono: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """
        Returns (ok, reason). ok=False indicates timeout/mismatch.
        Succeeds if |V_meas - V_target| <= tol.
        If timeout elapses without reaching tolerance, returns False.
        """
        if self._precharge_started_mono is None:
            # Not started; treat as ok
            return True, ""
        now = time.monotonic() if now_mono is None else now_mono
        dv = abs(float(measured_voltage_v or 0.0) - self._precharge_target_v)
        if dv <= self.precharge_tol_v:
            return True, ""
        # Still outside tolerance: check time budget
        if (now - self._precharge_started_mono) >= self.precharge_timeout_s > 0:
            # Special case: if current is essentially zero and voltage not converging,
            # likely open circuit / EV not closed. Fail as precharge not established.
            return False, "Precharge timeout: voltage did not reach target within tolerance"
        return True, ""

    # --- Steady (CurrentDemand) monitoring ---
    def record_command(self, target_voltage_v: float, target_current_a: float) -> None:
        self._last_command_v = float(target_voltage_v or 0.0)
        self._last_command_i = float(target_current_a or 0.0)

    def _within_frac(self, diff: float, ref: float, frac: float) -> bool:
        if ref <= 0:
            return abs(diff) <= 1e-6
        return abs(diff) <= (abs(ref) * frac)

    def check_steady(
        self,
        measured_voltage_v: float,
        measured_current_a: float,
        ev_target_voltage_v: float,
        ev_target_current_a: float,
        now_mono: Optional[float] = None,
    ) -> SteadyMismatchResult:
        """
        Compares measured vs EV requested setpoints with tolerance and persistence.

        - Ignores current checks if both requested and measured current are very low
          (< min_current_for_check_a), to avoid noise at idle.
        - Starts a mismatch window on first out-of-tolerance sample; if still
          out after grace, returns warn; if persists past abort_s, returns abort.
        """
        now = time.monotonic() if now_mono is None else now_mono
        v_req = float(ev_target_voltage_v or 0.0)
        i_req = float(ev_target_current_a or 0.0)
        v_meas = float(measured_voltage_v or 0.0)
        i_meas = float(measured_current_a or 0.0)

        v_ok = self._within_frac(v_meas - v_req, max(v_req, 1.0), self.steady_v_tol_frac)

        # For current checks, if both req and meas below threshold, skip
        if abs(i_req) < self.min_current_for_check_a and abs(i_meas) < self.min_current_for_check_a:
            i_ok = True
        else:
            i_ok = self._within_frac(i_meas - i_req, max(abs(i_req), 1.0), self.steady_i_tol_frac)

        if v_ok and i_ok:
            # Clear any ongoing mismatch window
            self._mismatch_started_mono = None
            return SteadyMismatchResult(ok=True, action="continue", reason="")

        # Start or continue mismatch window
        start = self._mismatch_started_mono
        if start is None:
            self._mismatch_started_mono = now
            return SteadyMismatchResult(ok=True, action="continue", reason="")

        elapsed = now - start
        if elapsed >= self.mismatch_abort_s > 0:
            return SteadyMismatchResult(
                ok=False,
                action="abort",
                reason="Persistent power mismatch beyond tolerance",
            )
        if elapsed >= self.mismatch_grace_s > 0:
            return SteadyMismatchResult(
                ok=True,
                action="warn",
                reason="Transient power mismatch beyond tolerance",
            )
        return SteadyMismatchResult(ok=True, action="continue", reason="")

