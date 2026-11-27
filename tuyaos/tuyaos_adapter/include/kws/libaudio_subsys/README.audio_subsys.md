# TAL audio subsystem library (user-supplied)

`audio_subsys.a` comes from the customer's JieLi TAL audio package
(`jieli_lib_v2.zip`, 2026-10). The archive is pi32v2 LLVM bitcode
(clang 4.0.1) and is linked by the JIELI SDK's LTO wrapper.

The package also shipped 27 headers (`audio_subsys_manager.h`,
`audio_subsys_kws_tflm*.h`, `audio_subsys_module.h`, AEC/AGC/NS/filter/
resample/template/webrtc wrappers, ...). They are not kept: the adapter only
uses two small declarations of the exported symbols, `audio_subsys_kws.h`
here and `include/vad/audio_subsys_rnn_vad.h`, and most of those headers
describe APIs this archive does not even export.

What the archive actually exports (verified with llvm-nm):

- `rnn_vad_create/init/process/destroy/start/stop/set_callback`
- `keyword_tflite_config_init/get_mem_size/create/add_keyword/forward_pcm/destroy`
- `audio_subsys_speexdsp_wrap_*`
- C++ `wekws::CtcBeamSearch` / `CtcGreedyKws` / `KeywordSpottingTflite`

The high-level `audio_subsys_manager_*` / `audio_subsys_kws_tflm_*` /
`audio_subsys_vad_*` declarations are NOT exported by this archive; the TKL
adapter uses the lower-level engines above.

The archive embeds the nhty ("你好涂鸦") TFLite model
(`g_fsmn_epoch17_nhty_hty_int8_model_data`, 136,392 bytes). It carries no
token/word table, so `keyword_tflite_add_keyword()` still needs token IDs
from the training/export side.

## Repacked

The supplied archive shipped 34 trained models (~4.7 MB of its 6.3 MB, one
member each) plus TFLite Micro test helpers, none of which this image uses.
The committed archive is repacked without those members (262 -> 227 members,
6.3 MB -> 2.3 MB, `llvm-ar x` / `rms` / `rcs`). The wake word model now lives
only in `src/port/tal_nhty_model.c`, which the linker resolves the engine's
default model symbol to. Start from the full archive if another wake word is
added.
