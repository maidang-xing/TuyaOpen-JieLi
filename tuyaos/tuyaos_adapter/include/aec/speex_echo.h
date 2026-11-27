/*
 * Compat declarations for the TAL archive's speexdsp echo canceller.
 *
 * The archive exports speex_echo_state_init/cancellation/destroy (speexdsp's
 * standard API); the shipped package only documents the higher-level
 * audio_subsys_speexdsp_wrap_* wrapper, which this file does not use.
 */
#ifndef __JIELI_SPEEX_ECHO_H__
#define __JIELI_SPEEX_ECHO_H__

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct SpeexEchoState_ SpeexEchoState;

/* frame_size in samples, filter_length in samples of echo tail to cancel. */
SpeexEchoState *speex_echo_state_init(int frame_size, int filter_length);
void            speex_echo_state_reset(SpeexEchoState *st);
void            speex_echo_state_destroy(SpeexEchoState *st);
/* rec and play are frame_size samples each; out receives the cleaned rec. */
void            speex_echo_cancellation(SpeexEchoState *st, const int16_t *rec, const int16_t *play, int16_t *out);

#ifdef __cplusplus
}
#endif

#endif /* __JIELI_SPEEX_ECHO_H__ */
