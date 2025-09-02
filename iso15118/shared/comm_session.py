"""
This module contains the SessionStateMachine and the V2GCommunicationSession,
which itself is subclassing the SessionStateMachine. These classes can be used
by both EVCC and SECC, as they share the exact same functionality when sending,
receiving, and processing messages during an ISO 15118 communication session.
"""

import asyncio
import gc
import logging
from abc import ABC, abstractmethod
from asyncio.streams import StreamReader, StreamWriter
from typing import List, Optional, Tuple, Type, Union

from pydantic import ValidationError
from typing_extensions import TYPE_CHECKING

from iso15118.shared.exceptions import (
    EXIDecodingError,
    FaultyStateImplementationError,
    InvalidV2GTPMessageError,
    MessageProcessingError,
    V2GMessageValidationError,
)
from iso15118.shared.exi_codec import EXI
from iso15118.shared.messages.app_protocol import (
    SupportedAppProtocolReq,
    SupportedAppProtocolRes,
)
from iso15118.shared.messages.datatypes import SelectedService as SelectedServiceV2_DIN
from iso15118.shared.messages.din_spec.msgdef import V2GMessage as V2GMessageDINSPEC
from iso15118.shared.messages.enums import (
    ControlMode,
    DINPayloadTypes,
    ISOV2PayloadTypes,
    ISOV20PayloadTypes,
    Namespace,
    Protocol,
    SessionStopAction,
)
from iso15118.shared.messages.iso15118_2.msgdef import V2GMessage as V2GMessageV2
from iso15118.shared.messages.iso15118_20.common_messages import (
    MatchedService as OfferedServiceV20,
)
from iso15118.shared.messages.iso15118_20.common_messages import (
    SelectedEnergyService,
    SelectedVAS,
)
from iso15118.shared.messages.iso15118_20.common_types import (
    V2GMessage as V2GMessageV20,
)
import os
import time
from iso15118.shared.messages.v2gtp import V2GTPMessage
from iso15118.shared.settings import shared_settings, SettingKey
from iso15118.shared.notifications import StopNotification
from iso15118.shared.states import Pause, State, Terminate
from iso15118.shared.utils import wait_for_tasks
from iso15118.shared.messages.timeouts import Timeouts as TShared

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # EVCCCommunicationSession and SECCCommunicationSession are used for
    # annotation purposes only, as a type hint for the comm_session class
    # attribute. But comm_session also imports State. To avoid a circular import
    # error, one can use the TYPE_CHECKING boolean from typing, which evaluates
    # to True during mypy or other 3rd party type checker but assumes the value
    # 'False' during runtime.
    # Please check:
    # https://stackoverflow.com/questions/61545580/how-does-mypy-use-typing-type-checking-to-resolve-the-circular-import-annotation
    # https://docs.python.org/3/library/typing.html#typing.TYPE_CHECKING
    from iso15118.evcc.comm_session_handler import EVCCCommunicationSession
    from iso15118.secc.comm_session_handler import SECCCommunicationSession


