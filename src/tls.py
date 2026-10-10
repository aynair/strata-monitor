"""TLS certificate handling: use configured cert or auto-generate self-signed."""

import ipaddress
import logging
import os
import socket
import subprocess

log = logging.getLogger("tls")


def _san(cfg):
    names = ["localhost", "127.0.0.1", "::1"]
    try:
        host = socket.gethostname()
        if host:
            names.append(host)
            fqdn = socket.getfqdn()
            if fqdn and fqdn != host:
                names.append(fqdn)
    except OSError:
        pass
    bind = cfg.get("bind") or "0.0.0.0"
    if bind not in ("0.0.0.0", "::", ""):
        names.append(bind)
    seen, out = set(), []
    for n in names:
        if not n or n in seen:
            continue
        seen.add(n)
        try:
            ipaddress.ip_address(n)
            out.append("IP:%s" % n)
        except ValueError:
            out.append("DNS:%s" % n)
    return ",".join(out)


def ensure_cert(cfg):
    """Return (cert_path, key_path). Auto-generates a self-signed pair when
    tls_cert/tls_key are not configured. Raises RuntimeError on failure."""
    cert = cfg.get("tls_cert")
    key = cfg.get("tls_key")
    if cert and key:
        for p in (cert, key):
            if not os.path.isfile(p):
                raise RuntimeError("tls file not found: %s" % p)
        return cert, key

    state_dir = os.environ.get("STRATA_MONITOR_STATE_DIR")
    if not state_dir:
        db = cfg.get("db") or ""
        if db and db != ":memory:":
            state_dir = os.path.dirname(os.path.abspath(db))
        else:
            state_dir = os.path.join(os.path.expanduser("~"),
                                     ".local", "state", "strata-monitor")
    tls_dir = os.path.join(state_dir, "tls")
    cert = cert or os.path.join(tls_dir, "strata-monitor.crt")
    key = key or os.path.join(tls_dir, "strata-monitor.key")

    if os.path.isfile(cert) and os.path.isfile(key):
        return cert, key

    os.makedirs(tls_dir, mode=0o750, exist_ok=True)
    cn = socket.gethostname() or cfg.get("bind") or "strata-monitor"
    subj = "/CN=%s" % cn
    cmd = ["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048",
           "-days", "825", "-keyout", key, "-out", cert,
           "-subj", subj, "-addext", "subjectAltName=%s" % _san(cfg)]
    log.info("generating self-signed cert -> %s", cert)
    try:
        subprocess.run(cmd, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError("openssl not found; set tls_cert/tls_key in config")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("openssl failed: %s" % exc.stderr.decode(errors="replace"))
    os.chmod(key, 0o600)
    os.chmod(cert, 0o644)
    return cert, key
