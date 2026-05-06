"""BlueZ pairing Agent.

Ported from hannahbee91/nuxbt (v1.1.2 - "Introduced a bluez agent to
silently accept pairing requests on the host").

Without an agent, BlueZ may show a system pairing-confirmation popup the
first time a Switch tries to pair with the emulated controller. If that
popup is not answered the connection eventually times out, which has been
the main source of "controller never connects" / "first connect is
flaky" reports.

This module registers a tiny D-Bus object that automatically accepts every
pairing request, suppressing the popup and making first-time connections
deterministic. ``run_agent_loop`` is intended to be launched in its own
``multiprocessing.Process`` from ``Nxbt.__init__``.

The agent depends on PyGObject (``gi.repository.GLib``) for its main
loop. PyGObject is *not* a hard nxbt dependency, so all imports are kept
local and any ImportError causes ``run_agent_loop`` to log a warning and
return cleanly instead of crashing the parent ``Nxbt`` process.
"""

import collections
import collections.abc
import logging


# dbus-python still references collections.Sequence on Python 3.10+
# where it has moved to collections.abc.Sequence. Patch it before any
# dbus.service import below.
if not hasattr(collections, "Sequence"):
    collections.Sequence = collections.abc.Sequence


AGENT_INTERFACE = "org.bluez.Agent1"
AGENT_MANAGER_INTERFACE = "org.bluez.AgentManager1"
SERVICE_NAME = "org.bluez"
BLUEZ_OBJECT_PATH = "/org/bluez"

DEFAULT_AGENT_PATH = "/org/bluez/nxbt_agent"


def _build_agent_class():
    """Build the BlueZAgent class lazily so importing this module does
    not require dbus / PyGObject to be installed."""

    import dbus.service

    class BlueZAgent(dbus.service.Object):
        """A BlueZ Agent that automatically accepts every pairing request,
        suppressing the system "Confirm Pairing" popup."""

        def __init__(self, bus, path):
            self.logger = logging.getLogger("nxbt")
            dbus.service.Object.__init__(self, bus, path)

        @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
        def Release(self):
            pass

        @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
        def AuthorizeService(self, device, uuid):
            self.logger.debug(f"AuthorizeService ({device}, {uuid})")
            return

        @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="s")
        def RequestPinCode(self, device):
            self.logger.debug(f"RequestPinCode ({device})")
            return "0000"

        @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="u")
        def RequestPasskey(self, device):
            self.logger.debug(f"RequestPasskey ({device})")
            import dbus
            return dbus.UInt32("000000")

        @dbus.service.method(AGENT_INTERFACE, in_signature="ouq", out_signature="")
        def DisplayPasskey(self, device, passkey, entered):
            self.logger.debug(
                f"DisplayPasskey ({device}, {passkey} reached {entered})")

        @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
        def DisplayPinCode(self, device, pincode):
            self.logger.debug(f"DisplayPinCode ({device}, {pincode})")

        @dbus.service.method(AGENT_INTERFACE, in_signature="ou", out_signature="")
        def RequestConfirmation(self, device, passkey):
            self.logger.debug(f"RequestConfirmation ({device}, {passkey})")
            return

        @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="")
        def RequestAuthorization(self, device):
            self.logger.debug(f"RequestAuthorization ({device})")
            return

        @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
        def Cancel(self):
            self.logger.debug("Cancel")

    return BlueZAgent


def run_agent_loop(agent_path=DEFAULT_AGENT_PATH):
    """Register a silent pairing agent with BlueZ and run a GLib MainLoop.

    Designed to be invoked as the ``target`` of ``multiprocessing.Process``.
    Blocks until the loop is interrupted (e.g. by terminating the process).

    If PyGObject / dbus-python are missing, logs a warning and returns
    so that the parent ``Nxbt`` instance can keep running without
    silent-pairing support. This preserves backward compatibility with
    environments that have never installed PyGObject.
    """

    logger = logging.getLogger("nxbt")

    try:
        import dbus
        import dbus.mainloop.glib
        import dbus.exceptions
        from gi.repository import GLib
    except ImportError as exc:
        logger.warning(
            "BlueZ pairing agent disabled: %s. Install PyGObject and "
            "dbus-python to silently auto-accept pairing requests.", exc)
        return

    try:
        BlueZAgent = _build_agent_class()
    except ImportError as exc:
        logger.warning(
            "BlueZ pairing agent disabled (dbus.service unavailable): %s",
            exc)
        return

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    bus = dbus.SystemBus()
    agent = BlueZAgent(bus, agent_path)

    try:
        obj = bus.get_object(SERVICE_NAME, BLUEZ_OBJECT_PATH)
        manager = dbus.Interface(obj, AGENT_MANAGER_INTERFACE)
    except dbus.exceptions.DBusException as exc:
        logger.warning(
            "BlueZ AgentManager1 unavailable, pairing agent disabled: %s",
            exc)
        return

    capability = "DisplayYesNo"

    try:
        manager.RegisterAgent(agent_path, capability)
    except dbus.exceptions.DBusException as exc:
        if "AlreadyExists" in str(exc):
            try:
                manager.UnregisterAgent(agent_path)
                manager.RegisterAgent(agent_path, capability)
            except Exception as exc2:
                logger.warning(
                    "Failed to re-register BlueZ pairing agent: %s", exc2)
                return
        else:
            logger.warning("Failed to register BlueZ pairing agent: %s", exc)
            return

    try:
        manager.RequestDefaultAgent(agent_path)
    except Exception as exc:
        logger.warning("Failed to set default BlueZ pairing agent: %s", exc)

    logger.debug("BlueZ silent pairing agent registered at %s", agent_path)

    mainloop = GLib.MainLoop()
    try:
        mainloop.run()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            manager.UnregisterAgent(agent_path)
        except Exception:
            pass
