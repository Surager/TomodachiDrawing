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
