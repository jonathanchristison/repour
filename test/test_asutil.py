import asyncio
import io
import os
import tempfile
import types
import unittest

import aiohttp
import aiohttp.web

import repour.asutil
import repour.exception

loop = asyncio.get_event_loop()

class TestDownload(unittest.TestCase):
    foo_bar = io.BytesIO(b"just read the instructions")

    @classmethod
    def setUpClass(cls):
        app = aiohttp.web.Application(loop=loop)

        def write(stream):
            @asyncio.coroutine
            def handler(request):
                resp = aiohttp.web.StreamResponse()
                resp.start(request)

                stream.seek(0)
                while True:
                    buf = stream.read(4096)
                    if not buf:
                        break
                    resp.write(buf)
                    yield from resp.drain()
                yield from resp.write_eof()

                return resp

            return handler

        app.router.add_route("GET", "/foo_bar", write(cls.foo_bar))

        host = "localhost"
        port = 51854
        cls.url = "http://{host}:{port}".format(**locals())
        cls.handler = app.make_handler()
        cls.server = loop.run_until_complete(loop.create_server(cls.handler, host, port))

    @classmethod
    def tearDownClass(cls):
        loop.run_until_complete(cls.handler.finish_connections(0.25))
        cls.server.close()
        loop.run_until_complete(cls.server.wait_closed())

    @staticmethod
    def fake_resp(suggest_filename=None):
        first_call = True
        @asyncio.coroutine
        def fake_read(l):
            nonlocal first_call
            first_call = False
            return b"hello" if first_call else None

        if suggest_filename is None:
            headers = {}
        else:
            headers = {
                aiohttp.hdrs.CONTENT_DISPOSITION: 'attachment; filename="{}"'.format(suggest_filename),
            }

        return types.SimpleNamespace(
            content=types.SimpleNamespace(read=fake_read),
            headers=headers,
        )

    def test_content_disposition(self):
        fake_resp = self.fake_resp(suggest_filename="foo.tar")
        self.assertEqual(repour.asutil._find_filename("bar.zip", fake_resp), "foo.tar")

    def test_basename(self):
        fake_resp = self.fake_resp()
        self.assertEqual(repour.asutil._find_filename("bar.zip", fake_resp), "bar.zip")

    def test_download(self):
        buf = io.BytesIO()

        filename = loop.run_until_complete(repour.asutil.download(self.url + "/foo_bar", buf))

        self.assertEqual(buf.getvalue(), self.foo_bar.getvalue())
        self.assertEqual(filename, "foo_bar")

class TestTemporaryDirectory(unittest.TestCase):
    def write_test_file(self, root):
        with open(os.path.join(root, "somefile.txt"), "w") as f:
            f.write("blah blah blah")

    def test_rmtree(self):
        with tempfile.TemporaryDirectory() as root:
            d = os.path.join(root, "test123")
            os.mkdir(d)
            self.write_test_file(d)

            loop.run_until_complete(repour.asutil.rmtree(d, loop=loop))
            self.assertFalse(os.path.exists(d))

    def test_tempdir(self):
        with tempfile.TemporaryDirectory() as d:
            self.write_test_file(d)
        self.assertFalse(os.path.exists(d))

class TestExpectOk(unittest.TestCase):
    b = b"just testing\ncongenital optimist\n"
    t = "just testing\ncongenital optimist\n"
    l = ["just testing", "congenital optimist"]

    def test_convert_bytes(self):
        self.assertEqual(repour.asutil._convert_bytes(self.b, "data"), self.b)
        self.assertEqual(repour.asutil._convert_bytes(self.b, "text"), self.t)
        self.assertEqual(repour.asutil._convert_bytes(self.b, "lines"), self.l)
        self.assertEqual(repour.asutil._convert_bytes(self.b, "single"), self.l[0])

    def test_exception(self):
        expect_ok = repour.asutil.expect_ok_closure(repour.exception.PullCommandError)

        ret = None
        with self.assertRaises(repour.exception.PullCommandError):
            ret = loop.run_until_complete(expect_ok(["/bin/false"]))
        self.assertIsNone(ret)

    def test_stdout(self):
        expect_ok = repour.asutil.expect_ok_closure()

        self.assertEqual(loop.run_until_complete(expect_ok(["printf", self.t], stdout="data")), self.b)
        self.assertEqual(loop.run_until_complete(expect_ok(["printf", self.t], stdout="single")), self.l[0])
