#include <Python.h>

#include <CoreFoundation/CoreFoundation.h>

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstring>
#include <mutex>
#include <string>
#include <vector>

#include "DeckLinkAPI.h"

namespace {

static std::string cf_string_to_utf8(CFStringRef value)
{
    if (!value)
        return std::string();

    CFIndex length = CFStringGetLength(value);
    CFIndex max_size = CFStringGetMaximumSizeForEncoding(length, kCFStringEncodingUTF8) + 1;
    std::vector<char> buffer(static_cast<size_t>(max_size), '\0');
    if (!CFStringGetCString(value, buffer.data(), max_size, kCFStringEncodingUTF8))
        return std::string();
    return std::string(buffer.data());
}

template <typename T>
static void safe_release(T*& value)
{
    if (value)
    {
        value->Release();
        value = nullptr;
    }
}

static BMDDisplayMode mode_from_string(const std::string& mode)
{
    if (mode == "1080p25" || mode == "HD1080p25" || mode == "Hp25")
        return bmdModeHD1080p25;
    if (mode == "1080p24" || mode == "HD1080p24" || mode == "24ps")
        return bmdModeHD1080p24;
    if (mode == "1080p30" || mode == "HD1080p30" || mode == "Hp30")
        return bmdModeHD1080p30;
    if (mode == "1080p50" || mode == "HD1080p50" || mode == "Hp50")
        return bmdModeHD1080p50;
    if (mode == "1080p60" || mode == "HD1080p60" || mode == "Hp60")
        return bmdModeHD1080p6000;
    if (mode == "1080i50" || mode == "HD1080i50" || mode == "Hi50")
        return bmdModeHD1080i50;
    if (mode == "1080i5994" || mode == "1080i59.94" || mode == "HD1080i5994" || mode == "Hi59")
        return bmdModeHD1080i5994;
    if (mode == "1080i60" || mode == "HD1080i60" || mode == "Hi60")
        return bmdModeHD1080i6000;
    if (mode == "720p50" || mode == "HD720p50" || mode == "hp50")
        return bmdModeHD720p50;
    if (mode == "720p5994" || mode == "720p59.94" || mode == "HD720p5994" || mode == "hp59")
        return bmdModeHD720p5994;
    if (mode == "720p60" || mode == "HD720p60" || mode == "hp60")
        return bmdModeHD720p60;
    if (mode == "2160p25" || mode == "4K2160p25" || mode == "4k25")
        return bmdMode4K2160p25;

    if (mode.size() == 4)
    {
        return static_cast<BMDDisplayMode>(
            (static_cast<uint32_t>(mode[0]) << 24) |
            (static_cast<uint32_t>(mode[1]) << 16) |
            (static_cast<uint32_t>(mode[2]) << 8) |
            static_cast<uint32_t>(mode[3])
        );
    }
    return bmdModeHD1080p25;
}

static BMDVideoConnection connection_from_string(const std::string& connection)
{
    if (connection == "hdmi" || connection == "HDMI")
        return bmdVideoConnectionHDMI;
    if (connection == "sdi" || connection == "SDI")
        return bmdVideoConnectionSDI;
    return bmdVideoConnectionUnspecified;
}

static BMDPixelFormat pixel_format_from_string(const std::string& pixel_format)
{
    if (pixel_format == "yuv" || pixel_format == "uyvy" || pixel_format == "2vuy")
        return bmdFormat8BitYUV;
    return bmdFormat8BitBGRA;
}

static uint8_t clamp_u8(double value)
{
    if (value < 0.0)
        return 0;
    if (value > 255.0)
        return 255;
    return static_cast<uint8_t>(value + 0.5);
}

class NativeDeckLinkOutput;

class OutputCallback final : public IDeckLinkVideoOutputCallback
{
public:
    explicit OutputCallback(NativeDeckLinkOutput* owner) : m_owner(owner), m_ref_count(1) {}

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, LPVOID* ppv) override;
    ULONG STDMETHODCALLTYPE AddRef() override { return ++m_ref_count; }
    ULONG STDMETHODCALLTYPE Release() override
    {
        ULONG value = --m_ref_count;
        if (value == 0)
            delete this;
        return value;
    }

