"""Contrato serial dos sensores e validade do alinhamento lunar (sem hardware)."""
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, Mock

from mount_controller import MountControllerAPI, _signed_angle


class SensorContractTests(unittest.TestCase):
    def setUp(self):
        self.api = MountControllerAPI()

    def test_bno_signed_angles_and_calibration(self):
        self.api._parse_line('BNO: heading 359.5000 roll -10.2500 pitch 42.0000 CAL sys 3 gyro 2 accel 1 mag 0')
        self.assertEqual(self.api.bno['roll'], -10.25)
        self.assertEqual(self.api.bno['cal_system'], 3)
        self.assertTrue(self.api.bno['available'])

    def test_rtc_osf_and_expiration(self):
        self.api._parse_line('RTC UTC: 2026-10-21T00:00:00Z OSF=1')
        self.assertIsNone(self.api._rtc_now())
        self.api._parse_line('RTC UTC: 2026-10-21T00:00:00Z OSF=0')
        self.assertIsNotNone(self.api._rtc_now())
        self.api.rtc_updated_at = time.monotonic() - 66
        self.assertIsNone(self.api._rtc_now())

    def test_missing_rtc_invalidates_time(self):
        self.api._parse_line('RTC UTC: 2026-10-21T00:00:00Z OSF=0')
        self.api._parse_line('RTC: INDISPONIVEL')
        self.assertIsNone(self.api._rtc_now())

    def test_local_sketch_sync_uses_pc_time_and_waits_for_readback(self):
        with patch.object(self.api, '_send_line') as send:
            before = datetime.now().strftime('D %d/%m/%Y %H:%M:%S')
            self.api._parse_line('Blumenau | 01/01/2020 00:00:00 BRT')
            after = datetime.now().strftime('D %d/%m/%Y %H:%M:%S')
            self.assertIn(send.call_args.args[0], (before, after))
            self.assertIsNone(self.api._rtc_now())
            self.api._parse_line('Blumenau | 01/01/2020 00:00:01 BRT')
            self.assertIsNone(self.api._rtc_now())
            line = datetime.now().strftime('Blumenau | %d/%m/%Y %H:%M:%S BRT')
            self.api._parse_line(line)
            self.assertIsNotNone(self.api._rtc_now())
            self.assertFalse(self.api.rtc_local_sync_pending)
            self.api._parse_line(line)
            self.assertEqual(send.call_count, 1)
            self.api._parse_line('Erro I2C no RTC!')
            self.assertIsNone(self.api._rtc_now())

    def test_controller_sync_keeps_utc_protocol(self):
        with patch.object(self.api, '_send_line') as send:
            self.assertTrue(self.api.sync_rtc()['ok'])
            self.assertTrue(send.call_args.args[0].startswith('RTC SET '))

    def test_local_invalid_date_does_not_crash_reader(self):
        self.api._parse_line('Blumenau | 31/02/2026 21:00:00 BRT')
        self.assertIsNone(self.api._rtc_now())

    def test_disconnect_and_init_require_alignment(self):
        self.api.moon['aligned'] = True
        self.api._parse_line('MOTORES PARADOS: reinicializacao solicitada')
        self.assertFalse(self.api.moon['aligned'])
        self.assertTrue(self.api.tracking_stop.is_set())
        self.api.moon['aligned'] = True
        self.api.disconnect()
        self.assertFalse(self.api.moon['aligned'])
        self.assertIsNone(self.api.position['az'])

    def test_alignment_maps_both_axes_across_north(self):
        self.api.position.update(az=2.0, alt=45.0)
        with patch.object(self.api, '_rtc_now', return_value=datetime.now(timezone.utc)), patch(
            'mount_controller.moon_horizontal_position', return_value=(358.0, 40.0)
        ):
            self.assertTrue(self.api.align_moon()['ok'])
        self.assertEqual(self.api.moon['offset_az'], 4.0)
        self.assertEqual(self.api.moon['offset_alt'], 5.0)
        self.assertEqual(_signed_angle(1.0, 359.0), 2.0)

    def test_tracking_corrects_three_arcminutes_without_waiting_for_48(self):
        self.api.position.update(az=100.0, alt=45.0)
        self.api.moon.update(aligned=True, offset_az=0.0, offset_alt=0.0)
        self.api.fine_tracking_supported = True
        with patch.object(self.api, '_rtc_now', return_value=datetime.now(timezone.utc)), patch(
            'mount_controller.moon_horizontal_position', return_value=(100.05, 45.0)
        ), patch.object(self.api, '_send_line') as send, patch.object(
            self.api.tracking_stop, 'wait', side_effect=lambda _: self.api.tracking_stop.set()
        ) as wait:
            self.api._moon_tracking_loop(4)
            self.assertIn('AZ TRACK 100.0500 4', [c.args[0] for c in send.call_args_list])
            wait.assert_called_once_with(1.0)

    def test_tracking_does_not_chase_errors_below_two_arcminutes(self):
        self.api.position.update(az=100.0, alt=45.0)
        self.api.fine_tracking_supported = True
        with patch.object(self.api, '_rtc_now', return_value=datetime.now(timezone.utc)), patch(
            'mount_controller.moon_horizontal_position', return_value=(100.01, 45.01)
        ), patch.object(self.api, '_send_line') as send, patch.object(
            self.api.tracking_stop, 'wait', side_effect=lambda _: self.api.tracking_stop.set()
        ):
            self.api._moon_tracking_loop(4)
            self.assertFalse(any('TRACK' in c.args[0] for c in send.call_args_list))

    def test_immediate_goto_reply_does_not_leave_motion_stuck(self):
        self.api.position['az'] = 100.0
        with patch.object(self.api, '_send_line', side_effect=lambda _: self.api._parse_line(
            'GOTO: eixo ja esta dentro da tolerancia'
        )):
            self.assertTrue(self.api.goto('AZ', 100.01, 4)['ok'])
        self.assertEqual(self.api.motion['az'], 'parado')

    def test_tracking_requires_fine_firmware(self):
        self.api.serial_port = Mock(is_open=True)
        with patch.object(self.api, '_send_line'):
            self.assertFalse(self.api.start_moon_tracking()['ok'])
        self.api._parse_line('TRACK_ARCMIN=1 | AZ/ALT TRACK <graus> [1-9]')
        self.assertTrue(self.api.fine_tracking_supported)


if __name__ == '__main__':
    unittest.main()
