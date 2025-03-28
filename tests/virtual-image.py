#!/usr/bin/env python3

import sys
try:
    import gi
    import os

    from gi.repository import GLib, Gio

    import unittest
    import socket
    import struct
    import subprocess
    import shutil
    import glob
    import cairo
    import tempfile
except Exception as e:
    print("Missing dependencies: %s" % str(e))
    sys.exit(77)

FPrint = None

# Exit with error on any exception, included those happening in async callbacks
sys.excepthook = lambda *args: (traceback.print_exception(*args), sys.exit(1))

def load_image(img):
    png = cairo.ImageSurface.create_from_png(img)

    # Cairo wants 4 byte aligned rows, so just add a few pixel if necessary
    w = png.get_width()
    h = png.get_height()
    w = (w + 3) // 4 * 4
    h = (h + 3) // 4 * 4
    img = cairo.ImageSurface(cairo.Format.A8, w, h)
    cr = cairo.Context(img)

    cr.set_source_rgba(1, 1, 1, 1)
    cr.paint()

    cr.set_source_rgba(0, 0, 0, 0)
    cr.set_operator(cairo.OPERATOR_SOURCE)

    cr.set_source_surface(png)
    cr.paint()

    return img

if 'FP_PRINTS_PATH' in os.environ:
    prints_path = os.environ['FP_PRINTS_PATH']
else:
    if 'MESON_SOURCE_ROOT' in os.environ:
        root = os.environ['MESON_SOURCE_ROOT']
    else:
        root = os.path.join(os.path.dirname(__file__), '..')

    prints_path = os.path.join(root, 'examples', 'prints')

ctx = GLib.main_context_default()