    HRESULT STDMETHODCALLTYPE ScheduledFrameCompleted(
        IDeckLinkVideoFrame* completedFrame,
        BMDOutputFrameCompletionResult result
    ) override;
    HRESULT STDMETHODCALLTYPE ScheduledPlaybackHasStopped(void) override;

private:
    NativeDeckLinkOutput* m_owner;
    std::atomic<ULONG> m_ref_count;
};

class NativeDeckLinkOutput
{
public:
    NativeDeckLinkOutput(
        std::string device_name,
        int width,
        int height,
        double fps,
        std::string mode,
        std::string connection,
        std::string pixel_format
    )
        : m_device_name(std::move(device_name)),
          m_requested_width(width),
          m_requested_height(height),
          m_requested_fps(fps),
          m_mode(mode_from_string(mode)),
          m_connection(connection_from_string(connection)),
          m_pixel_format(pixel_format_from_string(pixel_format)),
          m_latest_frame(static_cast<size_t>(width) * static_cast<size_t>(height) * 4U, 0)
    {
        for (size_t index = 3; index < m_latest_frame.size(); index += 4)
            m_latest_frame[index] = 255;
    }

    ~NativeDeckLinkOutput()
    {
        stop();
    }

    bool start(std::string& error)
    {
        std::lock_guard<std::mutex> guard(m_state_mutex);
        if (m_running)
            return true;

        IDeckLinkIterator* iterator = CreateDeckLinkIteratorInstance();
        if (!iterator)
        {
            error = "DeckLink API unavailable. Check Desktop Video driver installation.";
            return false;
        }

        IDeckLink* candidate = nullptr;
        while (iterator->Next(&candidate) == S_OK)
        {
            std::string name = display_name(candidate);
            if (m_device_name.empty() || name == m_device_name)
            {
                m_decklink = candidate;
                m_device_name = name;
                break;
            }
            candidate->Release();
        }
        iterator->Release();

        if (!m_decklink)
        {
            error = m_device_name.empty() ? "No DeckLink output device found." :
                "DeckLink device not found: " + m_device_name;
            return false;
        }

        if (m_connection != bmdVideoConnectionUnspecified)
        {
            IDeckLinkConfiguration* configuration = nullptr;
            if (m_decklink->QueryInterface(IID_IDeckLinkConfiguration, reinterpret_cast<void**>(&configuration)) == S_OK && configuration)
            {
                configuration->SetInt(bmdDeckLinkConfigVideoOutputConnection, static_cast<int64_t>(m_connection));
                configuration->Release();
            }
        }

        if (m_decklink->QueryInterface(IID_IDeckLinkOutput, reinterpret_cast<void**>(&m_output)) != S_OK || !m_output)
        {
            error = "Selected DeckLink device does not expose IDeckLinkOutput.";
            return fail_locked();
        }

        IDeckLinkDisplayMode* display_mode = nullptr;
        if (m_output->GetDisplayMode(m_mode, &display_mode) != S_OK || !display_mode)
        {
            error = "Requested DeckLink display mode is unavailable.";
            return fail_locked();
        }

        m_width = static_cast<int>(display_mode->GetWidth());
        m_height = static_cast<int>(display_mode->GetHeight());
        if (display_mode->GetFrameRate(&m_frame_duration, &m_time_scale) != S_OK)
        {
            display_mode->Release();
            error = "Could not query DeckLink display-mode frame rate.";
            return fail_locked();
        }
        display_mode->Release();

        bool supported = false;
        BMDDisplayMode actual_mode = bmdModeUnknown;
        HRESULT support_result = m_output->DoesSupportVideoMode(
            m_connection,
            m_mode,
            m_pixel_format,
            bmdNoVideoOutputConversion,
            bmdSupportedVideoModeDefault,
            &actual_mode,
            &supported
        );
        if (support_result != S_OK || !supported)
        {
            error = "DeckLink output does not support 8-bit BGRA for the requested mode.";
            return fail_locked();
        }

        resize_latest_locked(m_width, m_height);

        m_callback = new OutputCallback(this);
        if (!m_callback)
        {
            error = "Could not allocate DeckLink output callback.";
            return fail_locked();
        }
        if (m_output->SetScheduledFrameCompletionCallback(m_callback) != S_OK)
        {
            error = "Could not set DeckLink scheduled-frame callback.";
            return fail_locked();
        }

        if (m_output->EnableVideoOutput(m_mode, bmdVideoOutputFlagDefault) != S_OK)
        {
            error = "Could not enable DeckLink video output.";
            return fail_locked();
        }
        m_video_enabled = true;

        if (!create_ring_frames_locked(error))
            return fail_locked();

        m_running = true;
        m_frames_scheduled = 0;
        m_next_frame = 0;
        for (int index = 0; index < 4; ++index)
        {
            if (!schedule_next_frame())
            {
                error = "Could not schedule initial DeckLink video frames.";
                return fail_locked();
            }
        }

        if (m_output->StartScheduledPlayback(0, m_time_scale, 1.0) != S_OK)
        {
            error = "Could not start DeckLink scheduled playback.";
            return fail_locked();
        }
        m_playback_started = true;
        return true;
    }

