// aliyun_artc_pywrap — minimal pybind11 binding for the Aliyun ARTC SDK for
// Windows. See openspec/changes/windows-artc-pybind11/ for the full design.
//
// API surface intentionally mirrors what `isales_telephony.audio_bridge.
// windows_rtc_session.WindowsRtcSession` needs to satisfy isales-common's
// `RtcSession` ABC. The binding does NOT expose asyncio constructs —
// thread-safe queue draining happens on the Python side.

#include <pybind11/pybind11.h>
#include <pybind11/gil.h>
#include <pybind11/stl.h>
#include <pybind11/stl_bind.h>

#include "audio_observer.h"
#include "engine_listener.h"
#include "ring_buffer.h"

#include <rtc/engine_interface.h>
#include <rtc/engine_media_engine.h>

#include <atomic>
#include <memory>
#include <mutex>
#include <cstdio>
#include <stdexcept>
#include <string>

namespace py = pybind11;
using namespace iartc;

namespace {

// --- Custom exception -------------------------------------------------------

class AliyunArtcError : public std::runtime_error {
public:
    AliyunArtcError(int code, const std::string &msg)
        : std::runtime_error(msg), code_(code) {}
    int code() const noexcept { return code_; }

private:
    int code_;
};

// --- Engine wrapper ---------------------------------------------------------
//
// EngineHandle owns:
//   * the AliEngine* (destroyed via AliEngine::Destroy static)
//   * its IAliEngineMediaEngine* (released via Release())
//   * AudioObserver and EngineListener (kept alive by pybind11 `keep_alive`)
//
// Lifecycle invariants:
//   * destroy() must NOT race with audio-frame callbacks — caller (Python
//     adapter) must stop pushing frames and stop draining before destroy.
//   * destroy() is idempotent: second call is a no-op.

class EngineHandle {
public:
    EngineHandle() = default;
    ~EngineHandle() { destroy_unlocked(); }

    EngineHandle(const EngineHandle &) = delete;
    EngineHandle &operator=(const EngineHandle &) = delete;

    void create(const std::string &extras) {
        std::lock_guard<std::mutex> lk(mu_);
        if (engine_) {
            throw AliyunArtcError(/*EALREADY*/ -1, "engine already created");
        }
        py::gil_scoped_release nogil;
        engine_ = AliRTCSdk::AliEngine::Create(extras.empty() ? nullptr : extras.c_str());
        if (!engine_) {
            // Re-acquire GIL inside scope ends; we just need to throw with
            // the GIL — pybind11 handles it.
            throw AliyunArtcError(/*ENOMEM*/ -2, "AliEngine::Create returned nullptr");
        }

        // Resolve IAliEngineMediaEngine via QueryInterface.
        void *iface = nullptr;
        const int qrc = engine_->QueryInterface(
            AliRTCSdk::AliEngineInterfaceMediaEngine, &iface);
        if (qrc != 0 || iface == nullptr) {
            AliRTCSdk::AliEngine::Destroy();
            engine_ = nullptr;
            throw AliyunArtcError(qrc,
                "QueryInterface(MediaEngine) failed");
        }
        media_engine_ = static_cast<AliRTCSdk::IAliEngineMediaEngine *>(iface);
    }

    bool is_alive() const {
        std::lock_guard<std::mutex> lk(mu_);
        return engine_ != nullptr;
    }

    void set_event_listener(EngineListener *listener) {
        std::lock_guard<std::mutex> lk(mu_);
        require_alive_locked();
        const int rc = engine_->SetEngineEventListener(listener);
        if (rc != 0) {
            throw AliyunArtcError(rc, "SetEngineEventListener failed");
        }
    }

    void register_audio_observer(AudioObserver *observer) {
        std::lock_guard<std::mutex> lk(mu_);
        require_media_engine_locked();
        const int rc = media_engine_->RegisterAudioFrameObserver(observer);
        if (rc != 0) {
            throw AliyunArtcError(rc, "RegisterAudioFrameObserver failed");
        }
        registered_observer_ = observer;
    }

