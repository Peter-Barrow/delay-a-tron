import struct

# from typing import Self
from dataclasses import dataclass
from enum import Enum
from time import sleep
from typing import List, Tuple

import serial


@dataclass(frozen=True)
class AgiltronConnectionConfig:
    """Configuration parameters for Agiltron serial connection.

    This dataclass holds all the serial connection parameters needed
    to establish communication with an Agiltron delay stage device.

    Attributes:
        port: Serial port identifier (e.g., "/dev/ttyUSB0" or "COM1").
        baudrate: Communication speed in bits per second.
        timeout: Read timeout in seconds, None for blocking reads.
        bytesize: Number of data bits per character.
        parity: Parity checking mode.
        stop_bits: Number of stop bits.
    """

    port: str | int
    baudrate: int = 9600
    timeout: float | None = 1.0
    bytesize: int = serial.EIGHTBITS
    parity = serial.PARITY_NONE
    stop_bits: int = serial.STOPBITS_ONE


class MaxDelay(Enum):
    """Supported maximum delay values for Agiltron devices.

    Different Agiltron delay stage models support different maximum
    delay ranges. This enum defines the supported configurations.
    """

    ps_330 = 330
    ps_660 = 660
    ps_1200 = 1200


@dataclass(frozen=True)
class AgiltronUnit:
    """Unit conversion utilities for Agiltron delay stage.

    Handles conversions between device steps and physical units
    (picoseconds for delay, millimeters for position).

    Attributes:
        step_min: Minimum step value for the device.
        step_max: Maximum step value for the device.
        steps_per_ps: Conversion factor from picoseconds to steps.
        steps_per_mm: Conversion factor from millimeters to steps.
    """

    max_delay: MaxDelay
    steps_per_ps: float
    steps_per_mm: float
    step_max: int
    step_min: int = 1

    @classmethod
    def from_max_delay(cls, max_delay: MaxDelay) -> 'AgiltronUnit':
        """Create AgiltronUnit instance based on maximum delay specification.

        Args:
            max_delay: Maximum delay specification for the device.

        Returns:
            Configured AgiltronUnit instance.

        Raises:
            ValueError: If the specified max_delay is not supported.
        """
        if max_delay == MaxDelay.ps_330:
            step_max = 80_000
            return cls(
                max_delay=max_delay,
                steps_per_ps=step_max / 333.33,
                steps_per_mm=step_max / 50,
                step_max=step_max,
                step_min=1,
            )
        elif max_delay == MaxDelay.ps_660:
            step_max = 160_000
            return cls(
                max_delay=max_delay,
                step_min=1,
                step_max=step_max,
                steps_per_ps=step_max / 666.66,
                steps_per_mm=step_max / 50,
            )

        raise ValueError(
            f'Device with delay, {max_delay.value} ps, not supported'
        )

    def ps_from_steps(self, steps: int) -> float:
        """Convert device steps to picoseconds.

        Args:
            steps: Number of device steps.

        Returns:
            Equivalent delay in picoseconds.
        """
        return steps / self.steps_per_ps

    def steps_from_ps(self, picos: float) -> int:
        """Convert picoseconds to device steps.

        Args:
            picos: Delay in picoseconds.

        Returns:
            Equivalent number of device steps, rounded to nearest integer.
        """
        return int(round(picos * self.steps_per_ps))

    def mm_from_steps(self, steps: int) -> float:
        """Convert device steps to millimeters.

        Args:
            steps: Number of device steps.

        Returns:
            Equivalent position in millimeters.
        """
        return steps / self.steps_per_mm

    def steps_from_mm(self, mm: float) -> int:
        """Convert millimeters to device steps.

        Args:
            mm: Position in millimeters.

        Returns:
            Equivalent number of device steps, rounded to nearest integer.
        """
        return int(round(mm * self.steps_per_mm))


class CommandPrefix(Enum):
    """Command prefixes for Agiltron device communication.

    Each command sent to the Agiltron device must begin with a specific
    two-byte prefix that identifies the requested operation.
    """

    SET_TARGET = b'\x01\x14'
    GET_TARGET = b'\x01\x15'
    GET_CURRENT = b'\x01\x16'
    CALIBRATE_HOME = b'\x01\x20'
    STATUS_HOME = b'\x01\x21'


def _message(
    command: CommandPrefix, args: bytes = struct.pack('>i', 0)
) -> bytes:
    """Construct a complete message for the Agiltron device.

    Args:
        command: Command prefix identifying the operation.
        args: Command arguments as bytes (default: 4 bytes of zeros).

    Returns:
        Complete message ready to send to device.
    """
    return command.value + args


