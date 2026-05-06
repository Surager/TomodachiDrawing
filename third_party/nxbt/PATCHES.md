# Local modifications to nxbt

This directory contains a modified copy of Brikwerk/nxbt.

Original project:
https://github.com/Brikwerk/nxbt

License:
MIT License

Main changes:

- Add drawing/reliable-input mode.
- Repeatedly send non-neutral HID reports during held states.
- Repeatedly send neutral reports after critical actions.
- Reduce missed state-transition issues during automated drawing.
- Fix wait/neutral lines not clearing button & stick state in protocol:
  wait-only macro lines (e.g. "0.075s") now explicitly drive all
  buttons and sticks to neutral via `set_button_inputs(0,0,0)` and
  centred stick positions, instead of returning early and leaving
  the previous pressed state latched in the HID report.
- Sync `button_status` / `left_stick_centre` / `right_stick_centre`
  persistent fields in ControllerProtocol whenever `set_button_inputs`
  or `set_*_stick_inputs` is called, so that subcommand-reply ticks
  (which re-read those fields via `set_standard_input_report`) emit
  the correct current state instead of a stale snapshot.
- Replace relative-elapsed mainloop sleep with absolute-time metronome:
  the old calculation included the previous iteration's sleep in its
  elapsed measurement, causing alternating fast (~1ms) and slow (~7.5ms)
  frames instead of uniform ~7.6ms (132Hz). Now the server targets
  evenly-spaced ticks using a monotonically advancing deadline, with a
  catch-up reset if drift exceeds 0.5s.
- Assert neutral on every tick when no macro/direct-input is active:
  previously the idle path did not call `set_button_inputs`, so stale
  `button_status` from the last command was re-sent by
  `set_standard_input_report` during the gap between macro chunks.
  This caused buttons (especially DPAD) to stay held for the entire
  inter-chunk delay, making the cursor fly off-screen.
- Assert neutral immediately when a macro line's timer expires (before
  loading the next line): eliminates a 1-tick window where the old
  button state could leak into the next command or the between-line
  report, preventing phantom directional holds on line transitions.
- Fix debug `self.times` array: `pop(0)` instead of `pop()` so the
  sliding window drops the oldest sample, not the newest.
- Add silent BlueZ pairing agent (`nxbt/agent.py`, ported from
  hannahbee91/nuxbt v1.1.2 "Introduced a bluez agent to silently
  accept pairing requests on the host"). Without an agent, BlueZ may
  show a system "Confirm Pairing" popup the first time a Switch tries
  to pair with the emulated controller; if the popup is not answered
  the connection eventually times out, which was the dominant cause
  of "controller never connects" / flaky first-time connection
  reports. `Nxbt.__init__` now spawns a daemonised `multiprocessing
  .Process` running `run_agent_loop`, which registers a tiny D-Bus
  object on `org.bluez.AgentManager1` that auto-accepts every pairing
  request, then drives a `GLib.MainLoop`. `_on_exit` terminates the
  agent process. Imports of `gi.repository.GLib` and `dbus.service`
  are deferred and wrapped in `try/except ImportError`, so the agent
  becomes a soft dependency: when PyGObject is not installed
  `run_agent_loop` logs a warning and exits cleanly, leaving the
  parent `Nxbt` instance unaffected. Agent is registered at the
  rebranded path `/org/bluez/nxbt_agent` (instead of nuxbt's
  `/org/bluez/nuxbt_agent`) so it does not clash with an
  independently-installed nuxbt on the same host.
