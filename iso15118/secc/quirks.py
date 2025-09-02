import logging
from dataclasses import dataclass
from typing import Optional, Union

logger = logging.getLogger(__name__)


@dataclass
class QuirkProfile:
    """
    Session-scoped compatibility flags for known EV quirks.

    - force_include_evse_max_limits: ensure optional max-limit elements are sent
      where allowed by the standard.
    - current_demand_async_hal: reply quickly to CurrentDemand by issuing the
      HAL setpoint update in the background if needed.
    - timeout_multiplier: multiply sequence timeouts slightly for leniency.
    """

    force_include_evse_max_limits: bool = False
    current_demand_async_hal: bool = False
    timeout_multiplier: float = 1.0


def _norm_evcc_id(evcc_id: Union[bytes, str, None]) -> str:
    if evcc_id is None:
        return ""
    if isinstance(evcc_id, bytes):
        try:
            return evcc_id.hex(":").upper()
        except Exception:
            return evcc_id.hex().upper()
    return str(evcc_id).upper()


def identify_ev_quirks(evcc_id: Union[bytes, str, None]) -> QuirkProfile:
    """
    Returns a QuirkProfile for a given EVCC identifier.

    Currently returns a default profile and logs the EVCC ID for telemetry.
    Add model-specific rules here as you discover interoperability quirks.
    """
    ev_id = _norm_evcc_id(evcc_id)
    if ev_id:
        logger.info("EVCC identified: %s", ev_id)

    # Example of future matching logic (left as comments for clarity):
    # if ev_id.startswith("00:1A:7D"):  # Example vendor prefix
    #     return QuirkProfile(
    #         force_include_evse_max_limits=True,
    #         current_demand_async_hal=True,
    #         timeout_multiplier=1.2,
    #     )

    return QuirkProfile()

