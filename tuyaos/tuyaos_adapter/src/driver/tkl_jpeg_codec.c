/**
 * @file tkl_jpeg_codec.c
 * @brief JIELI AC792N Hardware JPEG Decoder Adapter for TuyaOpen
 *
 * Accelerates JPEG frame decompression using AC792N's onboard JPEG hardware
 * engine (jpeg_decode_one_image) + assembly-optimized YUV to RGB565 converter
 * (yuv420p_quto_rgb565).
 */

#include "tkl_jpeg_codec.h"
#include "tkl_output.h"
#include "tuya_error_code.h"

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdlib.h>

#if defined(JIELI_SELECTED_CHIP_WL83)

#ifndef __FS_H__
#define __FS_H__
#endif

#include "asm/jpeg_codec.h"
#include "yuv_to_rgb.h"
#include "yuv_soft_scalling.h"

extern uint32_t timer_get_ms(void);

static uint8_t *s_yuv_cache      = NULL;
static uint32_t s_yuv_cache_size = 0;
static bool     s_is_inited      = false;
static uint32_t s_dec_count      = 0;

OPERATE_RET tkl_jpeg_codec_init(void)
{
    if (s_is_inited) {
        return OPRT_OK;
    }

    jpeg_codec_init();

    /* Pre-allocate YUV buffer for up to 800x480 YUV420 (800*480*1.5 = 576000 bytes) */
    s_yuv_cache_size = 800 * 480 * 2;
    s_yuv_cache      = (uint8_t *)malloc(s_yuv_cache_size);
    if (!s_yuv_cache) {
        tkl_log_output("[JPEG-HW] WARNING: pre-allocating YUV cache failed\r\n");
        s_yuv_cache_size = 0;
    } else {
        tkl_log_output("[JPEG-HW] Hardware JPEG decoder initialized, YUV cache=%u bytes\r\n",
                       (unsigned int)s_yuv_cache_size);
    }

    s_is_inited = true;
    return OPRT_OK;
}

OPERATE_RET tkl_jpeg_codec_deinit(void)
{
    if (!s_is_inited) {
        return OPRT_OK;
    }

    if (s_yuv_cache) {
        free(s_yuv_cache);
        s_yuv_cache      = NULL;
        s_yuv_cache_size = 0;
    }

    s_is_inited = false;
    return OPRT_OK;
}

OPERATE_RET tkl_jpeg_codec_img_info_get(uint8_t *jpeg_buf, uint32_t jpeg_size, TKL_JPEG_CODEC_INFO_T *jpeg_info)
{
    if (!jpeg_buf || !jpeg_info || jpeg_size < 4) {
        return OPRT_INVALID_PARM;
    }

    struct jpeg_image_info info = {0};
    info.input.data.buf         = jpeg_buf;
    info.input.data.len         = jpeg_size;

    int err = jpeg_decode_image_info(&info);
    if (err != 0) {
        return OPRT_COM_ERROR;
    }

    jpeg_info->in_size    = jpeg_size;
    jpeg_info->out_width  = info.width;
    jpeg_info->out_height = info.height;
    jpeg_info->out_fmt    = TUYA_FRAME_FMT_JPEG;

    return OPRT_OK;
}

