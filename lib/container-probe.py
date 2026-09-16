"""One read-only broker request; never print credentials or server response bodies."""
import json
import socket
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the client key to another endpoint.


def probe():
    try:
        with open('/run/broker-client.json') as stream:
            config = json.load(stream)
        url = config['url'].rstrip('/') + '/listAccounts?provider=codex'
        request = urllib.request.Request(url, headers={'x-broker-key': config['key']})
    except (OSError, ValueError, KeyError):
        return {'kind': 'config'}
    try:
        address = urlsplit(url)
        socket.getaddrinfo(address.hostname, address.port or (443 if address.scheme == 'https' else 80))
        # Use the same direct Docker route as the wrapper, without host proxy env.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=10) as response:
            body = json.load(response)
        accounts = body.get('accounts') if isinstance(body, dict) else None
        if not isinstance(accounts, list) or not all(isinstance(a, str) for a in accounts):
            return {'kind': 'protocol'}
        return {'kind': 'ok' if accounts else 'empty', 'accounts': len(accounts)}
    except urllib.error.HTTPError as error:
        return {'kind': 'http', 'status': error.code}
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, 'reason', error)
        kind = 'dns' if isinstance(reason, socket.gaierror) else 'tls' if isinstance(reason, ssl.SSLError) else 'connect'
        return {'kind': kind}
    except ValueError:
        return {'kind': 'protocol'}


if __name__ == '__main__':
    print(json.dumps(probe()))