    void unregister_audio_observer() {
        std::lock_guard<std::mutex> lk(mu_);
        if (!media_engine_ || !registered_observer_) return;
        media_engine_->UnRegisterAudioFrameObserver(registered_observer_);
        registered_observer_ = nullptr;
    }

    int add_external_audio_stream(int channels, int sample_rate) {
        std::lock_guard<std::mutex> lk(mu_);
        require_media_engine_locked();
        AliRTCSdk::AliEngineExternalAudioStreamConfig cfg{};
        cfg.channels = channels;
        cfg.sampleRate = sample_rate;
        cfg.publishVolume = 100;
        cfg.playoutVolume = 0;       // we don't want local playback
        cfg.enableDataDrive = false;
        const int stream_id = media_engine_->AddExternalAudioStream(cfg);
        if (stream_id < 0) {
            throw AliyunArtcError(stream_id, "AddExternalAudioStream failed");
        }
        return stream_id;
    }

    void push_external_audio(int stream_id, py::bytes pcm, int sample_rate,
                              int channels, int bytes_per_sample) {
        std::string buf = pcm;  // copy from py::bytes
        // numOfSamples = total_bytes / (bytes_per_sample * channels)
        const int total_bytes = static_cast<int>(buf.size());
        if (channels <= 0 || bytes_per_sample <= 0) {
            throw AliyunArtcError(-3, "invalid channel/bytes_per_sample");
        }
        const int num_samples = total_bytes / (bytes_per_sample * channels);

        AliRTCSdk::AliEngineAudioRawData raw{};
        raw.dataPtr = buf.data();
        raw.numOfSamples = num_samples;
        raw.bytesPerSample = bytes_per_sample;
        raw.numOfChannels = channels;
        raw.samplesPerSec = sample_rate;

        int rc;
        {
            std::lock_guard<std::mutex> lk(mu_);
            require_media_engine_locked();
            py::gil_scoped_release nogil;
            rc = media_engine_->PushExternalAudioStreamRawData(stream_id, raw);
        }
        if (rc != 0) {
            throw AliyunArtcError(rc, "PushExternalAudioStreamRawData failed");
        }
    }

    void remove_external_audio_stream(int stream_id) {
        std::lock_guard<std::mutex> lk(mu_);
        if (!media_engine_) return;
        media_engine_->RemoveExternalAudioStream(stream_id);
    }

    // Channel setup setters — Windows ARTC native API 用 setter pattern；
    // 不像 Linux Python wrapper 把这些打包成 JoinChannelConfig struct 传给
    // 5-arg JoinChannel。Per engine_interface.h:3843 SetChannelProfile +
    // :3855 SetClientRole + :4189 PublishLocalAudioStream。SHALL 在 join_
    // channel 之前依序调（per windows-artc-pybind11-join-config spec
    // § "Join channel setup 调用顺序约束"）。

    void set_channel_profile(AliRTCSdk::AliEngineChannelProfile profile) {
        int rc;
        {
            std::lock_guard<std::mutex> lk(mu_);
            require_alive_locked();
            py::gil_scoped_release nogil;
            rc = engine_->SetChannelProfile(profile);
        }
        if (rc != 0) {
            throw AliyunArtcError(rc, "SetChannelProfile returned non-zero");
        }
    }

    void set_client_role(AliRTCSdk::AliEngineClientRole role) {
        int rc;
        {
            std::lock_guard<std::mutex> lk(mu_);
            require_alive_locked();
            py::gil_scoped_release nogil;
            rc = engine_->SetClientRole(role);
        }
        if (rc != 0) {
            throw AliyunArtcError(rc, "SetClientRole returned non-zero");
        }
    }

    // SetExternalAudioSource 不在 Windows ARTC native API 上 (该接口仅
    // 出现在 Linux Python wrapper 层)；Windows 用既有
    // EngineHandle::add_external_audio_stream (调 IAliEngineMediaEngine::
    // AddExternalAudioStream) 创建外部音频流，效果等价。

    void publish_local_audio_stream(bool enabled) {
        int rc;
        {
            std::lock_guard<std::mutex> lk(mu_);
            require_alive_locked();
            py::gil_scoped_release nogil;
            rc = engine_->PublishLocalAudioStream(enabled);
        }
        if (rc != 0) {
            throw AliyunArtcError(rc, "PublishLocalAudioStream returned non-zero");
        }
    }

