import pytest
pytest_plugins = "pytest_homeassistant_custom_component"

@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield

# Some sandboxed CI runners forbid socketpair writes.
# A pipe keeps executor wakeups working without enabling any network access.
import asyncio
import os
import socket


def _install_pipe_wakeup_if_needed():
    reader, writer = socket.socketpair()
    try:
        writer.send(b'\0')
        return
    except PermissionError:
        pass
    finally:
        reader.close()
        writer.close()

    class PipeEnd:
        def __init__(self, fd):
            self.fd = fd
        def fileno(self):
            return self.fd
        def recv(self, size):
            return os.read(self.fd, size)
        def send(self, data):
            return os.write(self.fd, data)
        def close(self):
            os.close(self.fd)

    def make_pipe(loop):
        read_fd, write_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        loop._ssock, loop._csock = PipeEnd(read_fd), PipeEnd(write_fd)
        loop._internal_fds += 1
        loop._add_reader(read_fd, loop._read_from_self)

    asyncio.SelectorEventLoop._make_self_pipe = make_pipe


_install_pipe_wakeup_if_needed()

@pytest.fixture(autouse=True)
def frontend_without_assets(hass):
    """No browser bundle is needed for registry/flow tests or offline runners."""
    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL
    from pytest_homeassistant_custom_component.common import mock_component
    mock_component(hass, 'frontend')
    hass.data[DATA_EXTRA_MODULE_URL] = set()
