#include "audio_observer.h"

#include <cstring>
#include <vector>

namespace iartc {

using AliRTCSdk::AliEngineAudioRawData;

namespace {

PcmFrame to_pcm_frame(const AliEngineAudioRawData &raw, const char *uid) {
    PcmFrame f;
    const std::size_t total_bytes =
        static_cast<std::size_t>(raw.numOfSamples) *
        static_cast<std::size_t>(raw.bytesPerSample) *
        static_cast<std::size_t>(raw.numOfChannels);
    if (raw.dataPtr != nullptr && total_bytes > 0) {
        f.pcm.resize(total_bytes);
        std::memcpy(f.pcm.data(), raw.dataPtr, total_bytes);
    }
    f.sample_rate = raw.samplesPerSec;
    f.channels = raw.numOfChannels;
    f.bytes_per_sample = raw.bytesPerSample;
    f.num_samples = raw.numOfSamples;
    f.remote_uid = uid ? std::string(uid) : std::string();
    f.remote_capture_ms = raw.remoteCaptureTimeMs;
    return f;
}

}  // namespace

AudioObserver::AudioObserver(std::shared_ptr<FrameRingBuffer> buffer,
                             std::string expected_remote_uid)
    : buffer_(std::move(buffer)),
      expected_remote_uid_(std::move(expected_remote_uid)) {}

bool AudioObserver::OnCapturedAudioFrame(AliEngineAudioRawData) { return true; }
bool AudioObserver::OnProcessCapturedAudioFrame(AliEngineAudioRawData) { return true; }
bool AudioObserver::OnPublishAudioFrame(AliEngineAudioRawData) { return true; }

bool AudioObserver::OnPlaybackAudioFrame(AliEngineAudioRawData raw) {
    // Mixed playback path — used when we want a single mixed stream from
    // all remote users. v1.0 has exactly 1 remote (the engine uid), so
    // playback == remote audio.
    if (buffer_) {
        buffer_->push(to_pcm_frame(raw, nullptr));
    }
    return true;
}

bool AudioObserver::OnMixedAllAudioFrame(AliEngineAudioRawData) { return true; }

bool AudioObserver::OnRemoteUserAudioFrame(const char *uid,
                                           AliEngineAudioRawData raw) {
    if (!buffer_) return true;
    if (!expected_remote_uid_.empty() &&
        (uid == nullptr || expected_remote_uid_ != uid)) {
        return true;
    }
    buffer_->push(to_pcm_frame(raw, uid));
    return true;
}

}  // namespace iartc
