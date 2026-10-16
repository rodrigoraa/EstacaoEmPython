"""Aceita cabecalhos de proxy so de remetentes imediatos autorizados."""

from ipaddress import ip_address, ip_network

from werkzeug.middleware.proxy_fix import ProxyFix


class TrustedProxyFix:
    def __init__(self, app, networks):
        self.app = app
        self.networks = tuple(ip_network(value.strip(), strict=False) for value in networks)
        if any(network.prefixlen == 0 for network in self.networks):
            raise ValueError("TRUSTED_PROXY_IPS nao pode confiar em toda a Internet")
        # Host comes from the normal Host header, checked by Flask TRUSTED_HOSTS.
        self.proxy_app = ProxyFix(app, x_for=1, x_proto=1, x_host=0, x_port=0)

    def __call__(self, environ, start_response):
        try:
            address = ip_address(environ.get("REMOTE_ADDR", ""))
            trusted = any(address in network for network in self.networks)
        except ValueError:
            trusted = False
        return (self.proxy_app if trusted else self.app)(environ, start_response)
