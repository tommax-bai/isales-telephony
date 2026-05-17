#include "engine_listener.h"

#include <pybind11/gil.h>

#include <exception>

namespace iartc {

namespace {

void invoke_locked(py::object &cb, py::args args) {
    if (!cb || cb.is_none()) return;
    try {
        cb(*args);
    } catch (const py::error_already_set &e) {
        // Don't propagate Python exceptions back into the SDK's C++ frame.
        // Log via Python's stdout — the Python adapter wires up logging
        // before registering callbacks.
        PyErr_WriteUnraisable(cb.ptr());
    } catch (const std::exception &) {
        // Swallow native exceptions for the same reason.
    }
}

}  // namespace

void EngineListener::set_on_join(py::object cb) {
    std::lock_guard<std::mutex> lk(mu_);
    on_join_ = std::move(cb);
}

void EngineListener::set_on_leave(py::object cb) {
    std::lock_guard<std::mutex> lk(mu_);
    on_leave_ = std::move(cb);
}

void EngineListener::set_on_connection_lost(py::object cb) {
    std::lock_guard<std::mutex> lk(mu_);
    on_connection_lost_ = std::move(cb);
}

void EngineListener::set_on_error(py::object cb) {
    std::lock_guard<std::mutex> lk(mu_);
    on_error_ = std::move(cb);
}

void EngineListener::clear_callbacks() {
    py::gil_scoped_acquire gil;
    std::lock_guard<std::mutex> lk(mu_);
    on_join_ = py::object();
    on_leave_ = py::object();
    on_connection_lost_ = py::object();
    on_error_ = py::object();
}

void EngineListener::OnJoinChannelResult(int result, const char *channel,
                                          const char *userId, int elapsed) {
    py::gil_scoped_acquire gil;
    py::object cb;
    {
        std::lock_guard<std::mutex> lk(mu_);
        cb = on_join_;
    }
    invoke_locked(cb,
                  py::make_tuple(result,
                                 channel ? channel : "",
                                 userId ? userId : "",
                                 elapsed));
}

void EngineListener::OnLeaveChannelResult(int result, AliRTCSdk::AliEngineStats) {
    py::gil_scoped_acquire gil;
    py::object cb;
    {
        std::lock_guard<std::mutex> lk(mu_);
        cb = on_leave_;
    }
    invoke_locked(cb, py::make_tuple(result));
}

void EngineListener::OnConnectionLost() {
    py::gil_scoped_acquire gil;
    py::object cb;
    {
        std::lock_guard<std::mutex> lk(mu_);
        cb = on_connection_lost_;
    }
    invoke_locked(cb, py::make_tuple());
}

void EngineListener::OnOccurError(int error, const char *msg) {
    py::gil_scoped_acquire gil;
    py::object cb;
    {
        std::lock_guard<std::mutex> lk(mu_);
        cb = on_error_;
    }
    invoke_locked(cb, py::make_tuple(error, msg ? msg : ""));
}

}  // namespace iartc
