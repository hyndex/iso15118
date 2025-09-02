"""
This module contains the SECC's State used to process the EVCC's
SupportedAppProtocolReq. These states are independent of the protocol
(either ISO 15118 or DIN SPEC 70121), as the EVCC and SECC use the
SupportedAppProtocolReq and -Res message pair to mutually agree upon a protocol.
"""

import logging
from typing import Type, Union

from iso15118.secc.comm_session_handler import SECCCommunicationSession
from iso15118.secc.states.din_spec_states import SessionSetup as SessionSetupDINSPEC
from iso15118.secc.states.iso15118_2_states import SessionSetup as SessionSetupV2
from iso15118.secc.states.iso15118_20_states import SessionSetup as SessionSetupV20
from iso15118.secc.states.secc_state import StateSECC
from iso15118.shared.messages.app_protocol import (
    ResponseCodeSAP,
    SupportedAppProtocolReq,
    SupportedAppProtocolRes,
)
from iso15118.shared.messages.din_spec.msgdef import V2GMessage as V2GMessageDINSPEC
from iso15118.shared.messages.enums import Namespace, Protocol
from iso15118.shared.messages.iso15118_2.msgdef import V2GMessage as V2GMessageV2
from iso15118.shared.messages.iso15118_20.common_types import (
    V2GMessage as V2GMessageV20,
)
from iso15118.shared.messages.timeouts import Timeouts
from iso15118.shared.states import State, Terminate

logger = logging.getLogger(__name__)


class SupportedAppProtocol(StateSECC):
    """
    The state in which the SECC processes a SupportedAppProtocolReq from
    the EVCC to agree upon a mutually supported ISO 15118 version.
    """

    def __init__(self, comm_session: SECCCommunicationSession):
        # TODO: less the time used for waiting for and processing the SDPRequest
        super().__init__(comm_session, Timeouts.V2G_EVCC_COMMUNICATION_SETUP_TIMEOUT)

    async def process_message(
        self,
        message: Union[
            SupportedAppProtocolReq,
            SupportedAppProtocolRes,
            V2GMessageV2,
            V2GMessageV20,
            V2GMessageDINSPEC,
        ],
        message_exi: bytes = None,
    ):
        msg = self.check_msg(
            message, SupportedAppProtocolReq, [SupportedAppProtocolReq]
        )
        if not msg:
            return

        sap_req: SupportedAppProtocolReq = msg

        # Capture EV-offered protocols for diagnostics
        ev_offers = [
            {
                "ns": p.protocol_ns,
                "major": p.major_version,
                "minor": p.minor_version,
                "priority": p.priority,
                "schema_id": p.schema_id,
            }
            for p in sap_req.app_protocol
        ]
        # Decide selection order: EV priority or EVSE preference
        if getattr(self.comm_session.config, "sap_prefer_ev_priority", True):
            sap_req.app_protocol.sort(key=lambda proto: proto.priority)
            candidates = sap_req.app_protocol
        else:
            # Build candidate list in EVSE preferred order, filtered by EV offers
            # Map EV-offered namespaces to list of entries for quick lookup
            ev_index = {}
            for p in sap_req.app_protocol:
                ev_index.setdefault(p.protocol_ns, []).append(p)
            preferred = []
            for proto in (self.comm_session.config.supported_protocols or []):
                ns = proto.ns.value
                # Collect matching EV offers for this ns
                for p in ev_index.get(ns, []):
                    preferred.append(p)
            candidates = preferred if preferred else sap_req.app_protocol

        sap_res: Union[SupportedAppProtocolRes, None] = None
        supported_ns_list = [
            protocol.ns.value
            for protocol in self.comm_session.config.supported_protocols
        ]
        next_state: Type[State] = Terminate  # some default that is not None

        selected_protocol = Protocol.UNKNOWN
        for protocol in candidates:
            if protocol.protocol_ns in supported_ns_list:
                if (
                    protocol.protocol_ns == Protocol.ISO_15118_2.ns.value
                    and protocol.major_version == 2
                ):
                    selected_protocol = Protocol.get_by_ns(protocol.protocol_ns)
                    next_state = SessionSetupV2

                    if protocol.minor_version == 0:
                        res = ResponseCodeSAP.NEGOTIATION_OK
                    else:
                        res = ResponseCodeSAP.MINOR_DEVIATION

                    sap_res = SupportedAppProtocolRes(
                        response_code=res, schema_id=protocol.schema_id
                    )
                    break

                if (
                    protocol.protocol_ns == Protocol.DIN_SPEC_70121.ns.value
                    and protocol.major_version == 2
                ):
                    selected_protocol = Protocol.get_by_ns(protocol.protocol_ns)

                    # This is the earliest point where we realize
                    # that we are dealing with DINSPEC.
                    self.comm_session.selected_charging_type_is_ac = False
                    next_state = SessionSetupDINSPEC

                    if protocol.minor_version == 0:
                        res = ResponseCodeSAP.NEGOTIATION_OK
                    else:
                        res = ResponseCodeSAP.MINOR_DEVIATION

                    sap_res = SupportedAppProtocolRes(
                        response_code=res, schema_id=protocol.schema_id
                    )
                    break

                if (
                    protocol.protocol_ns.startswith(Namespace.ISO_V20_BASE)
                    and protocol.major_version == 1
                ):
                    selected_protocol = Protocol.get_by_ns(protocol.protocol_ns)
                    next_state = SessionSetupV20

                    if protocol.minor_version == 0:
                        res = ResponseCodeSAP.NEGOTIATION_OK
                    else:
                        res = ResponseCodeSAP.MINOR_DEVIATION

                    sap_res = SupportedAppProtocolRes(
                        response_code=res, schema_id=protocol.schema_id
                    )
                    break

        if not sap_res:
            # Build a detailed reason including EV vs EVSE capabilities
            try:
                ev_list = [
                    f"{o['ns']} v{o['major']}.{o['minor']} (prio={o['priority']})"
                    for o in ev_offers
                ]
            except Exception:
                ev_list = []
            try:
                evse_list = [p.name for p in (self.comm_session.config.supported_protocols or [])]
            except Exception:
                evse_list = []
            reason = (
                "No common protocol – EV supports "
                + ", ".join(ev_list) + "; EVSE supports " + ", ".join(evse_list)
            )
            logger.error("SupportedAppProtocol negotiation failed: %s", reason)
            self.stop_state_machine(reason, message, ResponseCodeSAP.NEGOTIATION_FAILED)
            return

        self.create_next_message(
            next_state,
            sap_res,
            # TODO Timeouts.V2G_SECC_SEQUENCE_TIMEOUT
            #      needs to be reduced by the
            #      elapsed time so far
            Timeouts.V2G_SECC_SEQUENCE_TIMEOUT,
            Namespace.SAP,
        )
        self.comm_session.protocol = selected_protocol
        self.comm_session.evse_controller.set_selected_protocol(selected_protocol)
        logger.info(f"Chosen protocol: {self.comm_session.protocol}")
