"""TLS mounts must work without chmod when the controller owns permissions."""
import errno
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from common.util import ssl_config


@pytest.mark.parametrize('mode', [0o600, 0o700])
def test_secure_mount_needs_no_write(monkeypatch, mode):
    monkeypatch.setattr(ssl_config, 'os', SimpleNamespace(
        stat=lambda _: SimpleNamespace(st_mode=mode), chmod=Mock()))
    chmod = Mock(side_effect=OSError(errno.EROFS, 'read-only'))
    monkeypatch.setattr(ssl_config.os, 'chmod', chmod)
    ssl_config._restrict_ssl_permissions('/mounted/secret', mode)
    chmod.assert_not_called()


@pytest.mark.parametrize('error', [errno.EROFS, errno.EPERM, errno.EACCES])
@pytest.mark.parametrize('mode', [0o400, 0o440, 0o750])
def test_secure_controller_permissions_retained(monkeypatch, error, mode):
    monkeypatch.setattr(ssl_config, 'os', SimpleNamespace(
        stat=lambda _: SimpleNamespace(st_mode=mode), chmod=Mock()))
    monkeypatch.setattr(ssl_config.os, 'chmod', Mock(side_effect=OSError(error, 'managed mount')))
    ssl_config._restrict_ssl_permissions('/mounted/secret', 0o600)


@pytest.mark.parametrize('mode', [0o644, 0o666, 0o777, 0o660])
def test_insecure_unmodifiable_mount_fails(monkeypatch, mode):
    monkeypatch.setattr(ssl_config, 'os', SimpleNamespace(
        stat=lambda _: SimpleNamespace(st_mode=mode), chmod=Mock()))
    monkeypatch.setattr(ssl_config.os, 'chmod', Mock(side_effect=OSError(errno.EROFS, 'read-only')))
    with pytest.raises(PermissionError, match='Unsafe permissions'):
        ssl_config._restrict_ssl_permissions('/mounted/secret', 0o600)


def test_other_io_failure_not_swallowed(monkeypatch):
    monkeypatch.setattr(ssl_config, 'os', SimpleNamespace(
        stat=lambda _: SimpleNamespace(st_mode=0o400), chmod=Mock()))
    monkeypatch.setattr(ssl_config.os, 'chmod', Mock(side_effect=OSError(errno.EIO, 'io error')))
    with pytest.raises(OSError) as exc:
        ssl_config._restrict_ssl_permissions('/mounted/secret', 0o600)
    assert exc.value.errno == errno.EIO
