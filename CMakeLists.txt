cmake_minimum_required(VERSION 3.26)
project(cufast_native LANGUAGES CXX)

if(NOT WIN32)
  message(FATAL_ERROR "cufast targets Windows only (DXGI / WIC / SendInput).")
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Python 3.14 REQUIRED COMPONENTS Interpreter Development.Module)
find_package(nanobind CONFIG REQUIRED)

nanobind_add_module(_native NB_STATIC
  src/cufast_native/module.cpp
)

target_compile_definitions(_native PRIVATE
  NOMINMAX
  WIN32_LEAN_AND_MEAN
  UNICODE
  _UNICODE
)

# /arch:AVX2 for the downscale kernel; /O2 comes from Release.
target_compile_options(_native PRIVATE /arch:AVX2 /fp:fast)

target_link_libraries(_native PRIVATE
  d3d11 dxgi windowscodecs user32 gdi32 ole32 shcore
)

install(TARGETS _native LIBRARY DESTINATION cufast)
