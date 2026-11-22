"""Upload files to the printer over implicit FTPS (port 990).

Bambu printers need two things Python's ftplib does not do by default:

* implicit TLS: the control connection is TLS from the first byte;
* TLS session reuse on the data connection, otherwise the printer drops
  the transfer.

The printer also tends to hang on the TLS close_notify at the end of an
upload, so ``storbinary`` here skips ``unwrap()``.
"""

from __future__ import annotations

import ftplib
import os
import socket
import ssl
from typing import Callable


def _insecure_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class ImplicitFTPS(ftplib.FTP_TLS):
    """FTP_TLS variant for implicit TLS with data-channel session reuse."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("context", _insecure_context())
        super().__init__(*args, **kwargs)
        self._sock: socket.socket | None = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        # Wrap the control socket in TLS as soon as it is created.
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn, server_hostname=self.host, session=self.sock.session
            )
        return conn, size

    def storbinary(self, cmd, fp, blocksize=32768, callback=None, rest=None):
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd, rest) as conn:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback:
                    callback(buf)
        return self.voidresp()


def upload_file(
    host: str,
    access_code: str,
    local_path: str,
    remote_name: str | None = None,
    port: int = 990,
    use_tls: bool = True,
    username: str = "bblp",
    progress: Callable[[int, int], None] | None = None,
    timeout: float = 30.0,
) -> str:
    """Upload ``local_path`` to the printer. Returns the remote file name.

    ``progress(sent_bytes, total_bytes)`` is called as data goes out.
    ``use_tls=False`` is only for tests against a plain FTP server.
    """
    remote_name = remote_name or os.path.basename(local_path)
    total = os.path.getsize(local_path)
    sent = 0

    def on_chunk(buf: bytes) -> None:
        nonlocal sent
        sent += len(buf)
        if progress:
            progress(sent, total)

    ftp: ftplib.FTP = ImplicitFTPS(timeout=timeout) if use_tls else ftplib.FTP(timeout=timeout)
    ftp.connect(host, port)
    try:
        ftp.login(username, access_code)
        if use_tls:
            ftp.prot_p()
        ftp.set_pasv(True)
        with open(local_path, "rb") as fp:
            ftp.storbinary(f"STOR {remote_name}", fp, callback=on_chunk)
    finally:
        try:
            ftp.quit()
        except (OSError, EOFError, ftplib.Error):
            ftp.close()
    return remote_name


def list_files(
    host: str,
    access_code: str,
    port: int = 990,
    use_tls: bool = True,
    username: str = "bblp",
    timeout: float = 15.0,
) -> list[str]:
    """List file names in the printer's storage root (used as a connection test)."""
    ftp: ftplib.FTP = ImplicitFTPS(timeout=timeout) if use_tls else ftplib.FTP(timeout=timeout)
    ftp.connect(host, port)
    try:
        ftp.login(username, access_code)
        if use_tls:
            ftp.prot_p()
        ftp.set_pasv(True)
        return ftp.nlst()
    finally:
        try:
            ftp.quit()
        except (OSError, EOFError, ftplib.Error):
            ftp.close()
