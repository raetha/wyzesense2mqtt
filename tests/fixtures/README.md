# Test Fixtures

This directory holds byte captures from a real WyzeSense USB dongle for use
in regression tests.  The files are optional — the test suite runs fine
without them, but `test_dongle_protocol.py::test_*_from_fixture` tests will
be skipped if the files are absent.

## Files

| File | Contents |
|------|----------|
| `hid_capture.bin` | Raw HID frames captured from `/dev/hidraw0` |

## Format

`hid_capture.bin` is a simple length-prefixed binary stream, one frame per
record:

```
[1 byte: frame length N][N bytes: raw HID frame data]
[1 byte: frame length N][N bytes: raw HID frame data]
...
```

This is the exact format written by `tools/capture_hid.py`.

## MAC address obfuscation

All sensor and dongle MAC addresses in the capture should be replaced with
`AAAAAAAA` (first sensor), `BBBBBBBB` (second), etc., and the dongle MAC
with `DONGLE01`.  The `tools/capture_hid.py` script prompts for the real
MACs to replace before saving the file.

## Regenerating

Run `python3 tools/capture_hid.py` for 60 seconds with sensors active, then
copy `hid_capture.bin` here.  See the script for full usage.
