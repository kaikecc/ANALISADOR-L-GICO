import csv
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch
import subprocess

from logic_analyzer import AnalyzerModel, read_capture_csv
from logiclab_sigrok import SigrokWorker, convert_binary, scan_devices


class SigrokTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def convert(self, data, rate=24_000_000, a=0, b=1):
        raw = self.root / 'capture.bin'
        raw.write_bytes(data)
        model = AnalyzerModel()
        count = convert_binary(raw, rate, a, b, model)
        return count, [(s.us, s.bits) for s in model.snapshot()]

    def test_submicrosecond_edges_and_final_duration(self):
        count, rows = self.convert(bytes([0, 0, 1, 1, 3, 3]))
        self.assertEqual(count, 6)
        self.assertEqual([b for t, b in rows], [0, 1, 3, 3])
        self.assertAlmostEqual(rows[1][0], 2 / 24)
        self.assertAlmostEqual(rows[-1][0], 5 / 24)

    def test_mapping_high_channels_ignores_unselected_bits(self):
        _, rows = self.convert(bytes([1, 3, 0x40, 0xc0, 0xc1]), rate=1_000_000, a=6, b=7)
        self.assertEqual(rows, [(0, 0), (2, 1), (3, 3), (4, 3)])

    def test_chunk_boundary_does_not_duplicate_edge(self):
        count, rows = self.convert(b'\x01' * (256 * 1024 + 10), rate=1_000_000)
        self.assertEqual(rows, [(0, 1), (count - 1, 1)])

    def test_empty_file(self):
        self.assertEqual(self.convert(b''), (0, []))

    def test_300_seconds_allowed_but_larger_rejected(self):
        worker = SigrokWorker(AnalyzerModel(), queue.Queue())
        worker.configure(24_000_000, 300)
        self.assertEqual(worker.seconds, 300)
        with self.assertRaises(ValueError):
            worker.configure(24_000_000, 301)

    def test_live_conversion_consumes_appended_data_before_completion(self):
        raw = self.root / 'live.bin'
        raw.write_bytes(b'\x00\x01')
        finished = threading.Event()
        observed = threading.Event()
        model = AnalyzerModel()
        add = model.add
        def observe(us, bits):
            add(us, bits)
            if bits == 3:
                observed.set()
        model.add = observe
        thread = threading.Thread(target=convert_binary, args=(raw, 1_000_000, 0, 1, model, None, finished))
        thread.start()
        try:
            with raw.open('ab') as fp:
                fp.write(b'\x03\x03')
            self.assertTrue(observed.wait(2))
            self.assertTrue(thread.is_alive())
        finally:
            finished.set()
            thread.join(2)
        self.assertEqual(model.snapshot()[-1].us, 3)

    def test_stop_interrupts_conversion_inside_chunk(self):
        raw = self.root / 'busy.bin'
        raw.write_bytes(b'\x00\x03' * 100_000)
        worker = SigrokWorker(AnalyzerModel(), queue.Queue())
        original_add = worker.model.add
        def add_and_stop(us, bits):
            original_add(us, bits)
            if worker.model.count() == 10:
                worker.send_immediate('STOP')
        worker.model.add = add_and_stop
        with self.assertRaises(InterruptedError):
            convert_binary(raw, 24_000_000, 0, 1, worker.model, worker.cancelled)
        self.assertLessEqual(worker.model.count(), 1024)
        self.assertTrue(raw.exists())

    def test_stop_after_completion_does_not_leave_native_finalizing(self):
        from logiclab_native import LogicLabAPI
        api = LogicLabAPI()
        self.addCleanup(api.close)
        api.worker = SigrokWorker(api.model, api.events)
        api.worker.port = 'fx2lafw:conn=1.6'
        api.worker.running = False
        self.assertTrue(api.stop_capture()['ok'])
        state = api.poll()
        self.assertFalse(state['finalizing'])
        self.assertFalse(state['capturing'])

    def test_conversion_cancellation(self):
        raw = self.root / 'capture.bin'
        raw.write_bytes(b'\x00')
        event = threading.Event()
        event.set()
        with self.assertRaises(InterruptedError):
            convert_binary(raw, 1_000_000, 0, 1, AnalyzerModel(), event)

    def test_fractional_csv_roundtrip(self):
        target = self.root / 'capture.csv'
        model = AnalyzerModel()
        model.start_recording(target)
        model.add(0, 0)
        model.add(1 / 24, 3)
        model.stop_recording()
        samples, discarded = read_capture_csv(str(target))
        self.assertEqual(discarded, 0)
        self.assertAlmostEqual(samples[1].us, 1 / 24)
        self.assertEqual(samples[1].bits, 3)

    def test_scan_selects_only_eight_channel_fx2(self):
        output = (b'The following devices were found:\n'
                  b'fx2lafw:conn=1.4 - Saleae Logic with 8 channels: D0 D1 D2 D3 D4 D5 D6 D7\n'
                  b'fx2lafw:conn=1.5 - Cypress FX2 with 16 channels: D0 D1\n')
        with patch('logiclab_sigrok.find_sigrok', return_value='sigrok-cli'), patch('logiclab_sigrok.subprocess.run', return_value=subprocess.CompletedProcess([], 0, output, b'')):
            self.assertEqual([d['device'] for d in scan_devices()], ['fx2lafw:conn=1.4'])

    def test_settings_reject_invalid_values(self):
        worker = SigrokWorker(AnalyzerModel(), queue.Queue())
        for args in [(123, 1, 0, 1), (1_000_000, float('nan'), 0, 1),
                     (1_000_000, 0, 0, 1), (1_000_000, 1, 2, 2), (1_000_000, 1, 0, 8)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                worker.configure(*args)

    def test_connect_resolves_changed_address_for_single_device(self):
        worker = SigrokWorker(AnalyzerModel(), queue.Queue())
        with patch('logiclab_sigrok.scan_devices', return_value=[{'device': 'fx2lafw:conn=1.6'}]):
            worker.connect('fx2lafw:conn=1.5')
        self.assertEqual(worker.port, 'fx2lafw:conn=1.6')

    def test_connect_does_not_guess_between_devices_or_empty_selection(self):
        worker = SigrokWorker(AnalyzerModel(), queue.Queue())
        with patch('logiclab_sigrok.scan_devices', return_value=[{'device': 'fx2lafw:conn=1.6'}, {'device': 'fx2lafw:conn=1.7'}]):
            with self.assertRaises(RuntimeError):
                worker.connect('fx2lafw:conn=1.5')
        with patch('logiclab_sigrok.scan_devices', return_value=[{'device': 'fx2lafw:conn=1.6'}]):
            with self.assertRaises(RuntimeError):
                worker.connect('')

    def run_capture(self, code=0, data=b'\x00\x01\x03', stopped=False):
        events = queue.Queue()
        model = AnalyzerModel()
        worker = SigrokWorker(model, events)
        worker.port = 'fx2lafw:conn=1.4'
        worker.configure(1_000_000, .01)
        folder = self.root / 'captures'
        folder.mkdir(exist_ok=True)
        model.start_recording(folder / 'mapped.csv')
        class Process:
            def __init__(self, args, **kwargs):
                self.args = args
                Path(args[args.index('--output-file') + 1]).write_bytes(data)
            def wait(self, **kwargs):
                if stopped:
                    worker.stopped.set()
                return code
        with patch('logiclab_sigrok.__file__', str(self.root / 'logiclab_sigrok.py')), patch('logiclab_sigrok.find_sigrok', return_value='sigrok-cli'), patch('logiclab_sigrok.subprocess.Popen', Process):
            worker._capture()
        metadata = json.loads(next(folder.glob('*.json')).read_text())
        messages = []
        while not events.empty():
            messages.append(events.get_nowait())
        self.assertIsNone(model.recording_file)
        self.assertFalse(worker.running)
        self.assertEqual(messages[-1][0], 'capture_end')
        return metadata, model, messages

    def test_successful_capture_saves_raw_and_metadata(self):
        metadata, model, _ = self.run_capture(data=b'\x00' * 5000 + b'\x03' * 5000)
        self.assertEqual(metadata['status'], 'complete')
        self.assertEqual(metadata['samples'], 10000)
        self.assertEqual(model.snapshot()[1].us, 5000)
        self.assertEqual(metadata['channel_a'], 0)

    def test_stopped_capture_is_labelled_partial(self):
        metadata, model, _ = self.run_capture(code=1, stopped=True)
        self.assertEqual(metadata['status'], 'partial')
        self.assertEqual(metadata['samples'], 3)

    def test_driver_failure_is_not_success(self):
        metadata, model, messages = self.run_capture(code=1)
        self.assertEqual(metadata['status'], 'error')
        self.assertEqual(model.count(), 0)
        self.assertTrue(any(kind == 'error' for kind, value in messages))

    def test_empty_acquisition_reports_error(self):
        metadata, _, _ = self.run_capture(data=b'')
        self.assertEqual(metadata['status'], 'error')


if __name__ == '__main__':
    unittest.main()
