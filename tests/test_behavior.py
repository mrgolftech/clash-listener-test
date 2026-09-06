"""Offline regression checks: python -m unittest discover -s tests -v."""
from argparse import Namespace
import unittest
from unittest.mock import MagicMock, patch

import requests
import test_listeners as app


class BehaviorTests(unittest.TestCase):
    def setUp(self):
        self.args = Namespace(
            proxy_host=None, controller='http://127.0.0.1:9097', timeout=1,
            url='https://www.gstatic.com/generate_204', expected_status=204,
            no_ip=False, ip_url='https://api64.ipify.org?format=json')
        self.listener = {'port': 42000, 'listen': '127.0.0.1'}

    def test_only_pass_queries_ip_using_same_proxy(self):
        for protocol, scheme in [('http', 'http://'), ('socks5', 'socks5h://')]:
            for status in (204, 200, 302, 403, 500):
                with self.subTest(protocol=protocol, status=status), \
                        patch.object(app.socket, 'create_connection'), \
                        patch.object(app.requests, 'Session') as factory, \
                        patch.object(app, 'query_exit_ip') as query:
                    session = factory.return_value.__enter__.return_value
                    session.get.return_value.__enter__.return_value.status_code = status
                    query.return_value = {'exit_ip': '8.8.8.8', 'ip_error': None, 'ip_elapsed_ms': 1}
                    result = app.probe(self.listener, protocol, self.args)
                    self.assertEqual(result['ok'], status == 204)
                    self.assertFalse(session.trust_env)
                    self.assertTrue(session.proxies['https'].startswith(scheme))
                    self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
                    if status == 204:
                        query.assert_called_once_with(session, self.args.ip_url, self.args.timeout)
                    else:
                        query.assert_not_called()

    def test_request_timeout_skips_ip(self):
        with patch.object(app.socket, 'create_connection'), \
                patch.object(app.requests, 'Session') as factory, \
                patch.object(app, 'query_exit_ip') as query:
            factory.return_value.__enter__.return_value.get.side_effect = requests.exceptions.Timeout()
            self.assertFalse(app.probe(self.listener, 'http', self.args)['ok'])
            query.assert_not_called()

    def test_no_ip_skips_query(self):
        self.args.no_ip = True
        with patch.object(app.socket, 'create_connection'), \
                patch.object(app.requests, 'Session') as factory, \
                patch.object(app, 'query_exit_ip') as query:
            factory.return_value.__enter__.return_value.get.return_value.__enter__.return_value.status_code = 204
            self.assertTrue(app.probe(self.listener, 'http', self.args)['ok'])
            query.assert_not_called()

    def test_ip_failure_keeps_pass(self):
        with patch.object(app.socket, 'create_connection'), \
                patch.object(app.requests, 'Session') as factory:
            response = MagicMock()
            response.__enter__.return_value.status_code = 204
            session = factory.return_value.__enter__.return_value
            session.get.side_effect = [response, requests.exceptions.Timeout()]
            result = app.probe(self.listener, 'socks5', self.args)
            self.assertTrue(result['ok'])
            self.assertIsNone(result['error'])
            self.assertIsNotNone(result['ip_error'])

    def test_ip_response_validation(self):
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value
        response.status_code = 200
        for body, expected in [
            (b'{"ip":"8.8.8.8"}', '8.8.8.8'),
            (b'{"ip":"2606:4700:4700::1111"}', '2606:4700:4700::1111'),
            (b'{"ip":"127.0.0.1"}', None), (b'{"ip":"invalid"}', None),
            (b'{}', None), (b'<html>error</html>', None), (b'x' * 5000, None),
        ]:
            with self.subTest(body=body[:50]):
                response.iter_content.return_value = [body]
                result = app.query_exit_ip(session, self.args.ip_url, 1)
                self.assertEqual(result['exit_ip'], expected)
                self.assertEqual(bool(result['ip_error']), expected is None)
        response.status_code = 302
        self.assertEqual(app.query_exit_ip(session, self.args.ip_url, 1)['ip_error'], 'HTTP 302')

    def test_color_modes(self):
        self.assertIn('\x1b[32m', app.Console('always').paint('PASS', 'green'))
        self.assertEqual(app.Console('never').paint('PASS', 'green'), 'PASS')
        with patch.dict(app.os.environ, {'NO_COLOR': '1'}):
            self.assertFalse(app.Console('auto').enabled)

    def test_auth_failure_does_not_read_local_config(self):
        with patch.object(app.requests, 'Session') as factory, patch.object(app.Path, 'open') as local:
            factory.return_value.__enter__.return_value.get.return_value.status_code = 401
            with self.assertRaisesRegex(ValueError, '401'):
                app.load_listeners(self.args.controller, 'test-placeholder', None, 1)
            local.assert_not_called()


if __name__ == '__main__':
    unittest.main()
