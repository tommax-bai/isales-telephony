# dingrtc_pywrap — Windows pybind11 binding for DingRTC C++ SDK 3.9.0.
#
# Mirror of isales-engine cloud Linux binding (deploy/cloud/pybind/dingrtc_pywrap/),
# retargeted to Windows: .lib import lib + .dll runtime DLL co-location model.
#
# Built out-of-source via build.ps1 or direct cmake invocation:
#   $tel = "C:\Users\tianx\codes\isales-telephony"
#   $env:Path = "C:\Program Files\CMake\bin;" + $env:Path
#   $py = "$tel\.venv-3.12\Scripts\python.exe"
#   cmake -S $tel\deploy\edge\windows\pybind\dingrtc_pywrap `
#         -B $tel\build\dingrtc-binding `
#         "-DPython3_EXECUTABLE=$py" "-DCMAKE_BUILD_TYPE=Release"
#   cmake --build $tel\build\dingrtc-binding --config Release
#
# Output: build/dingrtc-binding/Release/dingrtc_pywrap.cp312-win_amd64.pyd
#
# Runtime DLL co-location: all DLLs in $DINGRTC_SDK_ROOT/lib/x64/ must be
# next to .pyd at runtime (Windows loader has no rpath equivalent). build.ps1
# step "copy vendor DLLs" handles this for the frozen exe; for ad-hoc dev
# import, copy them once next to the .pyd or `os.add_dll_directory()` first.

cmake_minimum_required(VERSION 3.20)
project(dingrtc_pywrap LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT WIN32)
    message(FATAL_ERROR
        "dingrtc_pywrap (Windows) targets Windows only. "
        "macOS edge → audio_bridge/macos_dingrtc_pyobjc.py; "
        "Linux cloud → isales-engine/deploy/cloud/pybind/dingrtc_pywrap.")
endif()

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# ---------------------------------------------------------------------------
# Python — must match .venv-3.12 (matches PyInstaller bundle + ABI lock per
# windows-artc-pybind11/design.md Decision 7). build.ps1 探测
# `py -3.12 -c "import sys; print(sys.executable)"` 后传 -DPython3_EXECUTABLE=...
# ---------------------------------------------------------------------------
find_package(Python3 3.12 EXACT COMPONENTS Interpreter Development.Module REQUIRED)

message(STATUS "Python3_EXECUTABLE  = ${Python3_EXECUTABLE}")
message(STATUS "Python3_VERSION     = ${Python3_VERSION}")
message(STATUS "Python3_INCLUDE_DIRS= ${Python3_INCLUDE_DIRS}")

# ---------------------------------------------------------------------------
# pybind11 — installed via `pip install pybind11` in the build venv (.venv-3.12).
# 与 cloud Linux binding 同模式；不沿用 ARTC binding 的 deps/pybind11 submodule
# (避免 §7.11 删 ARTC binding 时跟 deps/ 一起遗失). 若 .venv-3.12 还没装
# pybind11, build.ps1 / 手工 cmake 前需先跑:
#   .\.venv-3.12\Scripts\python.exe -m pip install pybind11
# ---------------------------------------------------------------------------
execute_process(
    COMMAND "${Python3_EXECUTABLE}" -m pybind11 --cmakedir
    OUTPUT_VARIABLE pybind11_DIR_OUT
    OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE pybind11_FIND_RESULT
)
if(NOT pybind11_FIND_RESULT EQUAL 0)
    message(FATAL_ERROR
        "pybind11 not installed in the build venv at ${Python3_EXECUTABLE}. "
        "Run: \"${Python3_EXECUTABLE}\" -m pip install pybind11 — then re-cmake.")
endif()
set(pybind11_DIR "${pybind11_DIR_OUT}")
find_package(pybind11 REQUIRED CONFIG)

# ---------------------------------------------------------------------------
# DingRTC SDK (vendored binaries, gitignored, OUTSIDE this repo).
# Layout (per ~/codes/vendor/DingRTC_Windows_SDK_3_9_0/):
#   api/{engine_interface.h, engine_types.h, engine_conf.h, engine_utils.h, ...}
#   lib/x64/DingRTC.lib              # link-time import lib
#   lib/x64/DingRTC.dll              # runtime, main SDK (27 MB)
#   lib/x64/{ffmpeg,libSR,krt,hbal_se,libcrypto-1_1-x64,
#            libssl-1_1-x64,mediafoundation_capture}.dll  # runtime deps
#   lib/x64/{DingBeauty,MoziWhiteboard,pdfium}.dll        # 旁路 (视频/白板/PDF, 不用)
#   lib/x64/kashost.exe              # SDK helper (按需; runtime co-location 待 §7 实测)
#
# Default vendor location: $ENV{USERPROFILE}/codes/vendor/DingRTC_Windows_SDK_3_9_0
# Override: env ISALES_DINGRTC_WINDOWS_SDK_PATH
# (matches isales-telephony/deploy/edge/windows/STATE.md § DingRTC SDK).
# ---------------------------------------------------------------------------
if(NOT DEFINED ENV{ISALES_DINGRTC_WINDOWS_SDK_PATH})
    set(DINGRTC_SDK_ROOT "$ENV{USERPROFILE}/codes/vendor/DingRTC_Windows_SDK_3_9_0")
