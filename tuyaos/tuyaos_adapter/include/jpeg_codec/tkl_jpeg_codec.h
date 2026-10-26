/**
 * @file tkl_jpeg_codec.h
 * @brief Common process - adapter the jpeg codec api for JIELI platform
 * @version 0.1
 * @copyright Copyright (c) 2021-2026 Tuya Inc. All Rights Reserved.
 */

#ifndef __TKL_JPEG_CODEC_H__
#define __TKL_JPEG_CODEC_H__

#include "tuya_cloud_types.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    JPEG_DEC_OUT_YUV422 = 0,
    JPEG_DEC_OUT_RGB565,
    JPEG_DEC_OUT_RGB888,
} JPEG_DEC_OUT_FMT;

typedef struct {
    TUYA_FRAME_FMT_E out_fmt;
    uint16_t         out_width;
    uint16_t         out_height;
    uint32_t         in_size;
} TKL_JPEG_CODEC_INFO_T;

/**
 * @brief jpeg codec init
 *
 * @return OPRT_OK on success. Others on error
 */
OPERATE_RET tkl_jpeg_codec_init(void);

/**
 * @brief jpeg codec deinit
 *
 * @return OPRT_OK on success. Others on error
 */
OPERATE_RET tkl_jpeg_codec_deinit(void);

/**
 * @brief get jpeg image info
 *
 * @param[in]  jpeg_buf: jpeg data buffer
 * @param[in]  jpeg_size: jpeg data size in bytes
 * @param[out] jpeg_info: returned image info
 * @return OPRT_OK on success. Others on error
 */
OPERATE_RET tkl_jpeg_codec_img_info_get(uint8_t *jpeg_buf, uint32_t jpeg_size, TKL_JPEG_CODEC_INFO_T *jpeg_info);

/**
 * @brief hardware/software accelerated jpeg convert to target format
 *
 * @param[in]  src_buf: jpeg source buffer
 * @param[out] dst_buf: target frame buffer
 * @param[in]  jpeg_codec_info: image info with size/width/height
 * @param[in]  out_fmt: target pixel format
 * @return OPRT_OK on success. Others on error
 */
OPERATE_RET tkl_jpeg_codec_convert(uint8_t *src_buf, uint8_t *dst_buf, TKL_JPEG_CODEC_INFO_T *jpeg_codec_info,
                                   JPEG_DEC_OUT_FMT out_fmt);

#ifdef __cplusplus
}
#endif

#endif /* __TKL_JPEG_CODEC_H__ */
