/* Acoustic echo cancellation on the TAL archive's speexdsp engine.
 *
 * The capture path calls tuya_audio_aec_process() with the mic PCM and the
 * playback path calls tuya_audio_aec_ref_write() with what goes to the DAC.
 * Frames are cancelled in speex's 10 ms units (160 samples at 16 kHz) and the
 * adaptive filter (1024 taps, ~64 ms) learns the DAC buffer and acoustic
 * delay itself, so no manual alignment is needed.
 *
 * Until playback has actually queued something the mic is passed through
 * untouched: an all-zero reference only trains the filter on nothing and can
 * attenuate the signal the KWS front-end depends on.
 */
#include "speex_echo.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "tkl_mutex.h"
#include "tkl_system.h"

#define TAL_AEC_FRAME_SAMPLES 160   /* 10 ms at 16 kHz, speex's unit */
#define TAL_AEC_FILTER_LEN    1024  /* taps, ~64 ms of delay headroom */

/* 256 ms of playback history; the reference is consumed in order so a stalled
 * writer costs zeros rather than repetition. */
#define TAL_AEC_REF_SAMPLES 4096

/* Playback is considered over when no reference PCM has been queued for this
 * long. The ring alone is not enough: the consumer runs on the capture path,
 * so when capture stops (wake hand-off, session setup) unconsumed reference
 * samples stay in the ring forever and "playing" would never clear, which
 * makes the KWS worker skip every inference from then on. */
#define TAL_AEC_PLAYING_IDLE_MS 300

static SpeexEchoState  *s_aec;
static int16_t          s_ref[TAL_AEC_REF_SAMPLES];
static uint32_t         s_ref_write;
static uint32_t         s_ref_read;
static uint32_t         s_ref_write_ms;
static TKL_MUTEX_HANDLE s_lock;
static uint8_t          s_create_failed;

static int __aec_lock(void)
{
    if (s_lock == NULL && tkl_mutex_create_init(&s_lock) != OPRT_OK) {
        return -1;
    }
    return tkl_mutex_lock(s_lock) == OPRT_OK ? 0 : -1;
}

static void __aec_unlock(void)
{
    if (s_lock != NULL) {
        (void)tkl_mutex_unlock(s_lock);
    }
}

/* Caller holds the lock. */
static int __aec_ensure(void)
{
    if (s_aec != NULL) {
        return 0;
    }
    if (s_create_failed) {
        return -1;
    }
    s_aec = speex_echo_state_init(TAL_AEC_FRAME_SAMPLES, TAL_AEC_FILTER_LEN);
    if (s_aec == NULL) {
        /* One attempt only: a failed create is a build/link problem, and
         * retrying on every captured frame would just burn CPU. */
        s_create_failed = 1;
        printf("[SPEEX_AEC] engine create failed\n");
        return -1;
    }
    printf("[SPEEX_AEC] ready frame=%d filter=%d\n", TAL_AEC_FRAME_SAMPLES, TAL_AEC_FILTER_LEN);
    return 0;
}

/* True while playback PCM is queued in the reference ring, i.e. the speaker is
 * (or just was) producing sound. The KWS worker uses it to skip inference so
 * TTS decode, networking and the UI keep the CPU, and so the device does not
 * trigger its own wake word from the speaker. */
int tuya_audio_aec_is_playing(void)
{
    int playing;

    if (__aec_lock() != 0) {
        return 0;
    }
    playing = (s_ref_write != s_ref_read) &&
              ((uint32_t)tkl_system_get_millisecond() - s_ref_write_ms) < TAL_AEC_PLAYING_IDLE_MS;
    __aec_unlock();
    return playing;
}

void tuya_audio_aec_ref_write(const int16_t *pcm, int samples)
{
    uint32_t first;

    if (pcm == NULL || samples <= 0 || __aec_lock() != 0) {
        return;
    }
    if (__aec_ensure() == 0 && samples <= TAL_AEC_REF_SAMPLES) {
        uint32_t used = s_ref_write - s_ref_read;

        /* Keep the window near the newest audio: if playback ran ahead of
         * capture, drop the oldest samples instead of replaying stale ones. */
        if (used + (uint32_t)samples > TAL_AEC_REF_SAMPLES) {
            s_ref_read = s_ref_write - (uint32_t)(TAL_AEC_REF_SAMPLES - samples);
        }
        first = TAL_AEC_REF_SAMPLES - (s_ref_write % TAL_AEC_REF_SAMPLES);
        if (first > (uint32_t)samples) {
            first = (uint32_t)samples;
        }
        memcpy(s_ref + (s_ref_write % TAL_AEC_REF_SAMPLES), pcm, first * sizeof(int16_t));
        if ((uint32_t)samples > first) {
            memcpy(s_ref, pcm + first, ((uint32_t)samples - first) * sizeof(int16_t));
        }
        s_ref_write += (uint32_t)samples;
        s_ref_write_ms = (uint32_t)tkl_system_get_millisecond();
    }
    __aec_unlock();
}

/* Cancels in place. Samples past the last whole 10 ms frame are left as is. */
void tuya_audio_aec_process(int16_t *mic, int samples)
{
    int offset = 0;

    if (mic == NULL || samples < TAL_AEC_FRAME_SAMPLES || __aec_lock() != 0) {
        return;
    }
    if (__aec_ensure() != 0) {
        __aec_unlock();
        return;
    }
    if (s_ref_write == s_ref_read) {
        /* Nothing has been played yet: keep the mic bit-exact. */
        __aec_unlock();
        return;
    }
    while (samples - offset >= TAL_AEC_FRAME_SAMPLES) {
        int16_t play[TAL_AEC_FRAME_SAMPLES];
        uint32_t first;

        if (s_ref_write - s_ref_read >= TAL_AEC_FRAME_SAMPLES) {
            first = TAL_AEC_REF_SAMPLES - (s_ref_read % TAL_AEC_REF_SAMPLES);
            if (first > TAL_AEC_FRAME_SAMPLES) {
                first = TAL_AEC_FRAME_SAMPLES;
            }
            memcpy(play, s_ref + (s_ref_read % TAL_AEC_REF_SAMPLES), first * sizeof(int16_t));
            if (first < TAL_AEC_FRAME_SAMPLES) {
                memcpy(play + first, s_ref, (TAL_AEC_FRAME_SAMPLES - first) * sizeof(int16_t));
            }
            s_ref_read += TAL_AEC_FRAME_SAMPLES;
        } else {
            memset(play, 0, sizeof(play));
        }
        speex_echo_cancellation(s_aec, mic + offset, play, mic + offset);
        offset += TAL_AEC_FRAME_SAMPLES;
    }
    __aec_unlock();
}
