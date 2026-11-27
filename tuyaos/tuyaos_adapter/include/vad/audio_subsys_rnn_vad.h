/*
 * Compat declarations for the TAL archive's RNN VAD engine (rnn_vad_*).
 *
 * Same archive as the KWS engine (kws/libaudio_subsys/audio_subsys.a); the
 * shipped VAD headers describe an audio_subsys_manager API that this archive
 * does not export. The layout below is the one the adapter linked against.
 */
#ifndef __TAL_AUDIO_SUBSYS_RNN_VAD_H__
#define __TAL_AUDIO_SUBSYS_RNN_VAD_H__

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct rnn_vad_param_in {
    float min_speech_len;      /* ms, default 200 */
    float max_speech_interval; /* ms, default 1000 */
} RNN_VAD_PARAM_T;

void  *rnn_vad_create(void);
int    rnn_vad_init(RNN_VAD_PARAM_T *param_in, void *obj);
float  rnn_vad_process(void *obj, short *x);
void   rnn_vad_destroy(void *obj);
void   rnn_vad_start(void *obj);
void   rnn_vad_stop(void *obj);
void   rnn_vad_set_callback(void *obj, float threshold);

#ifdef __cplusplus
}
#endif

#endif /* __TAL_AUDIO_SUBSYS_RNN_VAD_H__ */