def _check_result(
    command_issued: CommandPrefix, command_returned: bytes
) -> None:
    """Verify that device response matches the issued command.

    Args:
        command_issued: The command that was sent to the device.
        command_returned: The command prefix returned by the device.

    Raises:
        RuntimeError: If the returned command doesn't match the issued command.
    """
    # happy path, all is good
    if command_returned == command_issued.value:
        return

    # guess we have an error
    try:
        com_ret = CommandPrefix(command_returned)
        raise RuntimeError(
            f'Issued "{command_issued.name}" but received "{com_ret.name}"'
        )
    except ValueError:
        cname = command_issued.name
        ret = command_returned.hex()
        raise RuntimeError(
            f'Issued "{cname}" but received unknown response: {ret}'
        )


def _request(
    device: serial.Serial,
    command: CommandPrefix,
    args: bytes | None = None,
) -> int:
    """Send a command to device and return the response.

    Args:
        device: Serial connection to the device.
        command: Command to send.
        args: Optional command arguments.

    Returns:
        Integer response from the device.

    Raises:
        RuntimeError: If device response doesn't match the command.
        serial.SerialException: If communication fails.
    """
    if args is None:
        args = struct.pack('>i', 0)

    message = _message(command, args)
    device.write(message)

    # Read 6 bytes: 2 for command prefix, 4 for integer response
    response = device.read(6)
    if len(response) != 6:
        raise RuntimeError(f'Expected 6 bytes, got {len(response)}')

    result_command = response[:2]
    result_info = struct.unpack('>i', response[2:])[0]

    _check_result(command, result_command)
    return result_info


def _get_current_location(device: serial.Serial) -> int:
    """Get the current position of the device in steps.

    Args:
        device: Serial connection to the device.

    Returns:
        Current position in device steps.
    """
    return _request(device, CommandPrefix.GET_CURRENT)


def _target_get(device: serial.Serial) -> int:
    """Get the target position of the device in steps.

    Args:
        device: Serial connection to the device.

    Returns:
        Target position in device steps.
    """
    return _request(device, CommandPrefix.GET_TARGET)


def _target_set(
    device: serial.Serial,
    steps: int,
    wait: bool = True,
    wait_time: float = 0.1,
) -> int:
    """Set the target position of the device.

    Args:
        device: Serial connection to the device.
        steps: Target position in device steps.
        wait: Whether to wait for movement to complete.
        wait_time: Polling interval in seconds when waiting.

    Returns:
        Confirmed target position from device response.
    """
    requested_setpoint = struct.pack('>i', steps)

    result_info = _request(
        device,
        CommandPrefix.SET_TARGET,
        requested_setpoint,
    )

    if wait:
        while _get_current_location(device) != steps:
            sleep(wait_time)

    return result_info


class HomeStatus(Enum):
    """Status of the device homing operation.

    Indicates whether the device has completed its homing calibration
    or is currently in the process of homing.
    """

    HOMED = 0
    HOMING = 1


def _home_status(device: serial.Serial) -> HomeStatus:
    """Get the current homing status of the device.

    Args:
        device: Serial connection to the device.

    Returns:
        Current homing status.
    """
    res = _request(device, CommandPrefix.STATUS_HOME)
    return HomeStatus(res)


def _home_calibrate(
    device: serial.Serial,
    wait: bool = True,
    wait_time: float = 0.1,
) -> None:
    """Initiate homing calibration of the device.

    Args:
        device: Serial connection to the device.
        wait: Whether to wait for homing to complete.
        wait_time: Polling interval in seconds when waiting.
    """
    _request(device, CommandPrefix.CALIBRATE_HOME)
    if wait:
        while _home_status(device) != HomeStatus.HOMED:
            sleep(wait_time)