class SessionStateMachine(ABC):
    """
    Each newly established TCP session initiates a communication session, which
    is essentially the sate machine for the ISO 15118 message handling.
    """

    def __init__(
        self,
        start_state: Type[State],
        comm_session: Union["EVCCCommunicationSession", "SECCCommunicationSession"],
    ):
        """
        The EVCC state machine starts with waiting for the
        SupportedAppProtocolRes message from the SECC.

        The SECC state machine starts with waiting for the
        SupportedAppProtocolReq message from the EVCC.

        The ISO 15118 version is determined with the SupportedAppProtocolRequest.
        The states ProcessSupportedAppProtocolReq (SECC) and
        ProcessSupportedAppProtocolRes (EVCC), respectively, need to set the
        iso_version of the corresponding CommunicationSession object.

        Args:
            start_state: The state that initialises the state machine
            comm_session:   The V2GCommunicationSession object of either the
                            the EVCC or SECC. Needed to access certain session
                            variables, as certain states need to read
                            and store session relevant information, depending on
                            the message.

                            For example: the SupportedAppProtocolReq message
                            contains information about the EVCC's supported
                            protocol, which is relevant information needed
                            throughout the session.
        """
        self.start_state = start_state
        self.comm_session = comm_session
        self.current_state = start_state(comm_session)
        self.v20_payload_type_to_namespace = {
            ISOV20PayloadTypes.AC_MAINSTREAM: Namespace.ISO_V20_AC,
            ISOV20PayloadTypes.DC_MAINSTREAM: Namespace.ISO_V20_DC,
            ISOV20PayloadTypes.ACDP_MAINSTREAM: Namespace.ISO_V20_ACDP,
            ISOV20PayloadTypes.WPT_MAINSTREAM: Namespace.ISO_V20_WPT,
        }

    def get_exi_ns(
        self,
        payload_type: Union[DINPayloadTypes, ISOV2PayloadTypes, ISOV20PayloadTypes],
    ) -> Namespace:
        """
        Provides the right protocol namespace for the EXI decoder.
        In DIN SPEC 70121 and ISO 15118-2, all messages are defined
        in one XSD schema (e.g. 'urn:iso:15118:2:2013:MsgDef' for ISO 15118-2),
        but in ISO 15118-20, we need to distinguish between Common, AC, DC, WPT,
        and ACDP.

        An ISO 15118-20 communication starts with messages defined in namespace
        'urn:iso:std:iso:15118:-20:CommonMessages', but some messages are then
        energy mode specific and, thus, we need the specific schema where these
        messages are defined.
        """
        if self.comm_session.protocol == Protocol.UNKNOWN:
            return Namespace.SAP
        elif self.comm_session.protocol == Protocol.ISO_15118_2:
            return Namespace.ISO_V2_MSG_DEF
        elif self.comm_session.protocol == Protocol.DIN_SPEC_70121:
            return Namespace.DIN_MSG_DEF
        elif self.comm_session.protocol.ns.startswith(Namespace.ISO_V20_BASE):
            if isinstance(payload_type, ISOV20PayloadTypes):
                return self.v20_payload_type_to_namespace.get(
                    payload_type, Namespace.ISO_V20_COMMON_MSG
                )
        return Namespace.ISO_V20_COMMON_MSG

    async def process_message(self, message: bytes):
        """
        The following steps are conducted in this state machine's general
        process_message() function:

        1. Try to create a V2GTP (V2G Transfer Protocol) message from the
           incoming byte stream.
        2. If step 1 didn't raise a InvalidV2GTPMessageError then we try to
           EXI decode the V2GTP payload
        3. If step 2 didn't raise an EXIDecodingError then we hand over the
           decoded payload to the current state's process_message() function,
           which will create the next message to send - but only in case a new
           message needs to be sent. For that to be true, the next_v2gtp_msg
           must have been set by the current state, which is usually the case
           for the EVCC if the next state is not a Terminate state. The SECC
           will always send a next response, even if the next state is Terminate.
           The next state to transition to is also determined by the state's
           process_message() method.

        Args:
            message:    The incoming message from the EVCC/SECC, given as a
                        bytearray (the payload of a V2GTPMessage).
                        The message can be a
                        - SupportedAppProtocolRequest  (EVCC),
                        - SupportedAppProtocolResponse (SECC),
                        - V2GMessage according to the DIN SPEC 70121 standard,
                        - V2GMessage according to the ISO 15118-2 standard, or
                        - V2GMessage according to the ISO 15118-20 standard

        Raises:
            InvalidV2GTPMessageError, FaultyStateImplementationError,
            EXIDecodingError
        """
        # Step 1
        try:
            # First extract the V2GMessage payload from the V2GTPMessage ...
            # and then decode the bytearray into the message
            v2gtp_msg = V2GTPMessage.from_bytes(self.comm_session.protocol, message)
        except InvalidV2GTPMessageError as exc:
            logger.exception("Incoming TCPPacket is not a valid V2GTPMessage")
            raise exc

        # Step 2
        decoded_message: Union[
            SupportedAppProtocolReq,
            SupportedAppProtocolRes,
            V2GMessageV2,
            V2GMessageV20,
            V2GMessageDINSPEC,
            None,
        ] = None
        try:
            decoded_message = EXI().from_exi(
                v2gtp_msg.payload, self.get_exi_ns(v2gtp_msg.payload_type)
            )

            if hasattr(self.comm_session, "evse_id"):
                logger.trace(  # type: ignore[attr-defined]
                    f"{self.comm_session.evse_id}:::"
                    f"{v2gtp_msg.payload.hex()}:::"
                    f"{self.get_exi_ns(v2gtp_msg.payload_type).value}"
                )

        except V2GMessageValidationError as exc:
            logger.error(
                f"EXI message (ns={self.get_exi_ns(v2gtp_msg.payload_type)}) "
                f"where validation failed: {v2gtp_msg.payload.hex()}"
            )
            raise exc
        except EXIDecodingError as exc:
            logger.exception(f"{exc}")
            logger.error(
                f"EXI message (ns={self.get_exi_ns(v2gtp_msg.payload_type)}) "
                f"where error occured: {v2gtp_msg.payload.hex()}"
            )
            raise exc

        # Shouldn't happen, but just to be sure (otherwise mypy would complain)
        if not decoded_message:
            logger.error(
                "Unusual error situation: decoded_message is None"
                "although no EXIDecodingError was raised"
            )
            return

        # Step 3
        try:
            logger.info(f"{str(decoded_message)} received")
            await self.current_state.process_message(decoded_message, v2gtp_msg.payload)
        except MessageProcessingError as exc:
            logger.exception(
                f"{exc.__class__.__name__} while processing " f"{exc.message_name}"
            )
            raise exc
        except FaultyStateImplementationError as exc:
            logger.exception(f"{exc.__class__.__name__}: {exc}")
            raise exc
        except ValidationError as exc:
            logger.exception(f"{exc.__class__.__name__}: {exc.raw_errors}")
            raise exc
        except AttributeError as exc:
            logger.exception(f"{exc}")
            raise exc

        if (
            self.current_state.next_v2gtp_msg is None
            and self.current_state.next_state is not Terminate
        ):
            raise FaultyStateImplementationError(
                "Field 'next_v2gtp_msg' is "
                "None but must be set because "
                "next state is not Terminate"
            )

    def go_to_next_state(self):
        """
        This method assures that the communication session's current state is
        always up-to-date, which is something other parts of the code rely on.

        Is automatically called by the rcv_loop after sending the next message.
        But we only transition if the current state defined a next state.
        If that's not the case, the current state will decide how to transition
        based on the next incoming message.
        """
        if self.current_state.next_state:
            self.current_state.next_state(self.comm_session)

    def resume(self):
        logger.debug("Trying to resume communication session")
        self.current_state = self.start_state(self.comm_session)


