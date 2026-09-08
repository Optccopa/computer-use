#include <nanobind/nanobind.h>
namespace nb = nanobind;
NB_MODULE(_native, m) {
    m.def("ping", []() { return 42; });
}