OPERATE_RET tkl_jpeg_codec_convert(uint8_t *src_buf, uint8_t *dst_buf, TKL_JPEG_CODEC_INFO_T *jpeg_codec_info,
                                   JPEG_DEC_OUT_FMT out_fmt)
{
    if (!src_buf || !dst_buf || !jpeg_codec_info) {
        return OPRT_INVALID_PARM;
    }

    if (!s_is_inited) {
        tkl_jpeg_codec_init();
    }

    uint32_t t_start = timer_get_ms();

    struct jpeg_image_info info = {0};
    info.input.data.buf         = src_buf;
    info.input.data.len         = jpeg_codec_info->in_size;

    int err = jpeg_decode_image_info(&info);
    if (err != 0) {
        return OPRT_COM_ERROR;
    }

    uint16_t width      = info.width;
    uint16_t height     = info.height;
    uint32_t pix        = (uint32_t)width * height;
    uint32_t needed_len = pix + pix / 2; /* YUV420 size */

    if (s_yuv_cache == NULL || s_yuv_cache_size < needed_len) {
        if (s_yuv_cache) {
            free(s_yuv_cache);
        }
        s_yuv_cache_size = needed_len + 4096;
        s_yuv_cache      = (uint8_t *)malloc(s_yuv_cache_size);
        if (!s_yuv_cache) {
            s_yuv_cache_size = 0;
            return OPRT_MALLOC_FAILED;
        }
    }

    struct jpeg_decode_req req = {0};
    req.input_type             = JPEG_INPUT_TYPE_DATA;
    req.input.data.buf         = src_buf;
    req.input.data.len         = jpeg_codec_info->in_size;
    req.buf_y                  = s_yuv_cache;
    req.buf_u                  = s_yuv_cache + pix;
    req.buf_v                  = s_yuv_cache + pix + (pix / 4);
    req.buf_width              = width;
    req.buf_height             = height;
    req.out_width              = width;
    req.out_height             = height;
    req.output_type            = JPEG_DECODE_TYPE_YUV420; /* 0: force YUV420 hardware decode */
    req.bits_mode              = BITS_MODE_UNCACHE;

    /* Try JPEG_DEV_ID_1 first because DEV_0 is occupied by camera video stream encoder */
    err = jpeg_decode_one_image(&req, JPEG_DEV_ID_1);
    if (err != 0) {
        err = jpeg_decode_one_image(&req, JPEG_DEV_ID_0);
    }
    if (err != 0) {
        tkl_log_output("[JPEG-HW] jpeg_decode_one_image err=%d (w=%u, h=%u, in_len=%u)\r\n", err, width, height,
                       (unsigned int)jpeg_codec_info->in_size);
        return OPRT_COM_ERROR;
    }

    uint32_t t_hw_dec = timer_get_ms() - t_start;

    if (out_fmt == JPEG_DEC_OUT_RGB565) {
        yuv420p_quto_rgb565(s_yuv_cache, dst_buf, width, height, 0 /* little endian */);
    } else if (out_fmt == JPEG_DEC_OUT_YUV422) {
        memcpy(dst_buf, s_yuv_cache, needed_len);
    } else {
        return OPRT_NOT_SUPPORTED;
    }

    uint32_t t_total = timer_get_ms() - t_start;
    s_dec_count++;
    if (s_dec_count <= 5 || s_dec_count % 30 == 0) {
        tkl_log_output("[JPEG-HW] frame #%u hard-decoded %ux%u in %ums (hw=%ums, yuv2rgb=%ums)\r\n",
                       (unsigned int)s_dec_count, (unsigned int)width, (unsigned int)height, (unsigned int)t_total,
                       (unsigned int)t_hw_dec, (unsigned int)(t_total - t_hw_dec));
    }

    jpeg_codec_info->out_width  = width;
    jpeg_codec_info->out_height = height;

    return OPRT_OK;
}

#else

OPERATE_RET tkl_jpeg_codec_init(void)
{
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_jpeg_codec_deinit(void)
{
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_jpeg_codec_img_info_get(uint8_t *jpeg_buf, uint32_t jpeg_size, TKL_JPEG_CODEC_INFO_T *jpeg_info)
{
    (void)jpeg_buf;
    (void)jpeg_size;
    (void)jpeg_info;
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_jpeg_codec_convert(uint8_t *src_buf, uint8_t *dst_buf, TKL_JPEG_CODEC_INFO_T *jpeg_codec_info,
                                   JPEG_DEC_OUT_FMT out_fmt)
{
    (void)src_buf;
    (void)dst_buf;
    (void)jpeg_codec_info;
    (void)out_fmt;
    return OPRT_NOT_SUPPORTED;
}

#endif