else()
    set(DINGRTC_SDK_ROOT "$ENV{ISALES_DINGRTC_WINDOWS_SDK_PATH}")
endif()

set(DINGRTC_INCLUDE_DIR "${DINGRTC_SDK_ROOT}/api")
set(DINGRTC_LIB_DIR     "${DINGRTC_SDK_ROOT}/lib/x64")
set(DINGRTC_IMPLIB      "${DINGRTC_LIB_DIR}/DingRTC.lib")
set(DINGRTC_DLL         "${DINGRTC_LIB_DIR}/DingRTC.dll")

if(NOT EXISTS "${DINGRTC_INCLUDE_DIR}/engine_interface.h")
    message(FATAL_ERROR
        "DingRTC SDK headers not found at ${DINGRTC_INCLUDE_DIR}/engine_interface.h. "
        "Set ISALES_DINGRTC_WINDOWS_SDK_PATH to the SDK extraction root, "
        "or place SDK at %USERPROFILE%/codes/vendor/DingRTC_Windows_SDK_3_9_0/. "
        "Download: https://dingrtc.oss-cn-zhangjiakou.aliyuncs.com/sdk/windows/3.9.0/DingRTC_Windows_SDK_3_9_0.zip")
endif()

if(NOT EXISTS "${DINGRTC_IMPLIB}")
    message(FATAL_ERROR
        "DingRTC SDK import library not found at ${DINGRTC_IMPLIB}. "
        "vendor 目录可能缺 lib/x64/DingRTC.lib — 重解压官方 zip 或检查 SDK 版本/位数 (x64).")
endif()

if(NOT EXISTS "${DINGRTC_DLL}")
    message(FATAL_ERROR
        "DingRTC SDK runtime DLL not found at ${DINGRTC_DLL}. "
        "vendor 目录可能缺 lib/x64/DingRTC.dll — 重解压官方 zip.")
endif()

message(STATUS "DINGRTC_SDK_ROOT    = ${DINGRTC_SDK_ROOT}")
message(STATUS "DINGRTC_INCLUDE_DIR = ${DINGRTC_INCLUDE_DIR}")
message(STATUS "DINGRTC_IMPLIB      = ${DINGRTC_IMPLIB}")

add_library(DingRTC SHARED IMPORTED)
set_target_properties(DingRTC PROPERTIES
    IMPORTED_IMPLIB   "${DINGRTC_IMPLIB}"
    IMPORTED_LOCATION "${DINGRTC_DLL}"
    INTERFACE_INCLUDE_DIRECTORIES "${DINGRTC_INCLUDE_DIR}")

# ---------------------------------------------------------------------------
# pybind11 module.
# ---------------------------------------------------------------------------
pybind11_add_module(dingrtc_pywrap MODULE
    src/bindings.cpp
    src/audio_observer.cpp
    src/engine_listener.cpp
    src/ring_buffer.cpp
)

target_include_directories(dingrtc_pywrap PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
    "${DINGRTC_INCLUDE_DIR}"
)

target_link_libraries(dingrtc_pywrap PRIVATE DingRTC)

# MSVC-specific flags.
#
# Note on /permissive-: ARTC binding 因 vendor 头 anonymous typedef + member
# init 触 C7626 弃用了 /permissive- (见 aliyun_artc_pywrap/CMakeLists.txt
# 注释). DingRTC vendor 头是否也有同类问题待 §7 build 实测; 当前与 ARTC 同
# 策略 — 不开 /permissive-. 若 DingRTC 头干净, 后续可补回作为防御性检查.
if(MSVC)
    target_compile_options(dingrtc_pywrap PRIVATE
        /W3           # reasonable warnings
        /utf-8        # treat sources as UTF-8 (binding strings have CJK comments)
    )
    target_compile_definitions(dingrtc_pywrap PRIVATE
        _CRT_SECURE_NO_WARNINGS
        WIN32_LEAN_AND_MEAN
        NOMINMAX
    )
endif()

# Ensure the output ends up at a predictable path that build.ps1 picks up
# and PyInstaller spec globs.
set_target_properties(dingrtc_pywrap PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/$<CONFIG>"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/$<CONFIG>"
)

message(STATUS "pybind11 version    = ${pybind11_VERSION}")
message(STATUS "DingRTC vendor root = ${DINGRTC_SDK_ROOT}")
message(STATUS "Output .pyd dir     = ${CMAKE_BINARY_DIR}/<Config>")
