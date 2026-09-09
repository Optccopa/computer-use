cmake_minimum_required(VERSION 3.26)
project(cufast_native LANGUAGES CXX)

if(NOT WIN32)
  message(FATAL_ERROR "cufast targets Windows only (DXGI / WIC / SendInput).")
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Python 3.14 REQUIRED COMPONENTS Interpreter Development.Module)
find_package(nanobind CONFIG REQUIRED)

# Everything that touches the screen, the mouse or the keyboard, with no binding
# layer in it. Split out as its own library so it has more than one consumer: the
# Python module and the cufast CLI. That second consumer is what makes it possible
# to exercise the capture and input paths with nanobind out of the picture -- until
# now every native test ran through the binding, so a bug in the binding and a bug
# in the capture were indistinguishable from the test's side.
add_library(cufast_core STATIC
  src/native/capture/capture.cpp
  src/native/image/image.cpp
  src/native/input/input.cpp
  src/native/hotkey/hotkey.cpp
)

# Includes are folder-qualified from src/native, so a file says where it comes from
# ("image/image.hpp") rather than relying on being in the same directory.
target_include_directories(cufast_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src/native)

target_compile_definitions(cufast_core PUBLIC
  NOMINMAX
  WIN32_LEAN_AND_MEAN
  UNICODE
  _UNICODE
  # SetProcessDpiAwarenessContext and PER_MONITOR_AWARE_V2 need the Win10 SDK surface.
  _WIN32_WINNT=0x0A00
  WINVER=0x0A00
)

# /arch:AVX2 for the downscale kernel; /O2 comes from Release. PUBLIC so both
# consumers are built the same way -- the module refuses to load without AVX2, and a
# CLI compiled differently would be testing a different binary than the one shipped.
target_compile_options(cufast_core PUBLIC /arch:AVX2 /fp:fast)

target_link_libraries(cufast_core PUBLIC
  d3d11 dxgi windowscodecs user32 gdi32 ole32 shcore
)

# The Python module: bindings only, everything else comes from the core.
#
# NOMINSIZE is load-bearing: nanobind defaults to /Os (FavorSizeOrSpeed=Size) to
# keep binding glue small, which also suppresses auto-vectorization and unrolling
# in the downscale kernel -- the hottest loop in the project.
nanobind_add_module(_native NB_STATIC NOMINSIZE LTO
  src/native/bindings/module.cpp
)
target_link_libraries(_native PRIVATE cufast_core)

install(TARGETS _native LIBRARY DESTINATION cufast)
