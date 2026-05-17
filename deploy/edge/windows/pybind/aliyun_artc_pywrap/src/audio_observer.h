#pragma once

#include "ring_buffer.h"

#include <atomic>
#include <memory>

// ARTC SDK headers — paths set via vendor include directory.
#include <rtc/engine_media_engine.h>

namespace iartc {

/// IAudioFrameObserver implementation that hands remote-user PCM frames to
/// a Python-side ring buffer.
///
/// IMPORTANT: every On*AudioFrame callback runs on the SDK audio I/O thread,
/// NOT the Python main thread. We must NOT call into Python here — copy
/// PCM bytes, push to `buffer_`, return.
class AudioObserver final : public AliRTCSdk::IAudioFrameObserver {
public:
    AudioObserver(std::shared_ptr<FrameRingBuffer> buffer,
                  std::string expected_remote_uid);
    ~AudioObserver() override = default;

    // Required pure-virtual callbacks. We only consume the playback /
    // remote-user paths; the rest are no-ops that return true (don't
    // suppress the frame from the SDK's internal pipeline).
    bool OnCapturedAudioFrame(AliRTCSdk::AliEngineAudioRawData audioRawData) override;
    bool OnProcessCapturedAudioFrame(AliRTCSdk::AliEngineAudioRawData audioRawData) override;
    bool OnPublishAudioFrame(AliRTCSdk::AliEngineAudioRawData audioRawData) override;
    bool OnPlaybackAudioFrame(AliRTCSdk::AliEngineAudioRawData audioRawData) override;
    bool OnMixedAllAudioFrame(AliRTCSdk::AliEngineAudioRawData audioRawData) override;
    bool OnRemoteUserAudioFrame(const char *uid,
                                AliRTCSdk::AliEngineAudioRawData audioRawData) override;

    // Returns the underlying buffer for the Python drainer to read.
    std::shared_ptr<FrameRingBuffer> buffer() const { return buffer_; }

private:
    std::shared_ptr<FrameRingBuffer> buffer_;
    std::string expected_remote_uid_;  // optional filter; empty = accept all
};

}  // namespace iartc
