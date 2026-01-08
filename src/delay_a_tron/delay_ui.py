#!/usr/bin/env python3
"""
Agiltron Delay Stage Controller GUI
Main application file that loads the UI and connects all functionality
"""

import sys
import csv
from typing import Optional, List, Dict
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QFileDialog,
    QMessageBox,
    QTableWidgetItem,
    QMenu,
)
from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6 import uic

# Import the Agiltron driver
from .agiltron_delay import AgiltronDelay, MaxDelay, HomeStatus, discover

# Handle UI file loading from package resources
if sys.version_info >= (3, 9):
    from importlib.resources import files

    def get_ui_path(ui_filename):
        """Get path to UI file using importlib.resources"""
        return str(files('agiltron_gui.ui').joinpath(ui_filename))

else:
    import pkg_resources

    def get_ui_path(ui_filename):
        """Get path to UI file using pkg_resources"""
        return pkg_resources.resource_filename('agiltron_gui.ui', ui_filename)


class AgiltronController(QMainWindow):
    """Main window for Agiltron Delay Stage Controller"""

    # Signals
    position_updated = pyqtSignal(int)  # Emits current position in steps

    def __init__(self):
        super().__init__()

        # Load UI file
        # ui_path = Path(__file__).parent / 'agiltron_gui.ui'
        ui_path = '/home/peterbarrow/Projects/delay-a-tron/src/delay_a_tron/ui/agiltron_gui.ui'
        uic.loadUi(ui_path, self)

        # Set splitter default position
        # self.splitter.setSizes([250, 650])

        # Initialize variables
        self.device: Optional[AgiltronDelay] = None
        self.position_timer = QTimer()
        self.position_timer.timeout.connect(self.update_position)
        self.current_position = 0
        self.target_position = 0
        self.csv_positions: List[Dict] = []
        self.current_csv_index = 0
        self.clipboard_rows: List[Dict] = []  # For copy/paste operations
        self.playback_timer = QTimer()
        self.playback_timer.timeout.connect(self.playback_next_position)
        self.is_playing = False

        # Unit conversion factors (will be updated based on device)
        self.unit_converter = None

        # Setup UI
        self.setup_connections()
        self.setup_initial_state()
        self.scan_ports()

        # Log startup
        self.log('Application started')

    def setup_connections(self):
        """Connect all UI signals to their handlers"""

        # Connection controls
        self.btnScan.clicked.connect(self.scan_ports)
        self.btnConnect.clicked.connect(self.toggle_connection)

        # Update max delay options
        for max_delay in MaxDelay:
            value = max_delay.value
            self.comboMaxDelay.addItem(f'{value}ps', userData=value)

        self.comboMaxDelay.currentIndexChanged.connect(self.update_max_delay)

        # Start and end controls
        self.btnGoToStart.clicked.connect(self.go_to_start)
        self.btnGoToEnd.clicked.connect(self.go_to_end)

        # Jog controls
        self.btnJogFineBack.clicked.connect(
            lambda: self.jog(-self.spinFineStep.value())
        )
        self.btnJogFineForward.clicked.connect(
            lambda: self.jog(self.spinFineStep.value())
        )
        self.btnJogCoarseBack.clicked.connect(
            lambda: self.jog(-self.spinCoarseStep.value())
        )
        self.btnJogCoarseForward.clicked.connect(
            lambda: self.jog(self.spinCoarseStep.value())
        )

        # Position control
        self.comboGlobalUnits.currentIndexChanged.connect(
            self.on_units_changed,
        )
        self.btnGoToPosition.clicked.connect(self.go_to_target_position)
        self.spinTargetPosition.editingFinished.connect(
            self.update_slider_from_input
        )

        # Homing
        self.btnCalibrateHome.clicked.connect(self.calibrate_home)

        # CSV/Position list controls
        self.btnLoadCSV.clicked.connect(self.load_csv)
        self.btnSavePreset.clicked.connect(self.save_current_position)
        self.btnAddRow.clicked.connect(self.add_table_row)
        self.btnDeleteRow.clicked.connect(self.delete_table_row)
        self.btnSaveCSV.clicked.connect(self.save_csv)
        self.btnPrevious.clicked.connect(self.goto_previous_position)
        self.btnNext.clicked.connect(self.goto_next_position)
        self.btnGoToSelected.clicked.connect(self.goto_selected_position)
        # Note: Double-click now edits cells, not navigates
        self.tablePositions.cellChanged.connect(self.on_table_cell_changed)

        # Set up context menu for table rows
        # self.tablePositions.verticalHeader().setContextMenuPolicy(
        #     Qt.ContextMenuPolicy.CustomContextMenu
        # )
        self.tablePositions.verticalHeader().customContextMenuRequested.connect(
            self.show_row_context_menu
        )

        # Playback controls
        self.btnPlay.clicked.connect(self.start_playback)
        self.btnPause.clicked.connect(self.pause_playback)

        # Slider
        self.sliderTarget.valueChanged.connect(self.on_slider_changed)

        # Log controls
        self.btnToggleLog.toggled.connect(self.toggle_log)
        self.btnClearLog.clicked.connect(self.clear_log)
        self.btnSaveLog.clicked.connect(self.save_log)

        # Menu actions
        self.actionExit.triggered.connect(self.close)
        self.actionLoadCSV.triggered.connect(self.load_csv)
        self.actionSaveLog.triggered.connect(self.save_log)
        self.actionConnect.triggered.connect(self.toggle_connection)
        self.actionDisconnect.triggered.connect(self.disconnect_device)
        self.actionCalibrateHome.triggered.connect(self.calibrate_home)

    def setup_initial_state(self):
        """Set up initial UI state"""
        self.set_controls_enabled(False)
        self.labelConnectionStatusValue.setText('Disconnected')
        self.labelConnectionStatusValue.setStyleSheet(
            'color: red; font-weight: bold;'
        )
        self.labelHomingStatusValue.setText('Unknown')
        self.labelMotionStatusValue.setText('Idle')

        # Hide log by default
        self.logContainer.setVisible(False)

        # Set up position display table
        self.update_position_display(0, 0)


    def set_controls_enabled(self, enabled: bool):
        """Enable/disable controls based on connection state"""
        self.btnCalibrateHome.setEnabled(enabled)
        self.btnJogFineBack.setEnabled(enabled)
        self.btnJogFineForward.setEnabled(enabled)
        self.btnJogCoarseBack.setEnabled(enabled)
        self.btnJogCoarseForward.setEnabled(enabled)
        self.btnGoToPosition.setEnabled(enabled)
        self.sliderTarget.setEnabled(enabled)

    # Connection Management

    def scan_ports(self):
        """Scan for available serial ports"""
        self.comboBoxDevices.clear()
        # ports = serial.tools.list_ports.comports()
        device_list = discover()

        for description, port in device_list:
            self.comboBoxDevices.addItem(
                f'{port.device} - {port.description}', port.device
            )

        self.log(f'Found {len(device_list)} serial port(s)')

    def toggle_connection(self):
        """Connect or disconnect from device"""
        if self.device is None or not self.device.is_connected:
            self.connect_device()
        else:
            self.disconnect_device()

    def connect_device(self):
        """Connect to the selected device"""
        if self.comboBoxDevices.count() == 0:
            QMessageBox.warning(
                self,
                'No Device',
                'No serial ports found. Click Scan to refresh.',
            )
            return

        port = self.comboBoxDevices.currentData()
        if not port:
            return

        try:
            self.device = AgiltronDelay(
                port=port,
                baudrate=9600,
                timeout=1.0,
                wait=self.checkWaitForCompletion.isChecked(),
                max_delay=MaxDelay(self.comboMaxDelay.currentData()),
            )

            self.unit_converter = self.device._units

            # Update UI
            self.labelConnectionStatusValue.setText('Connected')
            self.labelConnectionStatusValue.setStyleSheet(
                'color: green; font-weight: bold;'
            )
            self.btnConnect.setText('Disconnect')
            self.set_controls_enabled(True)

            # Start position polling
            self.position_timer.start(100)  # Poll every 100ms

            # Check homing status
            self.update_homing_status()
            self.labelSliderMax.setText(f'{self.device._units.step_max}')

            self.jog(0)

            self.log(f'Connected to {port}')

        except Exception as e:
            QMessageBox.critical(
                self, 'Connection Error', f'Failed to connect: {str(e)}'
            )
            self.log(f'Connection failed: {str(e)}')

    def disconnect_device(self):
        """Disconnect from the device"""
        if self.device:
            self.position_timer.stop()
            self.device.close()
            self.device = None

        # Update UI
        self.labelConnectionStatusValue.setText('Disconnected')
        self.labelConnectionStatusValue.setStyleSheet(
            'color: red; font-weight: bold;'
        )
        self.btnConnect.setText('Connect')
        self.set_controls_enabled(False)
        self.labelHomingStatusValue.setText('Unknown')
        self.labelMotionStatusValue.setText('Idle')

        self.log('Disconnected from device')

    def update_max_delay(self):
        max_delay = self.device.max_delay

        try:
            max_delay = MaxDelay(self.comboMaxDelay.currentData())
        except Exception as e:
            self.log(f'Error setting max delay: {str(e)}')

        self.device.max_delay = max_delay
        self.unit_converter = self.device._units
        self.labelSliderMax.setText(f'{self.device._units.step_max}')
        self.on_units_changed(self.comboGlobalUnits.currentIndex())
        self.log(f'Set max delay to {max_delay.value}ps')

    # Position Management

    def update_position(self):
        """Poll current position from device"""
        if not self.device or not self.device.is_connected:
            return

        try:
            new_position = self.device.steps

            # Check if moving
            if abs(new_position - self.current_position) > 1:
                self.labelMotionStatusValue.setText('Moving')
                self.labelMotionStatusValue.setStyleSheet(
                    'color: orange; font-weight: bold;'
                )
            else:
                self.labelMotionStatusValue.setText('Idle')
                self.labelMotionStatusValue.setStyleSheet(
                    'color: green; font-weight: bold;'
                )

            self.current_position = new_position
            self.update_position_display(
                self.current_position, self.target_position
            )

            # Update slider visualization
            self.sliderTarget.update()  # Trigger repaint for custom indicator

        except Exception as e:
            self.log(f'Error reading position: {str(e)}')

    def update_position_display(self, current_steps: int, target_steps: int):
        """Update all position displays"""
        if not self.unit_converter:
            # If not connected, just show dashes
            # self.lineCurrentPosition.setText('--')
            self.labelCurrentSteps.setText('--')
            self.labelCurrentMM.setText('--')
            self.labelCurrentPS.setText('--')
            self.labelTargetSteps.setText('--')
            self.labelTargetMM.setText('--')
            self.labelTargetPS.setText('--')
            return

        # Convert to all units
        current_mm = self.unit_converter.mm_from_steps(current_steps)
        current_ps = self.unit_converter.ps_from_steps(current_steps)
        target_mm = self.unit_converter.mm_from_steps(target_steps)
        target_ps = self.unit_converter.ps_from_steps(target_steps)

        # # Update current position field (based on selected units)
        # unit_idx = self.comboGlobalUnits.currentIndex()
        # if unit_idx == 0:  # Steps
        #     self.lineCurrentPosition.setText(f'{current_steps}')
        # elif unit_idx == 1:  # mm
        #     self.lineCurrentPosition.setText(f'{current_mm:.3f}')
        # else:  # ps
        #     self.lineCurrentPosition.setText(f'{current_ps:.2f}')

        # Update all-units display table
        self.labelCurrentSteps.setText(f'{current_steps}')
        self.labelCurrentMM.setText(f'{current_mm:.3f}')
        self.labelCurrentPS.setText(f'{current_ps:.2f}')
        self.labelTargetSteps.setText(f'{target_steps}')
        self.labelTargetMM.setText(f'{target_mm:.3f}')
        self.labelTargetPS.setText(f'{target_ps:.2f}')

        # Update slider min/max labels
        self.labelSliderMin.setText('0')
        self.labelSliderMax.setText(f'{self.unit_converter.step_max}')

    def on_units_changed(self, index: int):
        """Handle global units change"""
        # Update spinbox properties based on selected unit
        if index == 0:  # Stepss
            self.spinTargetPosition.setDecimals(0)
            self.spinTargetPosition.setMaximum(100000)
        elif index == 1:  # mm
            self.spinTargetPosition.setDecimals(3)
            self.spinTargetPosition.setMaximum(100.0)
        else:  # ps
            self.spinTargetPosition.setDecimals(2)
            self.spinTargetPosition.setMaximum(1000.0)

        # Update display
        self.update_position_display(
            self.current_position, self.target_position
        )
        self.log(f'Units changed to: {self.comboGlobalUnits.currentText()}')

    def go_to_target_position(self):
        """Move to the target position"""
        if not self.device or not self.device.is_connected:
            return

        try:
            value = self.spinTargetPosition.value()
            unit_idx = self.comboGlobalUnits.currentIndex()

            # Convert to steps based on selected unit
            if unit_idx == 0:  # Steps
                target_steps = int(value)
            elif unit_idx == 1:  # mm
                target_steps = self.unit_converter.steps_from_mm(value)
            else:  # ps
                target_steps = self.unit_converter.steps_from_ps(value)

            # Clamp to valid range
            target_steps = max(
                self.unit_converter.step_min,
                min(target_steps, self.unit_converter.step_max),
            )

            self.target_position = target_steps
            self.device.steps = target_steps

            # Update slider
            self.sliderTarget.blockSignals(True)
            self.sliderTarget.setValue(target_steps)
            self.sliderTarget.blockSignals(False)

            self.log(f'Moving to position: {target_steps} steps')

        except Exception as e:
            self.log(f'Error moving to position: {str(e)}')
            QMessageBox.critical(self, 'Error', f'Failed to move: {str(e)}')

    def update_slider_from_input(self):
        """Update slider when input value changes"""
        # This will be called when user finishes editing the spinbox
        # We don't move yet, just update the slider
        value = self.spinTargetPosition.value()
        unit_idx = self.comboGlobalUnits.currentIndex()

        if not self.unit_converter:
            return

        if unit_idx == 0:  # Steps
            target_steps = int(value)
        elif unit_idx == 1:  # mm
            target_steps = self.unit_converter.steps_from_mm(value)
        else:  # ps
            target_steps = self.unit_converter.steps_from_ps(value)

        self.sliderTarget.blockSignals(True)
        self.sliderTarget.setValue(target_steps)
        self.sliderTarget.blockSignals(False)

    def on_slider_changed(self, value: int):
        """Handle slider value change"""
        self.target_position = value

        # Update spinbox based on selected units
        if not self.unit_converter:
            return

        unit_idx = self.comboGlobalUnits.currentIndex()

        self.spinTargetPosition.blockSignals(True)
        if unit_idx == 0:  # Steps
            self.spinTargetPosition.setValue(value)
        elif unit_idx == 1:  # mm
            self.spinTargetPosition.setValue(
                self.unit_converter.mm_from_steps(value)
            )
        else:  # ps
            self.spinTargetPosition.setValue(
                self.unit_converter.ps_from_steps(value)
            )
        self.spinTargetPosition.blockSignals(False)

        # Update display
        self.update_position_display(
            self.current_position, self.target_position
        )

    def go_to_start(self):
        if not self.device or not self.device.is_connected:
            return

        self.calibrate_home()
        self.jog(0)

    def go_to_end(self):
        if not self.device or not self.device.is_connected:
            return

        max_steps = self.unit_converter.step_max

        current_steps = self.device.steps

        self.jog(max_steps - current_steps)

    # Jog Controls

    def jog(self, steps: int):
        """Jog the stage by the specified number of steps"""
        if not self.device or not self.device.is_connected:
            return

        try:
            current = self.device.steps
            new_position = current + steps

            # Clamp to valid range
            new_position = max(
                self.unit_converter.step_min,
                min(new_position, self.unit_converter.step_max),
            )

            self.device.steps = new_position
            self.target_position = new_position

            # Update slider
            self.sliderTarget.blockSignals(True)
            self.sliderTarget.setValue(new_position)
            self.sliderTarget.blockSignals(False)

            self.log(
                f'Jogged {"+" if steps > 0 else ""}{steps} steps to {new_position}'
            )

        except Exception as e:
            self.log(f'Error jogging: {str(e)}')

    # Homing

    def calibrate_home(self):
        """Calibrate home position"""
        if not self.device or not self.device.is_connected:
            return

        try:
            self.log('Starting home calibration...')
            self.labelHomingStatusValue.setText('Homing')
            self.labelHomingStatusValue.setStyleSheet(
                'color: orange; font-weight: bold;'
            )

            self.device.calibrate_home()

            self.update_homing_status()
            self.log('Home calibration complete')

        except Exception as e:
            self.log(f'Error during homing: {str(e)}')
            QMessageBox.critical(self, 'Error', f'Homing failed: {str(e)}')

    def update_homing_status(self):
        """Update homing status display"""
        if not self.device or not self.device.is_connected:
            return

        try:
            status = self.device.homing_status()
            if status == HomeStatus.HOMED:
                self.labelHomingStatusValue.setText('Homed')
                self.labelHomingStatusValue.setStyleSheet(
                    'color: green; font-weight: bold;'
                )
            else:
                self.labelHomingStatusValue.setText('Not Homed')
                self.labelHomingStatusValue.setStyleSheet(
                    'color: orange; font-weight: bold;'
                )
        except Exception as e:
            self.log(f'Error checking homing status: {str(e)}')

    # CSV/Position List

    def load_csv(self):
        """Load positions from CSV file"""
        filename, _ = QFileDialog.getOpenFileName(
            self, 'Load Position List', '', 'CSV Files (*.csv);;All Files (*)'
        )

        if not filename:
            return

        try:
            self.csv_positions.clear()

            # Block signals during bulk loading
            self.tablePositions.blockSignals(True)
            self.tablePositions.setRowCount(0)

            with open(filename, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    position = {
                        'name': row.get('Name', ''),
                        'position': float(row.get('Position', 0)),
                        'units': row.get('Units', 'steps'),
                        'hold_time': float(row.get('Hold Time (s)', 1.0)),
                    }
                    self.csv_positions.append(position)
                    self.add_position_to_table(position)

            # Unblock signals after loading complete
            self.tablePositions.blockSignals(False)

            self.lineCSVFile.setText(filename)
            self.current_csv_index = 0
            self.update_csv_navigation()
            self.log(f'Loaded {len(self.csv_positions)} positions from CSV')

        except Exception as e:
            self.tablePositions.blockSignals(
                False
            )  # Make sure to unblock on error
            self.log(f'Error loading CSV: {str(e)}')
            QMessageBox.critical(self, 'Error', f'Failed to load CSV: {str(e)}')

    def add_position_to_table(self, position: Dict):
        """Add a position to the table"""
        row = self.tablePositions.rowCount()
        self.tablePositions.insertRow(row)

        self.tablePositions.setItem(row, 0, QTableWidgetItem(position['name']))
        self.tablePositions.setItem(
            row, 1, QTableWidgetItem(str(position['position']))
        )
        self.tablePositions.setItem(row, 2, QTableWidgetItem(position['units']))
        self.tablePositions.setItem(
            row, 3, QTableWidgetItem(str(position['hold_time']))
        )

    def save_current_position(self):
        """Save current position to the table"""
        if not self.device or not self.device.is_connected:
            QMessageBox.warning(
                self, 'Not Connected', 'Connect to device first'
            )
            return

        # Get current position in selected units
        unit_names = ['steps', 'mm', 'ps']
        unit_idx = self.comboGlobalUnits.currentIndex()
        # value = float(self.lineCurrentPosition.text())
        value = float(self.current_position)

        position = {
            'name': f'Position {self.tablePositions.rowCount() + 1}',
            'position': value,
            'units': unit_names[unit_idx],
            'hold_time': self.spinHoldTime.value(),
        }

        self.csv_positions.append(position)
        self.add_position_to_table(position)
        self.update_csv_navigation()
        self.log(f'Saved current position: {value} {unit_names[unit_idx]}')

    def add_table_row(self):
        """Add a new empty row to the table"""
        row = self.tablePositions.rowCount()

        # Block signals while adding row
        self.tablePositions.blockSignals(True)
        self.tablePositions.insertRow(row)

        # Create editable items with default values
        self.tablePositions.setItem(
            row, 0, QTableWidgetItem(f'Position {row + 1}')
        )
        self.tablePositions.setItem(row, 1, QTableWidgetItem('0.0'))
        self.tablePositions.setItem(row, 2, QTableWidgetItem('steps'))
        self.tablePositions.setItem(row, 3, QTableWidgetItem('1.0'))

        # Unblock signals
        self.tablePositions.blockSignals(False)

        # Add to csv_positions list
        position = {
            'name': f'Position {row + 1}',
            'position': 0.0,
            'units': 'steps',
            'hold_time': 1.0,
        }
        self.csv_positions.append(position)
        self.update_csv_navigation()
        self.log('Added new row to table')

    def delete_table_row(self):
        """Delete the selected row from the table"""
        current_row = self.tablePositions.currentRow()

        if current_row < 0:
            QMessageBox.warning(
                self, 'No Selection', 'Please select a row to delete'
            )
            return

        # Confirm deletion
        reply = QMessageBox.question(
            self,
            'Confirm Delete',
            f'Delete row {current_row + 1}?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )

        if reply == QMessageBox.StandardButton.Yes:
            self.tablePositions.removeRow(current_row)

            # Remove from csv_positions list
            if current_row < len(self.csv_positions):
                del self.csv_positions[current_row]

            # Adjust current index if needed
            if (
                self.current_csv_index >= len(self.csv_positions)
                and len(self.csv_positions) > 0
            ):
                self.current_csv_index = len(self.csv_positions) - 1
            elif len(self.csv_positions) == 0:
                self.current_csv_index = 0

            self.update_csv_navigation()
            self.log(f'Deleted row {current_row + 1}')

    def save_csv(self):
        """Save the table to a CSV file"""
        filename, _ = QFileDialog.getSaveFileName(
            self, 'Save Position List', '', 'CSV Files (*.csv);;All Files (*)'
        )

        if not filename:
            return

        try:
            with open(filename, 'w', newline='') as f:
                writer = csv.writer(f)
                # Write header
                writer.writerow(['Name', 'Position', 'Units', 'Hold Time (s)'])

                # Write data from table
                for row in range(self.tablePositions.rowCount()):
                    row_data = []
                    for col in range(self.tablePositions.columnCount()):
                        item = self.tablePositions.item(row, col)
                        row_data.append(item.text() if item else '')
                    writer.writerow(row_data)

            self.log(f'Saved table to {filename}')
            QMessageBox.information(
                self, 'Success', f'Table saved to {filename}'
            )

        except Exception as e:
            self.log(f'Error saving CSV: {str(e)}')
            QMessageBox.critical(self, 'Error', f'Failed to save CSV: {str(e)}')

    def on_table_cell_changed(self, row: int, column: int):
        """Handle changes to table cells"""
        # Block signals temporarily to prevent recursive updates
        if not hasattr(self, '_updating_table'):
            self._updating_table = False

        if self._updating_table:
            return

        item = self.tablePositions.item(row, column)
        if not item:
            return

        value = item.text()

        # Update the corresponding csv_positions entry
        if row < len(self.csv_positions):
            try:
                if column == 0:  # Name
                    self.csv_positions[row]['name'] = value
                elif column == 1:  # Position
                    self.csv_positions[row]['position'] = float(value)
                elif column == 2:  # Units
                    if value.lower() in ['steps', 'mm', 'ps']:
                        self.csv_positions[row]['units'] = value.lower()
                    else:
                        # Invalid unit, revert
                        self._updating_table = True
                        item.setText(self.csv_positions[row]['units'])
                        self._updating_table = False
                        QMessageBox.warning(
                            self,
                            'Invalid Unit',
                            "Unit must be 'steps', 'mm', or 'ps'",
                        )
                elif column == 3:  # Hold Time
                    self.csv_positions[row]['hold_time'] = float(value)
            except ValueError:
                # Invalid number, revert to previous value
                self._updating_table = True
                if column == 1:
                    item.setText(str(self.csv_positions[row]['position']))
                elif column == 3:
                    item.setText(str(self.csv_positions[row]['hold_time']))
                self._updating_table = False
                QMessageBox.warning(
                    self, 'Invalid Value', 'Please enter a valid number'
                )

    def show_row_context_menu(self, position):
        """Show context menu when right-clicking on row header"""
        # Get the row that was clicked
        row = self.tablePositions.verticalHeader().logicalIndexAt(position)

        if row < 0:
            return

        # Select the row
        self.tablePositions.selectRow(row)

        # Create context menu
        menu = QMenu(self)

        # Add actions
        add_action = QAction('Add Row', self)
        add_action.triggered.connect(self.add_table_row)
        menu.addAction(add_action)

        menu.addSeparator()

        copy_action = QAction('Copy Row', self)
        copy_action.triggered.connect(self.copy_row)
        menu.addAction(copy_action)

        cut_action = QAction('Cut Row', self)
        cut_action.triggered.connect(self.cut_row)
        menu.addAction(cut_action)

        paste_action = QAction('Paste Row', self)
        paste_action.triggered.connect(self.paste_row)
        paste_action.setEnabled(len(self.clipboard_rows) > 0)
        menu.addAction(paste_action)

        menu.addSeparator()

        delete_action = QAction('Delete Row', self)
        delete_action.triggered.connect(self.delete_table_row)
        menu.addAction(delete_action)

        # Show menu at cursor position
        menu.exec(self.tablePositions.verticalHeader().mapToGlobal(position))

    def copy_row(self):
        """Copy selected row(s) to clipboard"""
        selected_rows = set(
            item.row() for item in self.tablePositions.selectedItems()
        )

        if not selected_rows:
            return

        self.clipboard_rows.clear()

        for row in sorted(selected_rows):
            if row < len(self.csv_positions):
                # Deep copy the position data
                row_data = self.csv_positions[row].copy()
                self.clipboard_rows.append(row_data)

        self.log(f'Copied {len(self.clipboard_rows)} row(s)')

    def cut_row(self):
        """Cut selected row(s) to clipboard"""
        selected_rows = sorted(
            set(item.row() for item in self.tablePositions.selectedItems()),
            reverse=True,
        )

        if not selected_rows:
            return

        self.clipboard_rows.clear()

        # Copy data first
        for row in reversed(selected_rows):
            if row < len(self.csv_positions):
                row_data = self.csv_positions[row].copy()
                self.clipboard_rows.insert(0, row_data)

        # Then delete rows
        self.tablePositions.blockSignals(True)
        for row in selected_rows:
            self.tablePositions.removeRow(row)
            if row < len(self.csv_positions):
                del self.csv_positions[row]
        self.tablePositions.blockSignals(False)

        # Adjust current index if needed
        if (
            self.current_csv_index >= len(self.csv_positions)
            and len(self.csv_positions) > 0
        ):
            self.current_csv_index = len(self.csv_positions) - 1
        elif len(self.csv_positions) == 0:
            self.current_csv_index = 0

        self.update_csv_navigation()
        self.log(f'Cut {len(self.clipboard_rows)} row(s)')

    def paste_row(self):
        """Paste row(s) from clipboard"""
        if not self.clipboard_rows:
            return

        current_row = self.tablePositions.currentRow()
        if current_row < 0:
            current_row = self.tablePositions.rowCount()

        # Block signals during paste
        self.tablePositions.blockSignals(True)

        # Insert rows
        insert_position = current_row + 1
        for i, row_data in enumerate(self.clipboard_rows):
            row = insert_position + i
            self.tablePositions.insertRow(row)

            # Add items
            self.tablePositions.setItem(
                row, 0, QTableWidgetItem(row_data['name'])
            )
            self.tablePositions.setItem(
                row, 1, QTableWidgetItem(str(row_data['position']))
            )
            self.tablePositions.setItem(
                row, 2, QTableWidgetItem(row_data['units'])
            )
            self.tablePositions.setItem(
                row, 3, QTableWidgetItem(str(row_data['hold_time']))
            )

            # Add to csv_positions
            self.csv_positions.insert(row, row_data.copy())

        self.tablePositions.blockSignals(False)
        self.update_csv_navigation()
        self.log(f'Pasted {len(self.clipboard_rows)} row(s)')

    def goto_selected_position(self):
        """Go to the currently selected position in the table"""
        current_row = self.tablePositions.currentRow()

        if current_row < 0:
            QMessageBox.warning(
                self, 'No Selection', 'Please select a row first'
            )
            return

        if current_row >= len(self.csv_positions):
            return

        self.current_csv_index = current_row
        self.goto_csv_position(current_row)

    def goto_csv_position(self, index: int):
        """Go to a specific CSV position"""
        if not self.device or not self.device.is_connected:
            QMessageBox.warning(
                self, 'Not Connected', 'Connect to device first'
            )
            return

        if index < 0 or index >= len(self.csv_positions):
            return

        position = self.csv_positions[index]

        # Convert to steps
        if position['units'] == 'steps':
            target_steps = int(position['position'])
        elif position['units'] == 'mm':
            target_steps = self.unit_converter.steps_from_mm(
                position['position']
            )
        elif position['units'] == 'ps':
            target_steps = self.unit_converter.steps_from_ps(
                position['position']
            )
        else:
            self.log(f'Unknown unit: {position["units"]}')
            return

        # Move to position
        try:
            self.device.steps = target_steps
            self.target_position = target_steps

            # Update UI
            self.sliderTarget.blockSignals(True)
            self.sliderTarget.setValue(target_steps)
            self.sliderTarget.blockSignals(False)

            # Highlight row in table
            self.tablePositions.selectRow(index)

            self.log(f'Moving to CSV position {index + 1}: {position["name"]}')

        except Exception as e:
            self.log(f'Error moving to CSV position: {str(e)}')

    def goto_previous_position(self):
        """Go to previous position in list"""
        if len(self.csv_positions) == 0:
            return

        if self.current_csv_index > 0:
            self.current_csv_index -= 1
        elif self.checkLoop.isChecked():
            self.current_csv_index = len(self.csv_positions) - 1
        else:
            return

        self.goto_csv_position(self.current_csv_index)
        self.update_csv_navigation()

    def goto_next_position(self):
        """Go to next position in list"""
        if len(self.csv_positions) == 0:
            return

        if self.current_csv_index < len(self.csv_positions) - 1:
            self.current_csv_index += 1
        elif self.checkLoop.isChecked():
            self.current_csv_index = 0
        else:
            return

        self.goto_csv_position(self.current_csv_index)
        self.update_csv_navigation()

    def update_csv_navigation(self):
        """Update CSV navigation label"""
        if len(self.csv_positions) == 0:
            self.labelPosition.setText('- of -')
        else:
            self.labelPosition.setText(
                f'{self.current_csv_index + 1} of {len(self.csv_positions)}'
            )

    # Playback

    def start_playback(self):
        """Start automatic playback through positions"""
        if len(self.csv_positions) == 0:
            QMessageBox.warning(
                self, 'No Positions', 'Load a CSV file or add positions first'
            )
            return

        if not self.device or not self.device.is_connected:
            QMessageBox.warning(
                self, 'Not Connected', 'Connect to device first'
            )
            return

        self.is_playing = True
        self.btnPlay.setEnabled(False)
        self.btnPause.setEnabled(True)
        self.current_csv_index = 0

        self.log('Starting playback sequence')
        self.goto_csv_position(self.current_csv_index)

        # Start timer for next position
        hold_time = self.csv_positions[self.current_csv_index]['hold_time']
        self.playback_timer.start(int(hold_time * 1000))

    def pause_playback(self):
        """Pause automatic playback"""
        self.is_playing = False
        self.playback_timer.stop()
        self.btnPlay.setEnabled(True)
        self.btnPause.setEnabled(False)
        self.log('Playback paused')

    def playback_next_position(self):
        """Move to next position in playback sequence"""
        if not self.is_playing:
            return

        # Move to next position
        if self.current_csv_index < len(self.csv_positions) - 1:
            self.current_csv_index += 1
        elif self.checkLoop.isChecked():
            self.current_csv_index = 0
        else:
            # End of sequence
            self.pause_playback()
            self.log('Playback sequence complete')
            return

        self.goto_csv_position(self.current_csv_index)

        # Schedule next move
        hold_time = self.csv_positions[self.current_csv_index]['hold_time']
        self.playback_timer.start(int(hold_time * 1000))

    # Logging

    def toggle_log(self, checked: bool):
        """Toggle log output visibility"""
        self.logContainer.setVisible(checked)
        if checked:
            self.btnToggleLog.setText('▼ Hide Log Output')
        else:
            self.btnToggleLog.setText('▶ Show Log Output')

    def log(self, message: str):
        """Add message to log"""
        timestamp = datetime.now().strftime('%H:%M:%S')
        self.textLog.append(f'[{timestamp}] {message}')

    def clear_log(self):
        """Clear the log"""
        self.textLog.clear()
        self.log('Log cleared')

    def save_log(self):
        """Save log to file"""
        filename, _ = QFileDialog.getSaveFileName(
            self, 'Save Log', '', 'Text Files (*.txt);;All Files (*)'
        )

        if not filename:
            return

        try:
            with open(filename, 'w') as f:
                f.write(self.textLog.toPlainText())
            self.log(f'Log saved to {filename}')
        except Exception as e:
            QMessageBox.critical(
                self,
                'Error',
                f'Failed to save log: {str(e)}',
            )

    # Cleanup

    def closeEvent(self, event):
        """Handle window close event"""
        if self.device and self.device.is_connected:
            self.disconnect_device()
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = AgiltronController()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