class VirtualImage(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix='libfprint-')

        cls.sockaddr = os.path.join(cls.tmpdir, 'virtual-image.socket')
        os.environ['FP_VIRTUAL_IMAGE'] = cls.sockaddr

        cls.ctx = FPrint.Context()

        cls.dev = None
        for dev in cls.ctx.get_devices():
            # We might have a USB device in the test system that needs skipping
            if dev.get_driver() == 'virtual_image':
                cls.dev = dev
                break

        assert cls.dev is not None, "You need to compile with virtual_image for testing"

        cls.prints = {}
        for f in glob.glob(os.path.join(prints_path, '*.png')):
            n = os.path.basename(f)[:-4]
            cls.prints[n] = load_image(f)

        assert cls.prints, "No prints found in " + prints_path

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir)
        del cls.dev
        del cls.ctx

    def setUp(self):
        self.dev.open_sync()

        self.con = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.con.connect(self.sockaddr)

    def tearDown(self):
        self.con.close()
        del self.con
        self.dev.close_sync()

    def send_retry(self, retry_error=None, iterate=True):
        retry_error = retry_error if retry_error else FPrint.DeviceRetry.TOO_SHORT
        self.con.sendall(struct.pack('ii', -1, retry_error))
        while iterate and ctx.pending():
            ctx.iteration(False)

    def send_error(self, device_error=None, iterate=True):
        device_error = device_error if device_error else FPrint.DeviceError.GENERAL
        self.con.sendall(struct.pack('ii', -2, device_error))
        while iterate and ctx.pending():
            ctx.iteration(False)

    def send_finger_automatic(self, automatic, iterate=True):
        # Set whether finger on/off is reported around images
        self.con.sendall(struct.pack('ii', -3, 1 if automatic else 0))
        while iterate and ctx.pending():
            ctx.iteration(False)

    def send_finger_report(self, has_finger, iterate=True):
        # Send finger on/off
        self.con.sendall(struct.pack('ii', -4, 1 if has_finger else 0))
        while iterate and ctx.pending():
            ctx.iteration(False)

    def send_image(self, image, iterate=True):
        img = self.prints[image]

        mem = img.get_data()
        mem = mem.tobytes()
        assert len(mem) == img.get_width() * img.get_height()

        encoded_img = struct.pack('ii', img.get_width(), img.get_height())
        encoded_img += mem

        self.con.sendall(encoded_img)
        while iterate and ctx.pending():
            ctx.iteration(False)

    def wait_for_finger_status(self, finger_status, timeout=5000):
        done = False
        def on_timeout_reached():
            nonlocal done
            done = True

        if 'UNDER_VALGRIND' in os.environ:
            timeout = timeout * 3

        source = GLib.timeout_add(timeout, on_timeout_reached)
        while not done:
            if self.dev.get_finger_status() & finger_status:
                GLib.source_remove(source)
                return
            ctx.iteration(True)

        self.assertFalse(done)

    def test_features(self):
        self.assertTrue(self.dev.has_feature(FPrint.DeviceFeature.CAPTURE))
        self.assertTrue(self.dev.has_feature(FPrint.DeviceFeature.IDENTIFY))
        self.assertTrue(self.dev.has_feature(FPrint.DeviceFeature.VERIFY))
        self.assertTrue(self.dev.has_feature(FPrint.DeviceFeature.UPDATE_PRINT))
        self.assertFalse(self.dev.has_feature(FPrint.DeviceFeature.DUPLICATES_CHECK))
        self.assertFalse(self.dev.has_feature(FPrint.DeviceFeature.STORAGE))
        self.assertFalse(self.dev.has_feature(FPrint.DeviceFeature.STORAGE_LIST))
        self.assertFalse(self.dev.has_feature(FPrint.DeviceFeature.STORAGE_DELETE))
        self.assertFalse(self.dev.has_feature(FPrint.DeviceFeature.STORAGE_CLEAR))
        self.assertEqual(self.dev.get_features(),
                         FPrint.DeviceFeature.CAPTURE |
                         FPrint.DeviceFeature.IDENTIFY |
                         FPrint.DeviceFeature.VERIFY |
                         FPrint.DeviceFeature.UPDATE_PRINT)

    def test_capture_prevents_close(self):
        cancel = Gio.Cancellable()
        def cancelled_cb(dev, res, obj):
            print("Capture operation finished")
            with self.assertRaises(GLib.GError) as cm:
                dev.capture_finish(res)
            assert cm.exception.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED)
            print("Capture cancelled as expected")
            obj._cancelled = True

        self._cancelled = False
        self.dev.capture(True, cancel, cancelled_cb, self)

        with self.assertRaises(GLib.GError) as cm:
            self.dev.close_sync()
        assert cm.exception.matches(FPrint.device_error_quark(), FPrint.DeviceError.BUSY)

        cancel.cancel()
        while not self._cancelled:
            ctx.iteration(True)

    def enroll_print(self, image, template=None):
        self._step = 0
        self._enrolled = None

        def progress_cb(dev, step, fp, user_data):
            print('Print was processed, continuing')
            self._step = step

        def done_cb(dev, res):
            print("Enroll done")
            fp = dev.enroll_finish(res)
            self.assertEqual(self.dev.get_finger_status(), FPrint.FingerStatusFlags.NONE)
            self._enrolled = fp

        if template is None:
            template = FPrint.Print.new(self.dev)
            template.props.finger = FPrint.Finger.LEFT_THUMB
            template.props.username = "testuser"
            template.props.description = "test print"
            datetime = GLib.DateTime.new_now_local()
            date = GLib.Date()
            date.set_dmy(*datetime.get_ymd()[::-1])
            template.props.enroll_date = date
        self.assertEqual(self.dev.get_finger_status(), FPrint.FingerStatusFlags.NONE)
        self.dev.enroll(template, None, progress_cb, tuple(), done_cb)

        # Note: Assumes 5 enroll steps for this device!
        self.wait_for_finger_status(FPrint.FingerStatusFlags.NEEDED)
        self.send_image(image)
        while self._step < 1:
            ctx.iteration(True)

        # Test the image-device path where the finger is removed after
        # the minutiae scan is completed.
        self.send_finger_automatic(False)
        self.assertEqual(self.dev.get_finger_status(), FPrint.FingerStatusFlags.NEEDED)
        self.send_finger_report(True)
        self.assertEqual(self.dev.get_finger_status(),
            FPrint.FingerStatusFlags.NEEDED | FPrint.FingerStatusFlags.PRESENT)
        self.send_image(image)
        while self._step < 2:
            ctx.iteration(True)
        self.send_finger_report(False)

        self.assertEqual(self.dev.get_finger_status(), FPrint.FingerStatusFlags.NEEDED)

        self.send_finger_automatic(True)
        self.send_image(image)
        while self._step < 3:
            ctx.iteration(True)

        self.assertEqual(self.dev.get_finger_status(), FPrint.FingerStatusFlags.NEEDED)

        self.send_image(image)
        while self._step < 4:
            ctx.iteration(True)

        self.assertEqual(self.dev.get_finger_status(), FPrint.FingerStatusFlags.NEEDED)

        self.send_image(image)
        while self._enrolled is None:
            ctx.iteration(True)

        self.assertEqual(self.dev.get_finger_status(), FPrint.FingerStatusFlags.NONE)
        self.assertEqual(self._enrolled.props.driver, self.dev.get_driver())
        self.assertEqual(self._enrolled.props.device_id, self.dev.get_device_id())
        self.assertEqual(self._enrolled.props.device_stored,
                         bool(self.dev.get_features() & FPrint.DeviceFeature.STORAGE))
        self.assertIsNone(self._enrolled.get_image())

        return self._enrolled

    def test_enroll_verify(self):
        done = False

        def verify_cb(dev, res):
            try:
                self._verify_match, self._verify_fp = dev.verify_finish(res)
            except gi.repository.GLib.Error as e:
                self._verify_error = e

        fp_whorl = self.enroll_print('whorl')

        self._verify_match = None
        self._verify_fp = None
        self.dev.verify(fp_whorl, callback=verify_cb)
        self.wait_for_finger_status(FPrint.FingerStatusFlags.NEEDED)
        self.send_image('whorl')
        while self._verify_match is None:
            ctx.iteration(True)
        assert(self._verify_match)
        self.assertIsNotNone(self._verify_fp.props.image)

        self._verify_match = None
        self._verify_fp = None
        self.dev.verify(fp_whorl, callback=verify_cb)
        self.wait_for_finger_status(FPrint.FingerStatusFlags.NEEDED)
        self.send_image('tented_arch')
        while self._verify_match is None:
            ctx.iteration(True)
        assert(not self._verify_match)

        # Test fingerprint updates
        # Enroll a second print
        fp_whorl_tended_arch = self.enroll_print('tented_arch', fp_whorl)

        # Make sure the first print verifies successfully after the update
        self._verify_match = None
        self._verify_fp = None
        self.dev.verify(fp_whorl_tended_arch, callback=verify_cb)
        self.wait_for_finger_status(FPrint.FingerStatusFlags.NEEDED)
        self.send_image('whorl')
        while self._verify_match is None:
            ctx.iteration(True)
        assert(self._verify_match)

        # Make sure the second print verifies successfully after the update
        self._verify_match = None
        self._verify_fp = None
        self.dev.verify(fp_whorl_tended_arch, callback=verify_cb)
        self.wait_for_finger_status(FPrint.FingerStatusFlags.NEEDED)
        self.send_image('tented_arch')
        while self._verify_match is None:
            ctx.iteration(True)
        assert(self._verify_match)

        # Test verify error cases
        self._verify_fp = None
        self._verify_error = None
        self.dev.verify(fp_whorl, callback=verify_cb)
        self.wait_for_finger_status(FPrint.FingerStatusFlags.NEEDED)
        self.send_retry()
        while self._verify_fp is None and self._verify_error is None:
            ctx.iteration(True)
        assert(self._verify_error is not None)
        assert(self._verify_error.matches(FPrint.device_retry_quark(), FPrint.DeviceRetry.TOO_SHORT))

        self._verify_fp = None
        self._verify_error = None
        self.dev.verify(fp_whorl, callback=verify_cb)
        self.wait_for_finger_status(FPrint.FingerStatusFlags.NEEDED)
        self.send_error()
        while self._verify_fp is None and self._verify_error is None:
            ctx.iteration(True)
        assert(self._verify_error is not None)
        print(self._verify_error)
        assert(self._verify_error.matches(FPrint.device_error_quark(), FPrint.DeviceError.GENERAL))

    def test_identify(self):
        done = False

        fp_whorl = self.enroll_print('whorl')
        fp_tented_arch = self.enroll_print('tented_arch')

        def identify_cb(dev, res):
            print('Identify finished')
            try:
                self._identify_match, self._identify_fp = self.dev.identify_finish(res)
            except gi.repository.GLib.Error as e:
                print(e)
                self._identify_error = e

        self._identify_fp = None
        self.dev.identify([fp_whorl, fp_tented_arch], callback=identify_cb)
        self.wait_for_finger_status(FPrint.FingerStatusFlags.NEEDED)
        self.send_image('tented_arch')
        while self._identify_fp is None:
            ctx.iteration(True)
        assert(self._identify_match is fp_tented_arch)

        self._identify_fp = None
        self.dev.identify([fp_whorl, fp_tented_arch], callback=identify_cb)
        self.wait_for_finger_status(FPrint.FingerStatusFlags.NEEDED)
        self.send_image('whorl')
        while self._identify_fp is None:
            ctx.iteration(True)
        assert(self._identify_match is fp_whorl)

        # Test error cases
        self._identify_fp = None
        self._identify_error = None
        self.dev.identify([fp_whorl, fp_tented_arch], callback=identify_cb)
        self.wait_for_finger_status(FPrint.FingerStatusFlags.NEEDED)
        self.send_retry()
        while self._identify_fp is None and self._identify_error is None:
            ctx.iteration(True)
        assert(self._identify_error is not None)
        assert(self._identify_error.matches(FPrint.device_retry_quark(), FPrint.DeviceRetry.TOO_SHORT))

        self._identify_fp = None
        self._identify_error = None
        self.dev.identify([fp_whorl, fp_tented_arch], callback=identify_cb)
        self.send_error()
        while self._identify_fp is None and self._identify_error is None:
            ctx.iteration(True)
        assert(self._identify_error is not None)
        assert(self._identify_error.matches(FPrint.device_error_quark(), FPrint.DeviceError.GENERAL))

    def test_verify_serialized(self):
        done = False

        def verify_cb(dev, res):
            r, fp = dev.verify_finish(res)
            self._verify_match = r
            self._verify_fp = fp

        fp_whorl = self.enroll_print('whorl')

        fp_data = fp_whorl.serialize()
        fp_whorl_new = FPrint.Print.deserialize(fp_data)

        # The serialized/deserialized prints need to be equal
        assert fp_whorl.equal(fp_whorl_new)

        datetime = GLib.DateTime.new_now_local()
        date = GLib.Date()
        date.set_dmy(*datetime.get_ymd()[::-1])

        assert fp_whorl_new.props.username == "testuser"
        assert fp_whorl_new.props.description == "test print"
        assert fp_whorl_new.props.finger == FPrint.Finger.LEFT_THUMB
        assert date.compare(fp_whorl_new.props.enroll_date) == 0

        self._verify_match = None
        self._verify_fp = None
        self.dev.verify(fp_whorl_new, callback=verify_cb)
        self.wait_for_finger_status(FPrint.FingerStatusFlags.NEEDED)
        self.send_image('whorl')
        while self._verify_match is None:
            ctx.iteration(True)
        assert(self._verify_match)

        self._verify_match = None
        self._verify_fp = None
        self.dev.verify(fp_whorl_new, callback=verify_cb)
        self.wait_for_finger_status(FPrint.FingerStatusFlags.NEEDED)
        self.send_image('tented_arch')
        while self._verify_match is None:
            ctx.iteration(True)
        assert(not self._verify_match)

if __name__ == '__main__':
    try:
        gi.require_version('FPrint', '2.0')
        from gi.repository import FPrint
    except Exception as e:
        print("Missing dependencies: %s" % str(e))
        sys.exit(77)

    # avoid writing to stderr
    unittest.main(testRunner=unittest.TextTestRunner(stream=sys.stdout, verbosity=2))