    void stop()
    {
        std::lock_guard<std::mutex> guard(m_state_mutex);
        m_running = false;

        if (m_output && m_playback_started)
        {
            m_output->StopScheduledPlayback(0, nullptr, m_time_scale);
            m_playback_started = false;
        }
        if (m_output)
            m_output->SetScheduledFrameCompletionCallback(nullptr);
        if (m_output && m_video_enabled)
        {
            m_output->DisableVideoOutput();
            m_video_enabled = false;
        }

        for (IDeckLinkMutableVideoFrame*& frame : m_frames)
            safe_release(frame);
        m_frames.clear();

        if (m_callback)
        {
            m_callback->Release();
            m_callback = nullptr;
        }
        safe_release(m_output);
        safe_release(m_decklink);
    }

    bool submit_bgra(const uint8_t* bytes, size_t byte_count, int width, int height, std::string& error)
    {
        if (!bytes)
        {
            error = "Frame buffer is empty.";
            return false;
        }
        if (width != m_width || height != m_height)
        {
            error = "Frame dimensions do not match DeckLink output mode.";
            return false;
        }
        size_t expected = static_cast<size_t>(m_width) * static_cast<size_t>(m_height) * 4U;
        if (byte_count < expected)
        {
            error = "Frame buffer is smaller than expected BGRA size.";
            return false;
        }

        std::lock_guard<std::mutex> guard(m_frame_mutex);
        std::memcpy(m_latest_frame.data(), bytes, expected);
        ++m_submitted_frames;
        return true;
    }

    bool schedule_next_frame()
    {
        if (!m_running || !m_output || m_frames.empty())
            return false;

        IDeckLinkMutableVideoFrame* frame = m_frames[m_next_frame];
        void* dest = nullptr;
        if (!frame || frame->GetBytes(&dest) != S_OK || !dest)
            return false;

        copy_latest_to_decklink_frame(dest);

        HRESULT result = m_output->ScheduleVideoFrame(
            frame,
            static_cast<BMDTimeValue>(m_frames_scheduled.load()) * m_frame_duration,
            m_frame_duration,
            m_time_scale
        );
        if (result != S_OK)
        {
            ++m_schedule_failures;
            return false;
        }

        ++m_frames_scheduled;
        m_next_frame = (m_next_frame + 1) % m_frames.size();
        return true;
    }

    void on_frame_completed(BMDOutputFrameCompletionResult result)
    {
        ++m_completed_frames;
        if (result == bmdOutputFrameDisplayedLate)
            ++m_late_frames;
        if (!schedule_next_frame())
            ++m_schedule_failures;
    }

    void mark_stopped()
    {
        m_running = false;
    }

