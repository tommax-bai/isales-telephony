"""Read-only USB endpoint enumeration for the SIM7600 (VID 1e0e PID 9001).

Answers the STATE.md path-A precondition: does MI_04 (audio interface) have
separate bulk IN + bulk OUT endpoints, or only one shared endpoint?

Non-destructive: only reads the active configuration descriptor via libusb's
cached enumeration; does NOT claim the interface or detach the serial driver.
"""

import usb.core
import usb.util
import libusb_package

EP_TYPE = {0: "CTRL", 1: "ISOC", 2: "BULK", 3: "INTR"}


def main() -> None:
    be = libusb_package.get_libusb1_backend()
    devs = list(usb.core.find(find_all=True, idVendor=0x1E0E, idProduct=0x9001, backend=be))
    print(f"found {len(devs)} device(s) VID=1e0e PID=9001")
    for d in devs:
        print(f"\n=== bus {d.bus} addr {d.address} ===")
        try:
            cfg = d.get_active_configuration()
        except Exception as e:  # noqa: BLE001
            print("  get_active_configuration failed:", e)
            try:
                cfg = d[0]
            except Exception as e2:  # noqa: BLE001
                print("  cfg[0] also failed:", e2)
                continue
        print(f"  config #{cfg.bConfigurationValue}, {cfg.bNumInterfaces} interfaces")
        for intf in cfg:
            mi = intf.bInterfaceNumber
            tag = "  <== MI_04 AUDIO" if mi == 4 else ""
            print(
                f"  -- MI_{mi:02d} alt{intf.bAlternateSetting} "
                f"class={intf.bInterfaceClass:#04x} sub={intf.bInterfaceSubClass:#04x} "
                f"proto={intf.bInterfaceProtocol:#04x} nEP={intf.bNumEndpoints}{tag}"
            )
            for ep in intf:
                addr = ep.bEndpointAddress
                direction = "IN " if addr & 0x80 else "OUT"
                t = EP_TYPE.get(usb.util.endpoint_type(ep.bmAttributes), "?")
                print(
                    f"       ep 0x{addr:02x}  {direction}  {t}  maxpkt={ep.wMaxPacketSize}"
                )


if __name__ == "__main__":
    main()