    void join_channel(const std::string &token, const std::string &channel,
                      const std::string &user_id, const std::string &user_name) {
        int rc;
        {
            std::lock_guard<std::mutex> lk(mu_);
            require_alive_locked();
            py::gil_scoped_release nogil;
            rc = engine_->JoinChannel(token.c_str(), channel.c_str(),
                                       user_id.c_str(), user_name.c_str());
        }
        if (rc != 0) {
            char buf[96];
            std::snprintf(buf, sizeof(buf),
                          "JoinChannel returned rc=%d (0x%08X)", rc, rc);
            throw AliyunArtcError(rc, buf);
        }
    }

    void leave_channel() {
        int rc;
        {
            std::lock_guard<std::mutex> lk(mu_);
            require_alive_locked();
            py::gil_scoped_release nogil;
            rc = engine_->LeaveChannel();
        }
        if (rc != 0) {
            throw AliyunArtcError(rc, "LeaveChannel returned non-zero");
        }
    }

    void destroy() {
        std::lock_guard<std::mutex> lk(mu_);
        destroy_unlocked();
    }

private:
    void require_alive_locked() {
        if (!engine_) throw AliyunArtcError(-4, "engine is destroyed or not created");
    }
    void require_media_engine_locked() {
        require_alive_locked();
        if (!media_engine_) throw AliyunArtcError(-5, "media engine missing");
    }
    void destroy_unlocked() {
        if (registered_observer_ && media_engine_) {
            media_engine_->UnRegisterAudioFrameObserver(registered_observer_);
            registered_observer_ = nullptr;
        }
        if (media_engine_) {
            media_engine_->Release();
            media_engine_ = nullptr;
        }
        if (engine_) {
            // GIL release: Destroy can block on internal threads finishing.
            py::gil_scoped_release nogil;
            AliRTCSdk::AliEngine::Destroy();
            engine_ = nullptr;
        }
    }

    mutable std::mutex mu_;
    AliRTCSdk::AliEngine *engine_ = nullptr;
    AliRTCSdk::IAliEngineMediaEngine *media_engine_ = nullptr;
    AliRTCSdk::IAudioFrameObserver *registered_observer_ = nullptr;
};

}  // namespace

