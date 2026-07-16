"""
WyzeSense USB dongle device discovery.

Shared between the hub and the remote — this module must remain free of any
other ws2m imports.  Docker images and release packages place it flat
alongside the hub/remote code, so imports are bare (``import device_discovery``).

The WyzeSense bridge dongle enumerates as a USB HID device with vendor 1a86
(QinHeng Electronics) and product e024.
"""

import os
import stat

DONGLE_USB_VENDOR = "1a86"
DONGLE_USB_PRODUCT = "e024"


def find_all_dongle_devices() -> list[str]:
    """Scan /sys/class/hidraw for WyzeSense dongles.

    Returns exactly one /dev path per physical dongle.  The canonical
    /dev/hidrawN node is preferred when present; otherwise /dev is scanned
    recursively for a character device with a matching major/minor — this is
    what makes bind-mounted udev directories (e.g. /dev/ws2m-dongles/) work
    inside containers that have no direct device passthrough.
    """
    import glob

    devices = []

    for hidraw in glob.glob("/sys/class/hidraw/hidraw*"):
        try:
            # Walk upward until idVendor exists
            path = os.path.realpath(hidraw)
            while path != "/" and not os.path.exists(os.path.join(path, "idVendor")):
                path = os.path.dirname(path)
            if path == "/":
                continue

            # Check vendor/product
            with open(os.path.join(path, "idVendor")) as f:
                vendor = f.read().strip().lower()
            with open(os.path.join(path, "idProduct")) as f:
                product = f.read().strip().lower()
            if vendor != DONGLE_USB_VENDOR or product != DONGLE_USB_PRODUCT:
                continue

            # Read major/minor
            with open(os.path.join(hidraw, "dev")) as f:
                major, minor = map(int, f.read().split(":"))

            device_path = _find_dev_node(os.path.basename(hidraw), major, minor)
            if device_path is not None:
                devices.append(device_path)

        except OSError:
            continue

    return devices


def _device_matches(path: str, major: int, minor: int) -> bool:
    """Return True if *path* is a character device with the given major/minor."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISCHR(st.st_mode) and os.major(st.st_rdev) == major and os.minor(st.st_rdev) == minor


def _find_dev_node(kernel_name: str, major: int, minor: int, dev_root: str = "/dev") -> str | None:
    """Return one *dev_root* path for the character device (*major*, *minor*).

    Prefers the canonical <dev_root>/<kernel_name> node; falls back to a
    recursive scan of *dev_root*.  Returning a single path per device prevents
    opening the same dongle twice when it is reachable via both its canonical
    node and a udev-created alias (e.g. /dev/ws2m-dongles/hidrawN).
    """
    canonical = os.path.join(dev_root, kernel_name)
    if _device_matches(canonical, major, minor):
        return canonical

    stack = [dev_root]
    while stack:
        try:
            for entry in os.scandir(stack.pop()):
                if entry.is_dir(follow_symlinks=False):
                    # Skip irrelevant dirs
                    if not entry.name.startswith(("pts", "shm", "mqueue")):
                        stack.append(entry.path)
                    continue
                if _device_matches(entry.path, major, minor):
                    return entry.path
        except OSError:
            pass

    return None


def list_char_devices(directory: str) -> list[str]:
    """Return sorted paths of all character devices directly inside *directory*.

    Used when the configured dongle value is a directory (e.g. a udev-managed
    /dev/ws2m-dongles/ folder bind-mounted into the container).
    """
    devices: list[str] = []
    try:
        for entry in os.scandir(directory):
            try:
                st = entry.stat(follow_symlinks=True)
            except OSError:
                continue
            if stat.S_ISCHR(st.st_mode):
                devices.append(entry.path)
    except OSError:
        return []
    return sorted(devices)