    int width() const { return m_width; }
    int height() const { return m_height; }
    uint64_t submitted_frames() const { return m_submitted_frames.load(); }
    uint64_t scheduled_frames() const { return m_frames_scheduled.load(); }
    uint64_t completed_frames() const { return m_completed_frames.load(); }
    uint64_t late_frames() const { return m_late_frames.load(); }
    uint64_t schedule_failures() const { return m_schedule_failures.load(); }
    bool is_running() const { return m_running; }
    const std::string& device_name() const { return m_device_name; }

private:
    static std::string display_name(IDeckLink* decklink)
    {
        if (!decklink)
            return std::string();
        IDeckLinkProfileAttributes* attributes = nullptr;
        if (decklink->QueryInterface(IID_IDeckLinkProfileAttributes, reinterpret_cast<void**>(&attributes)) != S_OK || !attributes)
            return std::string();
        CFStringRef value = nullptr;
        std::string name;
        if (attributes->GetString(BMDDeckLinkDisplayName, &value) == S_OK && value)
        {
            name = cf_string_to_utf8(value);
            CFRelease(value);
        }
        attributes->Release();
        return name;
    }

    bool create_ring_frames_locked(std::string& error)
    {
        const int row_bytes = m_width * bytes_per_pixel();
        for (int index = 0; index < 4; ++index)
        {
            IDeckLinkMutableVideoFrame* frame = nullptr;
            if (m_output->CreateVideoFrame(
                    m_width,
                    m_height,
                    row_bytes,
                    m_pixel_format,
                    bmdFrameFlagDefault,
                    &frame
                ) != S_OK || !frame)
            {
                error = "Could not create DeckLink BGRA video frame.";
                return false;
            }
            m_frames.push_back(frame);
        }
        return true;
    }

    void resize_latest_locked(int width, int height)
    {
        std::lock_guard<std::mutex> guard(m_frame_mutex);
        m_latest_frame.assign(static_cast<size_t>(width) * static_cast<size_t>(height) * 4U, 0);
        for (size_t index = 3; index < m_latest_frame.size(); index += 4)
            m_latest_frame[index] = 255;
    }

    int bytes_per_pixel() const
    {
        return m_pixel_format == bmdFormat8BitYUV ? 2 : 4;
    }

    void copy_latest_to_decklink_frame(void* dest)
    {
        if (m_pixel_format == bmdFormat8BitBGRA)
        {
            std::lock_guard<std::mutex> guard(m_frame_mutex);
            std::memcpy(dest, m_latest_frame.data(), m_latest_frame.size());
            return;
        }

        uint8_t* out = static_cast<uint8_t*>(dest);
        std::lock_guard<std::mutex> guard(m_frame_mutex);
        for (int y = 0; y < m_height; ++y)
        {
            const uint8_t* row = m_latest_frame.data() + static_cast<size_t>(y) * static_cast<size_t>(m_width) * 4U;
            for (int x = 0; x < m_width; x += 2)
            {
                const uint8_t* p0 = row + static_cast<size_t>(x) * 4U;
                const uint8_t* p1 = row + static_cast<size_t>(std::min(x + 1, m_width - 1)) * 4U;

                double b0 = p0[0], g0 = p0[1], r0 = p0[2];
                double b1 = p1[0], g1 = p1[1], r1 = p1[2];
                uint8_t y0 = clamp_u8(16.0 + 0.183 * r0 + 0.614 * g0 + 0.062 * b0);
                uint8_t y1 = clamp_u8(16.0 + 0.183 * r1 + 0.614 * g1 + 0.062 * b1);
                uint8_t cb0 = clamp_u8(128.0 - 0.101 * r0 - 0.339 * g0 + 0.439 * b0);
                uint8_t cb1 = clamp_u8(128.0 - 0.101 * r1 - 0.339 * g1 + 0.439 * b1);
                uint8_t cr0 = clamp_u8(128.0 + 0.439 * r0 - 0.399 * g0 - 0.040 * b0);
                uint8_t cr1 = clamp_u8(128.0 + 0.439 * r1 - 0.399 * g1 - 0.040 * b1);

                *(out++) = static_cast<uint8_t>((static_cast<int>(cb0) + static_cast<int>(cb1)) / 2);
                *(out++) = y0;
                *(out++) = static_cast<uint8_t>((static_cast<int>(cr0) + static_cast<int>(cr1)) / 2);
                *(out++) = y1;
            }
        }
    }

