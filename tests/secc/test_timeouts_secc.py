import asyncio
import pytest

from iso15118.shared.notifications import StopNotification
from iso15118.secc.comm_session_handler import SECCCommunicationSession
from iso15118.secc.secc_settings import Config
from iso15118.secc.controller.interface import (
    EVSEControllerInterface,
    ServiceStatus,
    SessionStopAction,
)


class _DummyEVSEController(EVSEControllerInterface):
    def __init__(self):
        super().__init__()
        self.stop_charger_called = False
        self.session_ended_called = False
        self.update_data_link_calls = []

    async def set_status(self, status: ServiceStatus) -> None:
        return None

    async def get_evse_id(self, protocol):
        return "EVSE-TEST-01"

    async def get_supported_energy_transfer_modes(self, protocol):
        return []

    async def get_schedule_exchange_params(self, *args, **kwargs):
        return None

    async def get_energy_service_list(self):
        return None

    def is_eim_authorized(self) -> bool:
        return True

    async def is_authorized(self, *args, **kwargs):
        class _Resp:
            authorization_status = None
            certificate_response_status = None

        return _Resp()

    async def get_sa_schedule_list(self, *args, **kwargs):
        return None

    async def get_sa_schedule_list_dinspec(self, *args, **kwargs):
        return None

    async def get_meter_info_v2(self):
        return None

    async def get_meter_info_v20(self):
        return None

    async def get_supported_providers(self):
        return None

    async def set_hlc_charging(self, is_ongoing: bool) -> None:
        return None

    async def get_cp_state(self):
        # Default to connected state semantics for test simplicity
        from iso15118.shared.messages.enums import CpState

        return CpState.C2

    async def service_renegotiation_supported(self) -> bool:
        return False

    async def stop_charger(self) -> None:
        self.stop_charger_called = True

    async def set_present_protocol_state(self, state):
        return None

    async def get_ac_evse_status(self):
        return None

    async def get_ac_charge_params_v2(self):
        return None

    async def get_dc_evse_status(self):
        return None

    async def get_dc_charge_params_v2(self):
        return None

    async def get_dc_charge_parameter_limits_v20(self, *args, **kwargs):
        return None

    async def get_ac_charge_parameter_limits_v20(self, *args, **kwargs):
        return None

    async def get_dc_charge_loop_params_v20(self, *args, **kwargs):
        return None

    async def get_ac_charge_loop_params_v20(self, *args, **kwargs):
        return None

    async def get_15118_ev_certificate(self, *args, **kwargs) -> str:
        return ""

    async def update_data_link(self, action: SessionStopAction) -> None:
        self.update_data_link_calls.append(action)

    def ready_to_charge(self) -> bool:
        return True

    async def session_ended(self, current_state: str, reason: str):
        self.session_ended_called = True

    async def send_display_params(self):
        return None

    async def send_rated_limits(self):
        return None

    # Additional abstract API stubs to satisfy interface
    async def get_service_parameter_list(self, service_id: int):
        return None

    async def get_dc_charge_parameters(self):
        return None

    async def start_cable_check(self):
        return None

    async def get_cable_check_status(self):
        return None

    async def send_charging_command(
        self,
        evse_present_voltage=None,
        evse_present_current=None,
    ):
        return None

    async def is_evse_current_limit_achieved(self):
        return False

    async def is_evse_voltage_limit_achieved(self):
        return False

    async def is_evse_power_limit_achieved(self) -> bool:
        return False


async def _start_idle_server(host: str, port: int):
    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        # Keep connection open; do not send any bytes
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(_handle, host, port)
    return server


@pytest.mark.asyncio
async def test_secc_timeout_triggers_safe_stop(event_loop):
    # Launch idle server (mock EV)
    host = "127.0.0.1"
    server = await _start_idle_server(host, 0)
    sockets = server.sockets or []
    assert sockets
    port = sockets[0].getsockname()[1]

    try:
        # Connect as SECC side to the idle server
        reader, writer = await asyncio.open_connection(host, port)
        notify_q: asyncio.Queue = asyncio.Queue()
        cfg = Config()
        evse = _DummyEVSEController()

        # Create SECC session and start with a short timeout
        secc = SECCCommunicationSession(
            (reader, writer), notify_q, cfg, evse, evse_id="EVSE-TEST-01"
        )

        # Run with a very short timeout to make the test fast
        task = asyncio.create_task(secc.start(timeout=0.5))

        # Expect a StopNotification due to asyncio.TimeoutError and clean shutdown
        notif: StopNotification = await asyncio.wait_for(notify_q.get(), timeout=2.0)
        assert isinstance(notif, StopNotification)
        assert notif.successful is False
        # Verify safe-state transition: stop_charger must be called
        assert evse.stop_charger_called is True

        # Session task should finish
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        server.close()
        await server.wait_closed()
