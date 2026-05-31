"""Enumerate audio devices via DingRTC SDK."""
import os
os.add_dll_directory(r"C:\Users\tianx\codes\vendor\DingRTC_Windows_SDK_3_9_0\lib\x64")
import dingrtc_pywrap as d

engine = d.EngineHandle()
engine.create("")

print("=== Recording devices ===")
for dev in engine.get_recording_device_list():
    print(f"  {dev['name']!r}  id={dev['id']!r}")

print("\n=== Playout devices ===")
for dev in engine.get_playout_device_list():
    print(f"  {dev['name']!r}  id={dev['id']!r}")

engine.destroy()