    bool fail_locked()
    {
        m_running = false;
        if (m_output && m_playback_started)
        {
            m_output->StopScheduledPlayback(0, nullptr, m_time_scale);
            m_playback_started = false;
        }
        if (m_output)
            m_output->SetScheduledFrameCompletionCallback(nullptr);
        if (m_output && m_video_enabled)
        {
            m_output->DisableVideoOutput();
            m_video_enabled = false;
        }
        for (IDeckLinkMutableVideoFrame*& frame : m_frames)
            safe_release(frame);
        m_frames.clear();
        if (m_callback)
        {
            m_callback->Release();
            m_callback = nullptr;
        }
        safe_release(m_output);
        safe_release(m_decklink);
        return false;
    }

    std::string m_device_name;
    int m_requested_width = 1920;
    int m_requested_height = 1080;
    double m_requested_fps = 25.0;
    BMDDisplayMode m_mode = bmdModeHD1080p25;
    BMDVideoConnection m_connection = bmdVideoConnectionUnspecified;
    BMDPixelFormat m_pixel_format = bmdFormat8BitBGRA;

    IDeckLink* m_decklink = nullptr;
    IDeckLinkOutput* m_output = nullptr;
    OutputCallback* m_callback = nullptr;
    std::vector<IDeckLinkMutableVideoFrame*> m_frames;

    int m_width = 1920;
    int m_height = 1080;
    BMDTimeValue m_frame_duration = 1000;
    BMDTimeScale m_time_scale = 25000;
    std::atomic<uint64_t> m_frames_scheduled{0};
    size_t m_next_frame = 0;

    std::mutex m_state_mutex;
    std::mutex m_frame_mutex;
    std::vector<uint8_t> m_latest_frame;
    std::atomic<uint64_t> m_submitted_frames{0};
    std::atomic<uint64_t> m_completed_frames{0};
    std::atomic<uint64_t> m_late_frames{0};
    std::atomic<uint64_t> m_schedule_failures{0};
    bool m_running = false;
    bool m_video_enabled = false;
    bool m_playback_started = false;
};

HRESULT STDMETHODCALLTYPE OutputCallback::QueryInterface(REFIID iid, LPVOID* ppv)
{
    if (!ppv)
        return E_POINTER;
    *ppv = nullptr;

    CFUUIDBytes iunknown = CFUUIDGetUUIDBytes(IUnknownUUID);
    if (std::memcmp(&iid, &iunknown, sizeof(REFIID)) == 0 ||
        std::memcmp(&iid, &IID_IDeckLinkVideoOutputCallback, sizeof(REFIID)) == 0)
    {
        *ppv = static_cast<IDeckLinkVideoOutputCallback*>(this);
        AddRef();
        return S_OK;
    }
    return E_NOINTERFACE;
}

HRESULT STDMETHODCALLTYPE OutputCallback::ScheduledFrameCompleted(
    IDeckLinkVideoFrame* completedFrame,
    BMDOutputFrameCompletionResult result
)
{
    (void)completedFrame;
    if (m_owner)
        m_owner->on_frame_completed(result);
    return S_OK;
}

HRESULT STDMETHODCALLTYPE OutputCallback::ScheduledPlaybackHasStopped(void)
{
    if (m_owner)
        m_owner->mark_stopped();
    return S_OK;
}

typedef struct {
    PyObject_HEAD
    NativeDeckLinkOutput* output;
} PyDeckLinkOutput;

static void PyDeckLinkOutput_dealloc(PyDeckLinkOutput* self)
{
    delete self->output;
    self->output = nullptr;
    Py_TYPE(self)->tp_free(reinterpret_cast<PyObject*>(self));
}

static PyObject* PyDeckLinkOutput_start(PyDeckLinkOutput* self, PyObject* Py_UNUSED(args))
{
    if (!self->output)
    {
        PyErr_SetString(PyExc_RuntimeError, "DeckLink output has been released");
        return nullptr;
    }
    std::string error;
    if (!self->output->start(error))
    {
        PyErr_SetString(PyExc_RuntimeError, error.c_str());
        return nullptr;
    }
    Py_RETURN_TRUE;
}

