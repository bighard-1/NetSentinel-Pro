"""Validate outbound webhook destinations before configuration and delivery."""

import ipaddress
import socket
from urllib.parse import urlsplit

from services.config import AppSettings


def validate_webhook_url(value: str) -> str:
    url = value.strip()
    if not url:
        return ""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Webhook must use an HTTPS URL without embedded credentials")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("Webhook port is invalid") from exc
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("Webhook hostname could not be resolved") from exc
    if not addresses:
        raise ValueError("Webhook hostname could not be resolved")
    if not AppSettings().allow_private_webhook:
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise ValueError("Webhook may not resolve to a private or reserved address")
    return url