class V2GCommunicationSession(SessionStateMachine):
    """
    A communication session class that is shared between the EVCC and the SECC
    to execute the corresponding state machine, process incoming messages, and
    send subsequent messages as a result.
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(
        self,
        transport: Tuple[StreamReader, StreamWriter],
        start_state: Type["State"],
        session_handler_queue: asyncio.Queue,
        comm_session: Union["EVCCCommunicationSession", "SECCCommunicationSession"],
    ):
        """
        Initialise the communication session with EVCC or SECC specific
        parameters

        Args:
            transport:  A tuple consisting of a StreamReader and StreamWriter
                        object for the TCP socket
            start_state: The state that initialises the state machine
            session_handler_queue:  The asyncio.Queue object used for pushing
                                    timeout, termination, and pausing
                                    notifications to the session handler
            is_tls: Whether or not this TCP communication session is cryptographically
                    secured with TLS
            comm_session: The instance of EVCCCommunicationSession or
                          SECCCommunicationSession
        """
        self.protocol: Protocol = Protocol.UNKNOWN
        self.reader, self.writer = transport
        # For timeout, termination, and pausing notifications
        self.session_handler_queue = session_handler_queue
        self.peer_name = self.writer.get_extra_info("peername")
        self.session_id: str = ""
        # Mutually agreed-upon ISO 15118 application protocol as result of SAP
        self.chosen_protocol: str = ""
        # Whether the SECC supports service renegotiation (ISO 15118-20)
        self.service_renegotiation_supported: bool = False
        # The services which the SECC offers (ISO 15118-20)
        self.matched_services_v20: List[OfferedServiceV20] = []
        # The value-added services the EVCC selected (ISO 15118-20)
        self.selected_vas_list_v20: List[SelectedVAS] = []
        # The charge service and value-added services the EVCC selected (ISO 15118-2)
        self.selected_services: List[SelectedServiceV2_DIN] = []
        # The energy service the EVCC selected (ISO 15118-20)
        self.selected_energy_service: Optional[SelectedEnergyService] = None
        # Variable selected_charging_type_is_ac set if one of the AC modes is selected
        self.selected_charging_type_is_ac: bool = True
        # The SAScheduleTuple element the EVCC chose (referenced by ID)
        self.selected_schedule: Optional[int] = None
        # The control mode used for this session (Scheduled or Dynamic). In ISO 15118-2,
        # there is only Scheduled, in -20 we have both and need to choose certain
        # datatypes of messages based on which control mode was chosen
        self.control_mode: Optional[ControlMode] = None
        # Contains info whether the communication session is stopped successfully (True)
        # or due to a failure (False), plus additional info regarding the reason behind.
        self.stop_reason: Optional[StopNotification] = None
        self.last_message_sent: Optional[V2GTPMessage] = None
        self._started: bool = True
        # EXI per-session capture
        self._exi_seq: int = 0
        self._exi_dir: Optional[str] = None
        if shared_settings.get(SettingKey.MESSAGE_LOG_PER_SESSION):
            base = shared_settings.get(SettingKey.EXI_SESSION_DIR)
            ts = time.strftime("%Y%m%d-%H%M%S")
            peer = f"{self.peer_name[0]}_{self.peer_name[1]}" if isinstance(self.peer_name, tuple) else str(self.peer_name)
            self._exi_dir = os.path.join(str(base), f"session_{ts}_{peer}")
            try:
                os.makedirs(self._exi_dir, exist_ok=True)
            except Exception:
                self._exi_dir = None

        logger.info("Starting a new communication session")
        SessionStateMachine.__init__(self, start_state, comm_session)

        # Timeout override caps (env-driven). 0 disables.
        # V2G_TIMEOUT_MIN_S: minimum allowed per-step timeout (floats)
        # V2G_TIMEOUT_MAX_S: maximum allowed per-step timeout
        # V2G_SECC_SEQUENCE_TIMEOUT_CAP_S: cap when sequence timeout (default 60s) is used
        # V2G_EVCC_COMM_SETUP_TIMEOUT_CAP_S: cap when comm setup timeout (default 20s) is used
        import os

        def _f(name: str, default: float = 0.0) -> float:
            try:
                v = os.environ.get(name)
                return float(v) if v is not None and v != "" else default
            except Exception:
                return default

        self._to_min_s: float = _f("V2G_TIMEOUT_MIN_S", 0.0)
        self._to_max_s: float = _f("V2G_TIMEOUT_MAX_S", 0.0)
        self._cap_seq_s: float = _f("V2G_SECC_SEQUENCE_TIMEOUT_CAP_S", 0.0)
        self._cap_comm_setup_s: float = _f("V2G_EVCC_COMM_SETUP_TIMEOUT_CAP_S", 0.0)
        # One-shot grace extension when CP indicates physical link is still connected
        self._grace_s: float = _f("V2G_TIMEOUT_GRACE_S", 0.0)
        try:
            self._grace_remaining: int = int(os.environ.get("V2G_TIMEOUT_GRACE_MAX", "1"))
        except Exception:
            self._grace_remaining = 1

    def _effective_timeout(self, timeout: float) -> float:
        """Apply environment-driven caps to the given timeout value.

        Ensures per-message timeouts adhere to optional min/max and specific caps
        for well-known phases without changing call sites across the codebase.
        """
        eff = float(timeout or 0)
        # Specific caps when the default sequence or communication setup timeouts are used
        try:
            if self._cap_seq_s > 0 and eff >= float(TShared.V2G_SECC_SEQUENCE_TIMEOUT):
                eff = min(eff, self._cap_seq_s)
        except Exception:
            pass
        try:
            if self._cap_comm_setup_s > 0 and eff >= float(TShared.V2G_EVCC_COMMUNICATION_SETUP_TIMEOUT):
                eff = min(eff, self._cap_comm_setup_s)
        except Exception:
            pass
        # Global caps
        if self._to_max_s > 0:
            eff = min(eff, self._to_max_s)
        if self._to_min_s > 0:
            eff = max(eff, self._to_min_s)
        # Floor to a small positive value to avoid zero/negative waits
        try:
            if eff <= 0:
                eff = 0.01
        except Exception:
            eff = 0.01
        return eff

    async def start(self, timeout: float):
        """
        Starts a EVCC / SECC communication session by spawning up the rcv_loop()
        method, that constantly waits a given amount of seconds to read data
        from the TCP socket and process the incoming data.

        Args:
            timeout:    The time the EVCC / SECC is waiting for a next message
        """
        tasks = [self.rcv_loop(timeout)]

        try:
            self._started = True
            await wait_for_tasks(tasks)
        finally:
            self._started = False

    @abstractmethod
    def save_session_info(self):
        raise NotImplementedError

    async def _update_state_info(self, state: State):
        if hasattr(self.comm_session, "evse_controller"):
            evse_controller = self.comm_session.evse_controller
            await evse_controller.set_present_protocol_state(state)

    async def stop(self, reason: str):
        """
        Closes the TCP connection after 5 seconds and terminates or pauses the
        data link for this V2GCommunicationSession object after 2 seconds to
        make sure any message that needs to be sent can still go through.
        TODO check if that really makes sense of if TCP should be terminated
             after 2 s and the data link after 5 s

        Especially necessary for the SECC, which needs to send a response with
        a FAILED response code or a SessionStopRes with response code "OK"
        before closing the TCP connection.

        Not a formal requirement in ISO 15118-2, but a best practice decided
        within the ISO 15118 User Group, and it became a formal requirement in
        ISO 15118-20 (at least for the SECC side, see [V2G20-1633]).
        """
        if self.current_state.next_state == Pause:
            self.save_session_info()
            terminate_or_pause = SessionStopAction.PAUSE
        else:
            terminate_or_pause = SessionStopAction.TERMINATE

        logger.info(
            f"The data link will {terminate_or_pause} in 2 seconds and "
            "the TCP connection will close in 5 seconds. "
        )
        logger.info(f"Reason: {reason}")

        await asyncio.sleep(2)
        # Signal data link layer to either terminate or pause the data
        # link connection
        if hasattr(self.comm_session, "evse_controller"):
            evse_controller = self.comm_session.evse_controller
            await evse_controller.update_data_link(terminate_or_pause)
            await evse_controller.session_ended(str(self.current_state), reason)
        elif hasattr(self.comm_session, "ev_controller"):
            await self.comm_session.ev_controller.enable_charging(False)
        logger.info(f"{terminate_or_pause}d the data link")
        await asyncio.sleep(3)
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (asyncio.TimeoutError, ConnectionResetError) as exc:
            logger.info(str(exc))
        logger.info("TCP connection closed to peer with address " f"{self.peer_name}")

    async def send(self, message: V2GTPMessage):
        """
        Sends a V2GTPMessage via the TCP socket and stores the last message sent

        Args:
            message: A V2GTPMessage
        """

        # TODO: we may also check for writer exceptions
        raw = message.to_bytes()
        self.writer.write(raw)
        await self.writer.drain()
        self.last_message_sent = message
        logger.info(f"Sent {str(self.current_state.message)}")
        # Save EXI if configured
        try:
            if self._exi_dir:
                self._exi_seq += 1
                with open(os.path.join(self._exi_dir, f"{self._exi_seq:04d}_tx_{int(time.time()*1000)}.exi"), "wb") as f:
                    f.write(raw)
        except Exception:
            pass

    async def rcv_loop(self, timeout: float):
        """
        A constant loop that implements the timeout for each message. Starts
        waiting for a specified time (see argument 'timeout') to read something
        from the TCP socket via the StreamReader. Once data is received, it is
        processed and the according next message will be sent. The processing of
        the incoming data also returns the timeout value for the next message
        being sent, and the loop starts over again waiting to read data on the
        TCP socket for the given amount of time ('timeoout').

        We expect the incoming message to be an EXI encoded message.

        Args:
            timeout:    The time the EVCC / SECC is waiting for a next message
        """
        while True:
            try:
                eff_timeout = self._effective_timeout(timeout)
                # Robust V2GTP reassembly: read header (8 bytes) then payload
                header = await asyncio.wait_for(self.reader.readexactly(8), eff_timeout)
                # Validate and extract payload length
                if not V2GTPMessage.is_header_valid(self.comm_session.protocol, header):
                    raise InvalidV2GTPMessageError("Invalid V2GTP header")
                payload_len = V2GTPMessage.get_payload_length(header)
                if payload_len < 0:
                    raise InvalidV2GTPMessageError("Negative payload length")
                payload = b""
                if payload_len:
                    payload = await asyncio.wait_for(
                        self.reader.readexactly(payload_len), eff_timeout
                    )
                message = header + payload
                if message == b"" and self.reader.at_eof():
                    stop_reason: str = "TCP peer closed connection"
                    await self.stop(reason=stop_reason)
                    self.session_handler_queue.put_nowait(
                        StopNotification(
                            False,
                            stop_reason,
                            self.peer_name,
                        )
                    )
                    return
            except (asyncio.TimeoutError, ConnectionResetError) as exc:
                # Optional grace extension: if we timed out but CP is still connected,
                # allow one more short wait to accommodate slower counterparts.
                if isinstance(exc, asyncio.TimeoutError):
                    try:
                        if (
                            self._grace_s > 0
                            and self._grace_remaining > 0
                            and hasattr(self.comm_session, "evse_controller")
                        ):
                            from iso15118.shared.messages.enums import CpState  # lazy import

                            evse_controller = self.comm_session.evse_controller  # type: ignore[attr-defined]
                            cp = await evse_controller.get_cp_state()
                            # Treat non-disconnected states as connected
                            if cp not in (CpState.A1, CpState.A2, CpState.E, CpState.F, CpState.UNKNOWN):
                                self._grace_remaining -= 1
                                logger.warning(
                                    "RX timeout; applying one-shot grace of %.3fs (remaining=%d)",
                                    self._grace_s,
                                    self._grace_remaining,
                                )
                                timeout = self._grace_s
                                # Restart the loop with updated timeout
                                continue
                    except Exception:
                        # Fall through to normal timeout handling
                        pass
                if type(exc) is asyncio.TimeoutError:
                    if self.last_message_sent:
                        error_msg = (
                            f"{exc.__class__.__name__} occurred. Waited "
                            f"for {timeout} s after sending last message: "
                            f"{str(self.last_message_sent)}"
                        )
                    else:
                        error_msg = (
                            f"{exc.__class__.__name__} occurred. Waited "
                            f"for {timeout} s. No V2GTP message was "
                            "previously sent. This is probably a timeout "
                            f"while waiting for SupportedAppProtocolReq"
                        )
                else:
                    error_msg = f"{exc.__class__.__name__} occurred. {str(exc)}"

                self.stop_reason = StopNotification(False, error_msg, self.peer_name)

                await self.stop(reason=error_msg)
                self.session_handler_queue.put_nowait(self.stop_reason)
                return
            gc_enabled = gc.isenabled()
            try:
                if gc_enabled:
                    gc.disable()
                # This will create the values needed for the next state, such as
                # next_state, next_v2gtp_message, next_message_payload_type etc.
                # Save RX EXI if configured
                try:
                    if self._exi_dir:
                        self._exi_seq += 1
                        with open(os.path.join(self._exi_dir, f"{self._exi_seq:04d}_rx_{int(time.time()*1000)}.exi"), "wb") as f:
                            f.write(message)
                except Exception:
                    pass
                t0 = time.time()
                await self.process_message(message)
                if self.current_state.next_v2gtp_msg:
                    # next_v2gtp_msg would not be set only if the next state is either
                    # Terminate or Pause on the EVCC side
                    await self.send(self.current_state.next_v2gtp_msg)
                t1 = time.time()
                try:
                    if (t1 - t0) > 0.5:
                        logger.warning(
                            "Charge-loop processing latency high: %.0f ms", (t1 - t0) * 1000
                        )
                except Exception:
                    pass
                    await self._update_state_info(self.current_state)

                if self.current_state.next_state in (Terminate, Pause):
                    await self.stop(reason=self.comm_session.stop_reason.reason)
                    self.comm_session.session_handler_queue.put_nowait(
                        self.comm_session.stop_reason
                    )
                    return

                timeout = self.current_state.next_msg_timeout
                self.go_to_next_state()
            except (
                MessageProcessingError,
                FaultyStateImplementationError,
                EXIDecodingError,
                InvalidV2GTPMessageError,
                AttributeError,
                ValueError,
                ConnectionResetError,
                Exception,
            ) as exc:
                message_name = ""
                additional_info = ""
                if isinstance(exc, MessageProcessingError):
                    message_name = exc.message_name
                if isinstance(exc, FaultyStateImplementationError):
                    additional_info = f": {exc}"
                if isinstance(exc, EXIDecodingError):
                    additional_info = f": {exc}"
                if isinstance(exc, InvalidV2GTPMessageError):
                    additional_info = f": {exc}"

                stop_reason = (
                    f"{exc.__class__.__name__} occurred while processing message "
                    f"{message_name} in state {str(self.current_state)} : {exc}. "
                    f"{additional_info}"
                )

                self.stop_reason = StopNotification(
                    False,
                    stop_reason,
                    self.peer_name,
                )

                await self.stop(stop_reason)
                self.session_handler_queue.put_nowait(self.stop_reason)
                return
            finally:
                if gc_enabled:
                    gc.enable()