PYBIND11_MODULE(aliyun_artc_pywrap, m) {
    m.doc() = "Project-local pybind11 binding for the Aliyun ARTC SDK on "
              "Windows. See openspec/changes/windows-artc-pybind11/.";

    // Module version — bumped manually when binding ABI changes. Pyinstaller
    // logs this on startup as part of the smoke check.
    m.attr("__version__") = "0.1.0";

    py::register_exception<AliyunArtcError>(m, "AliyunArtcError");

    // ARTC SDK enum bindings — 仅暴露 v1.0 audio-only 场景需要的常量
    // (per windows-artc-pybind11-join-config spec § "必绑定的最小 SDK
    // 接口集" + design D2)。增 enum constant 走独立 change。
    // 注: ARTC SDK 用 typedef enum，值在外层 AliRTCSdk:: 命名空间下不是
    // enum class 嵌套；pybind11 暴露名仍可任意。
    py::enum_<AliRTCSdk::AliEngineChannelProfile>(m, "AliEngineChannelProfile")
        .value("ChannelProfileCommunication", AliRTCSdk::AliEngineCommunication)
        .value("ChannelProfileInteractiveLive", AliRTCSdk::AliEngineInteractiveLive)
        .export_values();

    py::enum_<AliRTCSdk::AliEngineClientRole>(m, "AliEngineClientRole")
        .value("AliEngineClientRoleInteractive",
               AliRTCSdk::AliEngineClientRoleInteractive)
        .value("AliEngineClientRoleLive", AliRTCSdk::AliEngineClientRoleLive)
        .export_values();

    py::class_<PcmFrame>(m, "PcmFrame")
        .def_readonly("pcm",                &PcmFrame::pcm)
        .def_readonly("sample_rate",        &PcmFrame::sample_rate)
        .def_readonly("channels",           &PcmFrame::channels)
        .def_readonly("bytes_per_sample",   &PcmFrame::bytes_per_sample)
        .def_readonly("num_samples",        &PcmFrame::num_samples)
        .def_readonly("remote_uid",         &PcmFrame::remote_uid)
        .def_readonly("remote_capture_ms",  &PcmFrame::remote_capture_ms);

    py::class_<FrameRingBuffer, std::shared_ptr<FrameRingBuffer>>(m, "FrameRingBuffer")
        .def(py::init<std::size_t>(), py::arg("capacity_frames") = 64)
        .def("drain",
             [](FrameRingBuffer &self, std::size_t max_n) {
                 std::vector<PcmFrame> out;
                 self.drain(out, max_n);
                 return out;
             },
             py::arg("max_n") = 8)
        .def("dropped",      &FrameRingBuffer::dropped)
        .def("size_approx",  &FrameRingBuffer::size_approx)
        .def("clear",        &FrameRingBuffer::clear);

    py::class_<AudioObserver, std::shared_ptr<AudioObserver>>(m, "AudioObserver")
        .def(py::init<std::shared_ptr<FrameRingBuffer>, std::string>(),
             py::arg("buffer"), py::arg("expected_remote_uid") = std::string())
        .def("buffer", &AudioObserver::buffer);

    py::class_<EngineListener, std::shared_ptr<EngineListener>>(m, "EngineListener")
        .def(py::init<>())
        .def("set_on_join",             &EngineListener::set_on_join)
        .def("set_on_leave",            &EngineListener::set_on_leave)
        .def("set_on_connection_lost",  &EngineListener::set_on_connection_lost)
        .def("set_on_error",            &EngineListener::set_on_error)
        .def("clear_callbacks",         &EngineListener::clear_callbacks);

    py::class_<EngineHandle>(m, "EngineHandle")
        .def(py::init<>())
        .def("create",                       &EngineHandle::create, py::arg("extras") = std::string())
        .def("is_alive",                     &EngineHandle::is_alive)
        .def("set_event_listener",
             [](EngineHandle &self, std::shared_ptr<EngineListener> l) {
                 self.set_event_listener(l.get());
             },
             py::arg("listener"),
             // keep listener alive while engine alive
             py::keep_alive<1, 2>())
        .def("register_audio_observer",
             [](EngineHandle &self, std::shared_ptr<AudioObserver> obs) {
                 self.register_audio_observer(obs.get());
             },
             py::arg("observer"),
             py::keep_alive<1, 2>())
        .def("unregister_audio_observer", &EngineHandle::unregister_audio_observer)
        .def("add_external_audio_stream", &EngineHandle::add_external_audio_stream,
             py::arg("channels") = 1, py::arg("sample_rate") = 16000)
        .def("push_external_audio",       &EngineHandle::push_external_audio,
             py::arg("stream_id"), py::arg("pcm"), py::arg("sample_rate") = 16000,
             py::arg("channels") = 1, py::arg("bytes_per_sample") = 2)
        .def("remove_external_audio_stream", &EngineHandle::remove_external_audio_stream)
        // Channel setup setters (windows-artc-pybind11-join-config) — SHALL
        // 在 join_channel 之前依序调:
        //   set_channel_profile → set_client_role
        //   → add_external_audio_stream (既有 binding，等价 Linux 的
        //     SetExternalAudioSource —— Windows SDK 没有该 setter，
        //     AddExternalAudioStream 创建外部 stream 即可)
        //   → publish_local_audio_stream → join_channel
        .def("set_channel_profile",       &EngineHandle::set_channel_profile,
             py::arg("profile"))
        .def("set_client_role",           &EngineHandle::set_client_role,
             py::arg("role"))
        .def("publish_local_audio_stream", &EngineHandle::publish_local_audio_stream,
             py::arg("enabled"))
        .def("join_channel",              &EngineHandle::join_channel,
             py::arg("token"), py::arg("channel"), py::arg("user_id"),
             py::arg("user_name") = std::string())
        .def("leave_channel",             &EngineHandle::leave_channel)
        .def("destroy",                   &EngineHandle::destroy);
}
