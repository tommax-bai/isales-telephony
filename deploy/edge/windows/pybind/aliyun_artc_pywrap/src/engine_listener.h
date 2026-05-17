#pragma once

#include <pybind11/pybind11.h>

#include <mutex>
#include <string>

#include <rtc/engine_interface.h>

namespace iartc {

namespace py = pybind11;

/// AliEngineEventListener派生类，把入会 / 离会 / 断连 / 错误事件 marshal 回
/// Python 端注册的 callable。事件率 < 10 Hz，trampoline + GIL acquire
/// 开销可忽略 (Decision 3 in design.md)。
///
/// 调用约定：Python 侧一次 set_event_callbacks(...) 注册四个 callable，
/// C++ 侧持引用，回调时显式 `py::gil_scoped_acquire` 后调 callable。
/// Python callable 异常 → C++ 侧捕获 + 落到 SDK 日志，绝不抛回到 C++。
class EngineListener final : public AliRTCSdk::AliEngineEventListener {
public:
    EngineListener() = default;
    ~EngineListener() override = default;

    // Called from Python (GIL held) to register / unregister callables.
    void set_on_join(py::object cb);
    void set_on_leave(py::object cb);
    void set_on_connection_lost(py::object cb);
    void set_on_error(py::object cb);
    void clear_callbacks();

    // Overrides — called from SDK threads (GIL NOT held by default).
    void OnJoinChannelResult(int result, const char *channel, const char *userId,
                              int elapsed) override;
    void OnLeaveChannelResult(int result, AliRTCSdk::AliEngineStats stats) override;
    void OnConnectionLost() override;
    void OnOccurError(int error, const char *msg) override;

private:
    std::mutex mu_;
    py::object on_join_;
    py::object on_leave_;
    py::object on_connection_lost_;
    py::object on_error_;
};

}  // namespace iartc
