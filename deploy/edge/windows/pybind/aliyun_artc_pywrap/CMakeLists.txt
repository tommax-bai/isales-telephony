cmake_minimum_required(VERSION 3.20)
project(aliyun_artc_pywrap LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT WIN32)
    message(FATAL_ERROR
        "aliyun_artc_pywrap targets Windows only. "
        "macOS edge走 mock loopback；Linux edge 不支持。")
endif()

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# ---------------------------------------------------------------------------
# Python — must match the interpreter that PyInstaller bundles in the frozen exe.
# build.ps1 探测 `python --version`，传 -DPython3_EXECUTABLE=<path>.
# ---------------------------------------------------------------------------
find_package(Python3 3.12 EXACT COMPONENTS Interpreter Development.Module REQUIRED)

message(STATUS "Python3_EXECUTABLE  = ${Python3_EXECUTABLE}")
message(STATUS "Python3_VERSION     = ${Python3_VERSION}")
message(STATUS "Python3_INCLUDE_DIRS= ${Python3_INCLUDE_DIRS}")

# ---------------------------------------------------------------------------
# pybind11 (header-only, vendored submodule).
# ---------------------------------------------------------------------------
set(PYBIND11_FINDPYTHON ON)
add_subdirectory(deps/pybind11)

# ---------------------------------------------------------------------------
# Aliyun ARTC SDK (vendored binaries, gitignored).
# Layout: ../../vendor/aliyun-artc-windows/{include, x64/Release/AliRTCSdk.lib}
# ---------------------------------------------------------------------------
set(ARTC_VENDOR_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../vendor/aliyun-artc-windows")
set(ARTC_INCLUDE_DIR "${ARTC_VENDOR_ROOT}/include")
set(ARTC_LIB_DIR     "${ARTC_VENDOR_ROOT}/x64/Release")
set(ARTC_LIB_FILE    "${ARTC_LIB_DIR}/AliRTCSdk.lib")

if(NOT EXISTS "${ARTC_INCLUDE_DIR}")
    message(FATAL_ERROR
        "ARTC SDK headers not found at ${ARTC_INCLUDE_DIR}. "
        "Drop the Aliyun ARTC SDK for Windows v7.6.0 zip into "
        "deploy/edge/windows/vendor/aliyun-artc-windows/ before building. "
        "See vendor/README.md for the download URL.")
endif()

if(NOT EXISTS "${ARTC_LIB_FILE}")
    message(FATAL_ERROR
        "ARTC SDK link library not found at ${ARTC_LIB_FILE}. "
        "vendor 目录可能缺 x64/Release/AliRTCSdk.lib — 重新解压官方 zip 或检查 SDK 版本。")
endif()

add_library(AliRTCSdk SHARED IMPORTED)
set_target_properties(AliRTCSdk PROPERTIES
    IMPORTED_IMPLIB   "${ARTC_LIB_FILE}"
    IMPORTED_LOCATION "${ARTC_LIB_DIR}/AliRTCSdk.dll"
    INTERFACE_INCLUDE_DIRECTORIES "${ARTC_INCLUDE_DIR}")

# ---------------------------------------------------------------------------
# pybind11 module.
# ---------------------------------------------------------------------------
pybind11_add_module(aliyun_artc_pywrap MODULE
    src/bindings.cpp
    src/audio_observer.cpp
    src/engine_listener.cpp
    src/ring_buffer.cpp
)

target_include_directories(aliyun_artc_pywrap PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
    "${ARTC_INCLUDE_DIR}"
)

target_link_libraries(aliyun_artc_pywrap PRIVATE AliRTCSdk)

# MSVC-specific flags — match what AliRTCSdk.lib is built with.
if(MSVC)
    target_compile_options(aliyun_artc_pywrap PRIVATE
        /W3           # reasonable warnings
        /permissive-  # strict conformance
        /utf-8        # treat sources as UTF-8 (binding strings have CJK comments)
    )
    target_compile_definitions(aliyun_artc_pywrap PRIVATE
        _CRT_SECURE_NO_WARNINGS
        WIN32_LEAN_AND_MEAN
        NOMINMAX
    )
endif()

# Ensure the output ends up at a predictable path that build.ps1 picks up
# and PyInstaller spec globs.
set_target_properties(aliyun_artc_pywrap PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/$<CONFIG>"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/$<CONFIG>"
)

message(STATUS "pybind11 version    = ${pybind11_VERSION}")
message(STATUS "ARTC vendor root    = ${ARTC_VENDOR_ROOT}")
message(STATUS "Output .pyd dir     = ${CMAKE_BINARY_DIR}/<Config>")