static PyObject* PyDeckLinkOutput_stop(PyDeckLinkOutput* self, PyObject* Py_UNUSED(args))
{
    if (self->output)
        self->output->stop();
    Py_RETURN_NONE;
}

static PyObject* PyDeckLinkOutput_submit_bgra(PyDeckLinkOutput* self, PyObject* args)
{
    Py_buffer buffer;
    int width = 0;
    int height = 0;
    long long frame_index = 0;
    if (!PyArg_ParseTuple(args, "y*iiL", &buffer, &width, &height, &frame_index))
        return nullptr;

    (void)frame_index;
    std::string error;
    bool ok = self->output && self->output->submit_bgra(
        static_cast<const uint8_t*>(buffer.buf),
        static_cast<size_t>(buffer.len),
        width,
        height,
        error
    );
    PyBuffer_Release(&buffer);
    if (!ok)
    {
        PyErr_SetString(PyExc_RuntimeError, error.empty() ? "Failed to submit BGRA frame" : error.c_str());
        return nullptr;
    }
    Py_RETURN_TRUE;
}

static PyObject* PyDeckLinkOutput_submit_gpu_texture(PyDeckLinkOutput* self, PyObject* args)
{
    (void)self;
    (void)args;
    Py_RETURN_FALSE;
}

static PyObject* PyDeckLinkOutput_status(PyDeckLinkOutput* self, PyObject* Py_UNUSED(args))
{
    if (!self->output)
        return Py_BuildValue("{s:O}", "running", Py_False);
    return Py_BuildValue(
        "{s:O,s:i,s:i,s:K,s:K,s:K,s:K,s:K,s:s}",
        "running", self->output->is_running() ? Py_True : Py_False,
        "width", self->output->width(),
        "height", self->output->height(),
        "submitted_frames", static_cast<unsigned long long>(self->output->submitted_frames()),
        "scheduled_frames", static_cast<unsigned long long>(self->output->scheduled_frames()),
        "completed_frames", static_cast<unsigned long long>(self->output->completed_frames()),
        "late_frames", static_cast<unsigned long long>(self->output->late_frames()),
        "schedule_failures", static_cast<unsigned long long>(self->output->schedule_failures()),
        "device", self->output->device_name().c_str()
    );
}

static PyMethodDef PyDeckLinkOutput_methods[] = {
    {"start", reinterpret_cast<PyCFunction>(PyDeckLinkOutput_start), METH_NOARGS, "Start DeckLink scheduled playback."},
    {"stop", reinterpret_cast<PyCFunction>(PyDeckLinkOutput_stop), METH_NOARGS, "Stop DeckLink scheduled playback."},
    {"submit_bgra", reinterpret_cast<PyCFunction>(PyDeckLinkOutput_submit_bgra), METH_VARARGS, "Submit one BGRA frame."},
    {"submit_gpu_texture", reinterpret_cast<PyCFunction>(PyDeckLinkOutput_submit_gpu_texture), METH_VARARGS, "GPU texture submission placeholder."},
    {"status", reinterpret_cast<PyCFunction>(PyDeckLinkOutput_status), METH_NOARGS, "Return DeckLink output status."},
    {nullptr, nullptr, 0, nullptr},
};

static PyObject* PyDeckLinkOutput_get_supports_gpu_texture(PyDeckLinkOutput* self, void* closure)
{
    (void)self;
    (void)closure;
    Py_RETURN_FALSE;
}

static PyGetSetDef PyDeckLinkOutput_getset[] = {
    {"supports_gpu_texture", reinterpret_cast<getter>(PyDeckLinkOutput_get_supports_gpu_texture), nullptr, "Whether native GPU texture submission is active.", nullptr},
    {nullptr, nullptr, nullptr, nullptr, nullptr},
};

static PyTypeObject PyDeckLinkOutputType = {
    PyVarObject_HEAD_INIT(nullptr, 0)
};

