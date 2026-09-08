cmake_minimum_required(VERSION 3.26)
project(cufast_native LANGUAGES CXX)

if(NOT WIN32)
  message(FATAL_ERROR "cufast targets Windows only (DXGI / WIC / SendInput).")
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Python 3.14 REQUIRED COMPONENTS Interpreter Development.Module)
find_package(nanobind CONFIG REQUIRED)

# NOMINSIZE is load-bearing: nanobind defaults to /Os (FavorSizeOrSpeed=Size) to
# keep binding glue small, which also suppresses auto-vectorization and unrolling
# in the downscale kernel -- the hottest loop in the project.
nanobind_add_module(_native NB_STATIC NOMINSIZE LTO
  src/cufast_native/module.cpp
  src/cufast_native/capture.cpp
  src/cufast_native/image.cpp
  src/cufast_native/input.cpp
  src/cufast_native/hotkey.cpp
)

target_compile_definitions(_native PRIVATE
  NOMINMAX
  WIN32_LEAN_AND_MEAN
  UNICODE
  _UNICODE
  # SetProcessDpiAwarenessContext and PER_MONITOR_AWARE_V2 need the Win10 SDK surface.
  _WIN32_WINNT=0x0A00
  WINVER=0x0A00
)

# /arch:AVX2 for the downscale kernel; /O2 comes from Release.
target_compile_options(_native PRIVATE /arch:AVX2 /fp:fast)

target_link_libraries(_native PRIVATE
  d3d11 dxgi windowscodecs user32 gdi32 ole32 shcore
)

install(TARGETS _native LIBRARY DESTINATION cufast)
