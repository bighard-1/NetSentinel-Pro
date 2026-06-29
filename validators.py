import ipaddress
import re


HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)


def normalize_host_identifier(value: str) -> str:
    host = value.strip()
    if not host:
        raise ValueError("IP地址或主机名不能为空")

    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass

    if HOSTNAME_RE.fullmatch(host):
        return host.rstrip(".").lower()

    raise ValueError("请输入合法的 IPv4、IPv6 地址或主机名")