static PyObject* list_devices(PyObject* self, PyObject* args)
{
    (void)self;
    (void)args;
    IDeckLinkIterator* iterator = CreateDeckLinkIteratorInstance();
    if (!iterator)
    {
        PyErr_SetString(PyExc_RuntimeError, "DeckLink API unavailable. Check Desktop Video driver installation.");
        return nullptr;
    }

    PyObject* devices = PyList_New(0);
    IDeckLink* decklink = nullptr;
    while (iterator->Next(&decklink) == S_OK)
    {
        IDeckLinkProfileAttributes* attributes = nullptr;
        if (decklink->QueryInterface(IID_IDeckLinkProfileAttributes, reinterpret_cast<void**>(&attributes)) == S_OK && attributes)
        {
            CFStringRef value = nullptr;
            if (attributes->GetString(BMDDeckLinkDisplayName, &value) == S_OK && value)
            {
                std::string name = cf_string_to_utf8(value);
                PyObject* py_name = PyUnicode_FromString(name.c_str());
                if (py_name)
                {
                    PyList_Append(devices, py_name);
                    Py_DECREF(py_name);
                }
                CFRelease(value);
            }
            attributes->Release();
        }
        decklink->Release();
    }
    iterator->Release();
    return devices;
}

static PyObject* create_output(PyObject* self, PyObject* args, PyObject* kwargs)
{
    (void)self;
    const char* device = "";
    int width = 1920;
    int height = 1080;
    double fps = 25.0;
    const char* mode = "1080p25";
    const char* connection = "";
    const char* pixel_format = "bgra";
    static const char* keywords[] = {"device", "width", "height", "fps", "mode", "connection", "pixel_format", nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|siidsss", const_cast<char**>(keywords), &device, &width, &height, &fps, &mode, &connection, &pixel_format))
        return nullptr;

    PyDeckLinkOutput* object = PyObject_New(PyDeckLinkOutput, &PyDeckLinkOutputType);
    if (!object)
        return nullptr;
    object->output = new NativeDeckLinkOutput(
        device ? device : "",
        width,
        height,
        fps,
        mode ? mode : "1080p25",
        connection ? connection : "",
        pixel_format ? pixel_format : "bgra"
    );
    return reinterpret_cast<PyObject*>(object);
}

static PyMethodDef module_methods[] = {
    {"list_devices", reinterpret_cast<PyCFunction>(list_devices), METH_NOARGS, "List installed DeckLink devices."},
    {"create_output", reinterpret_cast<PyCFunction>(create_output), METH_VARARGS | METH_KEYWORDS, "Create a DeckLink output object."},
    {nullptr, nullptr, 0, nullptr},
};

static struct PyModuleDef module_def = {
    PyModuleDef_HEAD_INIT,
    "topos_decklink",
    "Native Blackmagic DeckLink monitor-output bridge.",
    -1,
    module_methods,
};

}  // namespace

PyMODINIT_FUNC PyInit_topos_decklink(void)
{
    PyDeckLinkOutputType.tp_name = "topos_decklink.Output";
    PyDeckLinkOutputType.tp_basicsize = sizeof(PyDeckLinkOutput);
    PyDeckLinkOutputType.tp_flags = Py_TPFLAGS_DEFAULT;
    PyDeckLinkOutputType.tp_doc = "DeckLink scheduled playback output.";
    PyDeckLinkOutputType.tp_dealloc = reinterpret_cast<destructor>(PyDeckLinkOutput_dealloc);
    PyDeckLinkOutputType.tp_methods = PyDeckLinkOutput_methods;
    PyDeckLinkOutputType.tp_getset = PyDeckLinkOutput_getset;
    PyDeckLinkOutputType.tp_new = PyType_GenericNew;
    if (PyType_Ready(&PyDeckLinkOutputType) < 0)
        return nullptr;

    PyObject* module = PyModule_Create(&module_def);
    if (!module)
        return nullptr;
    Py_INCREF(&PyDeckLinkOutputType);
    if (PyModule_AddObject(module, "Output", reinterpret_cast<PyObject*>(&PyDeckLinkOutputType)) < 0)
    {
        Py_DECREF(&PyDeckLinkOutputType);
        Py_DECREF(module);
        return nullptr;
    }
    PyModule_AddStringConstant(module, "__version__", "0.1.0");
    return module;
}
