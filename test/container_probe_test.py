import importlib.util
import io
import json
import socket
import ssl
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('probe', Path(__file__).parents[1] / 'lib/container-probe.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ProbeTest(unittest.TestCase):
    def probe(self, body=None, error=None):
        response = Mock()
        response.__enter__ = Mock(return_value=io.StringIO(json.dumps(body)))
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        opener.open.side_effect = error
        config = json.dumps({'url': 'https://broker.test', 'key': 'client-secret-never-print'})
        with patch('builtins.open', return_value=io.StringIO(config)), \
                patch('socket.getaddrinfo', return_value=[]), \
                patch('urllib.request.build_opener', return_value=opener):
            result = module.probe()
        self.assertNotIn('client-secret', json.dumps(result))
        return result

    def test_responses(self):
        self.assertEqual(self.probe({'accounts': ['main']}), {'kind': 'ok', 'accounts': 1})
        self.assertEqual(self.probe({'accounts': []}), {'kind': 'empty', 'accounts': 0})
        self.assertEqual(self.probe({'token': 'never-print'}), {'kind': 'protocol'})
        self.assertEqual(self.probe([]), {'kind': 'protocol'})

    def test_errors(self):
        for status in [401, 403, 302, 500]:
            error = urllib.error.HTTPError('https://broker.test', status, 'private message', {}, None)
            self.assertEqual(self.probe(error=error), {'kind': 'http', 'status': status})
        for reason, kind in [(socket.gaierror('private message'), 'dns'),
                             (ssl.SSLCertVerificationError('private message'), 'tls'),
                             (TimeoutError('private message'), 'connect')]:
            self.assertEqual(self.probe(error=urllib.error.URLError(reason)), {'kind': kind})

    def test_redirect_refused(self):
        self.assertIsNone(module.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://unrelated.test'))


if __name__ == '__main__':
    unittest.main()
