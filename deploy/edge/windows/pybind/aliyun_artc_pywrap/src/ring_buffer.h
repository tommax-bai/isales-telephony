#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <vector>

namespace iartc {

/// One captured remote audio frame, ready to hand to Python.
///
/// owns its PCM bytes — the SDK callback's `void*` buffer is copied on the
/// audio I/O thread so the Python drainer can pick it up safely later.
struct PcmFrame {
    std::vector<uint8_t> pcm;       // raw PCM bytes (16-bit signed LE)
    int sample_rate = 0;            // e.g. 16000
    int channels = 0;               // 1 = mono
    int bytes_per_sample = 0;       // 2
    int num_samples = 0;            // per channel
    std::string remote_uid;         // empty for mixed/playout, set for per-user
    uint32_t remote_capture_ms = 0; // from AliEngineAudioRawData.remoteCaptureTimeMs
};

/// Bounded MPSC frame buffer.
///
/// The ARTC SDK invokes audio observer callbacks from a single I/O thread
/// at ~50 Hz, so single-producer is sufficient. The Python drainer is a
/// single asyncio task — single consumer. We use a mutex-protected deque
/// here rather than a fully lock-free SPSC structure: at 50 Hz with frames
/// of ~640 bytes each the lock contention is in the microsecond range and
/// not worth the lock-free complexity for v1.0.
///
/// On overflow (Python drainer too slow), the OLDEST frame is dropped
/// and `dropped` is incremented. The choice matches the WASAPI overflow
/// counter style in isales_telephony/modem_controller/audio/windows_wasapi.py.
class FrameRingBuffer {
public:
    explicit FrameRingBuffer(std::size_t capacity_frames);

    /// Push a frame. Called from the SDK audio I/O thread, GIL NOT held.
    /// Returns true if accepted without dropping, false if the oldest was dropped.
    bool push(PcmFrame frame);

    /// Drain up to `max_n` frames into `out`. Called from the Python
    /// drainer task — caller holds the GIL.
    /// Returns the number drained (may be 0).
    std::size_t drain(std::vector<PcmFrame> &out, std::size_t max_n);

    /// Total frames dropped since construction.
    std::uint64_t dropped() const noexcept { return dropped_.load(std::memory_order_relaxed); }

    /// Current depth (best-effort, no lock).
    std::size_t size_approx() const noexcept { return size_.load(std::memory_order_relaxed); }

    void clear();

private:
    const std::size_t capacity_;
    std::mutex mu_;
    std::vector<PcmFrame> ring_;
    std::size_t head_ = 0; // next write
    std::size_t tail_ = 0; // next read
    std::atomic<std::size_t> size_{0};
    std::atomic<std::uint64_t> dropped_{0};
};

}  // namespace iartc