class AgiltronDelay:
    """High-level interface for Agiltron delay stage control.

    This class provides a convenient Python interface for controlling
    Agiltron delay stage devices, with properties for position control
    in multiple units (steps, millimeters, picoseconds).

    Attributes:
        device: Serial connection to the Agiltron device.
        wait_time: Default polling interval for blocking operations.
    """

    def __init__(
        self,
        port: str | int,
        baudrate: int = 9600,
        timeout: float | None = 1.0,
        bytesize: int = serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stop_bits: int = serial.STOPBITS_ONE,
        wait: bool = True,
        wait_time: float = 0.1,
        max_delay: MaxDelay = MaxDelay.ps_330,
    ) -> None:
        """Initialize connection to Agiltron delay stage.

        Args:
            port: Serial port identifier.
            baudrate: Communication speed in bits per second.
            timeout: Read timeout in seconds.
            bytesize: Number of data bits per character.
            parity: Parity checking mode.
            stop_bits: Number of stop bits.
            wait: Default behavior for blocking operations.
            wait_time: Default polling interval in seconds.
            max_delay: Maximum delay specification for unit conversions.

        Raises:
            serial.SerialException: If serial connection fails.
            ValueError: If max_delay is not supported.
        """
        self._config = AgiltronConnectionConfig(
            port,
            baudrate,
            timeout,
            bytesize,
            # parity,
            stop_bits,
        )

        self.device = serial.Serial(
            port,
            baudrate=baudrate,
            timeout=timeout,
            bytesize=bytesize,
            parity=parity,
            stopbits=stop_bits,
        )

        self._units = AgiltronUnit.from_max_delay(max_delay)

        self.wait_time = wait_time
        self._wait = wait

    @property
    def max_delay(self) -> MaxDelay:
        return self._units.max_delay

    @max_delay.setter
    def max_delay(self, new_max_delay: MaxDelay) -> None:
        self._units = AgiltronUnit.from_max_delay(new_max_delay)

    @property
    def wait_to_finish(self) -> bool:
        """Whether operations wait for completion by default.

        Returns:
            True if operations wait for completion, False otherwise.
        """
        return self._wait

    @wait_to_finish.setter
    def wait_to_finish(self, wait: bool) -> None:
        """Set default waiting behavior for operations.

        Args:
            wait: Whether to wait for operations to complete.
        """
        self._wait = wait

    @property
    def steps(self) -> int:
        """Current position in device steps.

        Returns:
            Current position as number of steps.
        """
        if not self.device.is_open:
            self._reopen_connection()
        return _get_current_location(self.device)

    @steps.setter
    def steps(self, num_steps: int) -> None:
        """Move to specified position in steps.

        Args:
            num_steps: Target position in device steps.
        """
        if not self.device.is_open:
            self._reopen_connection()
        _target_set(self.device, num_steps, self._wait, self.wait_time)

    @property
    def position_mm(self) -> float:
        """Current position in millimeters.

        Returns:
            Current position in millimeters.
        """
        steps = self.steps
        return self._units.mm_from_steps(steps)

    @position_mm.setter
    def position_mm(self, mm: float) -> None:
        """Move to specified position in millimeters.

        Args:
            mm: Target position in millimeters.
        """
        steps: int = self._units.steps_from_mm(mm)
        self.steps = steps

    @property
    def delay_ps(self) -> float:
        """Current delay in picoseconds.

        Returns:
            Current relative delay in picoseconds.
        """
        steps = self.steps
        return self._units.ps_from_steps(steps)

    @delay_ps.setter
    def delay_ps(self, picos: float) -> None:
        """Set delay in picoseconds.

        Args:
            picos: Target delay in picoseconds.
        """
        steps = self._units.steps_from_ps(picos)
        self.steps = steps

    def get_target_delay_ps(self) -> float:
        """Get the target delay in picoseconds.

        Returns:
            Target delay in picoseconds.
        """
        steps = _target_get(self.device)
        return self._units.ps_from_steps(steps)

    def homing_status(self) -> HomeStatus:
        """Get the current homing status.

        Returns:
            Current homing status of the device.
        """
        if not self.device.is_open:
            self._reopen_connection()
        return _home_status(self.device)

    def calibrate_home(self) -> None:
        """Calibrate the home position of the device.

        Initiates homing calibration. If wait_to_finish is True,
        blocks until homing is complete.
        """
        if not self.device.is_open:
            self._reopen_connection()
        _home_calibrate(self.device, wait=self._wait, wait_time=self.wait_time)

    def close(self) -> None:
        """Close the serial connection to the device.

        Should be called when finished using the device to free
        the serial port for other applications.
        """
        if hasattr(self, 'device') and self.device.is_open:
            self.device.close()

    def _reopen_connection(self) -> None:
        """Reopen the serial connection using stored configuration.

        Raises:
            serial.SerialException: If unable to reopen connection.
        """
        self.device = serial.Serial(
            port=self._config.port,
            baudrate=self._config.baudrate,
            timeout=self._config.timeout,
            bytesize=self._config.bytesize,
            parity=self._config.parity,
            stopbits=self._config.stop_bits,
        )

    @property
    def is_connected(self) -> bool:
        """Check if the device connection is open.

        Returns:
            True if connection is open, False otherwise.
        """
        return hasattr(self, 'device') and self.device.is_open

    def __enter__(self):
        """Enter context manager.

        Ensures the device connection is open before returning.
        If the connection was closed, it will be reopened using
        the original configuration.

        Returns:
            Self for use in with statement.

        Raises:
            serial.SerialException: If unable to open/reopen connection.
        """
        if not self.device.is_open:
            self._reopen_connection()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit context manager and ensure cleanup.

        Args:
            exc_type: Exception type if an exception occurred.
            exc_val: Exception value if an exception occurred.
            exc_tb: Exception traceback if an exception occurred.
        """
        self.close()

    def __del__(self) -> None:
        """Destructor to ensure serial connection is closed.

        Automatically called when object is garbage collected.
        Provides safety net if close() wasn't called explicitly.
        """
        try:
            self.close()
        except Exception:
            # Ignore errors during destruction
            pass


DEVICE_LIST = list[tuple[str, serial.Serial]]


def discover() -> DEVICE_LIST:
    VID = 0x0403
    PID = 0x6001

    device_list: DEVICE_LIST = []

    from serial.tools import list_ports

    for port in list_ports.comports():
        if port.location is None:
            continue

        if port.pid != PID:
            continue

        if port.vid != VID:
            continue

        device_list.append((port.description, port))

    return device_list
