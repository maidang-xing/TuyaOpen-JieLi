/**
 * @file tkl_csi2.c
 * @brief JIELI WL83 MIPI CSI2 camera driver adapter for TuyaOpen
 *
 * Implements the TuyaOpen tkl_csi2 interface on top of Jieli's native
 * video_server (video0.1 channel, MIPI CSI2 camera path).
 */

#include "tkl_csi2.h"
#include "tkl_output.h"
#include "tuya_error_code.h"

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdlib.h>

#if defined(JIELI_SELECTED_CHIP_WL83)

#ifndef __FS_H__
#define __FS_H__ /* Prevent SDK's fs.h from conflicting with newlib stdio.h FILE typedef */
#endif

#include "server/server_core.h"
#include "server/video_server.h"
#include "device/device.h"

#define JL_000DC   0x63643030
#define JPEG_HEAD  0xE0FFD8FF
#define JPEG_HEAD1 0xC0FFD8FF

#define JL_CSI2_BUF_SIZE (256 * 1024)

static struct server *s_video_rec     = NULL;
static uint8_t       *s_video_buf     = NULL;
static uint32_t       s_video_buf_len = 0;
static TUYA_DVP_CFG_T s_csi_cfg;
static bool           s_is_inited  = false;
static bool           s_is_running = false;
static uint32_t       s_frame_id   = 0;
volatile int          g_csi2_open_status = 0;
volatile int          g_csi2_rec_err = 0;
volatile uint32_t     g_csi2_packet_count = 0;

static int s_csi2_packet_cb(u8 type, u8 *buf, u32 size)
{
    g_csi2_packet_count++;
    uint32_t s_pkt_cnt = g_csi2_packet_count;
    if (s_pkt_cnt <= 5 || s_pkt_cnt % 30 == 0) {
        tkl_log_output("[CSI2-CB] packet #%u: type=%d, size=%u\r\n", s_pkt_cnt, type, size);
    }
    if (!s_is_running || !buf || size < 8) {
        return 0;
    }

    uint32_t *head = (uint32_t *)buf;
    if (type == VIDEO_REC_JPEG_TYPE_VIDEO || type == 2) {
        if ((*head == JL_000DC && *(head + 2) == JPEG_HEAD) || *(head + 2) == JPEG_HEAD ||
            (*head == JL_000DC && *(head + 2) == JPEG_HEAD1) || *(head + 2) == JPEG_HEAD1) {
            buf += 8;
            size -= 8;
        }

        if (size >= 4 && buf[0] == 0xFF && buf[1] == 0xD8) {
            if (s_csi_cfg.inter_cfg.assign_cb && s_csi_cfg.inter_cfg.post_cb) {
                TUYA_DVP_FRAME_MANAGE_T *csi_frame =
                    s_csi_cfg.inter_cfg.assign_cb(TUYA_FRAME_FMT_JPEG, s_csi_cfg.inter_cfg.cb_param);
                if (csi_frame != NULL) {
                    s_frame_id++;
                    csi_frame->frame_id          = s_frame_id;
                    csi_frame->width             = s_csi_cfg.width;
                    csi_frame->height            = s_csi_cfg.height;
                    csi_frame->is_i_frame        = 1;
                    csi_frame->is_frame_complete = 1;
                    csi_frame->frame_fmt         = TUYA_FRAME_FMT_JPEG;
                    if (csi_frame->data != NULL && size <= csi_frame->data_len) {
                        memcpy(csi_frame->data, buf, size);
                        csi_frame->data_len        = size;
                        csi_frame->total_frame_len = size;
                        s_csi_cfg.inter_cfg.post_cb(csi_frame, s_csi_cfg.inter_cfg.cb_param);
                    }
                }
            }
        }
    }

    return 0;
}

OPERATE_RET tkl_csi2_init(TUYA_DVP_CFG_T *csi2_cfg)
{
    if (csi2_cfg == NULL) {
        return OPRT_INVALID_PARM;
    }

    memcpy(&s_csi_cfg, csi2_cfg, sizeof(TUYA_DVP_CFG_T));
    s_is_inited = true;
    return OPRT_OK;
}

OPERATE_RET tkl_csi2_deinit(void)
{
    tkl_csi2_stop();
    s_is_inited = false;
    memset(&s_csi_cfg, 0, sizeof(s_csi_cfg));
    return OPRT_OK;
}

OPERATE_RET tkl_csi2_start(void)
{
    if (!s_is_inited) {
        return OPRT_COM_ERROR;
    }
    if (s_is_running) {
        return OPRT_OK;
    }

    tkl_log_output("[CSI2] opening video_server video0.1...\r\n");
    s_video_rec = server_open("video_server", "video0.1");
    if (!s_video_rec) {
        g_csi2_open_status = 1;
        tkl_log_output("[CSI2] ERROR: server_open video0.1 failed!\r\n");
        return OPRT_COM_ERROR;
    }
    tkl_log_output("[CSI2] server_open video0.1 OK, allocating buffer...\r\n");

    s_video_buf_len = JL_CSI2_BUF_SIZE;
    s_video_buf     = (uint8_t *)malloc(s_video_buf_len);
    if (!s_video_buf) {
        g_csi2_open_status = 2;
        server_close(s_video_rec);
        s_video_rec = NULL;
        return OPRT_MALLOC_FAILED;
    }

    uint16_t w = s_csi_cfg.width ? s_csi_cfg.width : 640;
    uint16_t h = s_csi_cfg.height ? s_csi_cfg.height : 480;

    union video_req req;
    memset(&req, 0, sizeof(req));
    req.rec.camera_type = VIDEO_CAMERA_NORMAL;
    req.rec.channel     = 1;
    req.rec.width       = w;
    req.rec.src_w       = w;
    req.rec.height      = h;
    req.rec.src_h       = h;
    req.rec.state       = VIDEO_STATE_START;
    req.rec.format      = USER_VIDEO_FMT_AVI;
    req.rec.quality     = VIDEO_LOW_Q;
    req.rec.fps         = s_csi_cfg.fps ? (uint8_t)s_csi_cfg.fps : 15;
    req.rec.real_fps    = 15;
    req.rec.abr_kbps    = 3000;
    req.rec.cycle_time  = 60;
    req.rec.enable_dri  = 0;
    req.rec.online      = 1;
    req.rec.buf         = s_video_buf;
    req.rec.buf_len     = s_video_buf_len;

    int err = server_request(s_video_rec, VIDEO_REQ_REC, &req);
    g_csi2_rec_err = err;
    if (err != 0) {
        g_csi2_open_status = 3;
        tkl_log_output("[CSI2] ERROR: VIDEO_REQ_REC failed: %d\r\n", err);
        server_close(s_video_rec);
        s_video_rec = NULL;
        free(s_video_buf);
        s_video_buf = NULL;
        return OPRT_COM_ERROR;
    }
    tkl_log_output("[CSI2] VIDEO_REQ_REC OK!\r\n");

    req.rec.packet_cb = s_csi2_packet_cb;
    err               = server_request(s_video_rec, VIDEO_REQ_SET_PACKET_CALLBACK, &req);
    if (err != 0) {
        g_csi2_open_status = 4;
        memset(&req, 0, sizeof(req));
        req.rec.channel = 1;
        req.rec.state   = VIDEO_STATE_STOP;
        server_request(s_video_rec, VIDEO_REQ_REC, &req);
        server_close(s_video_rec);
        s_video_rec = NULL;
        free(s_video_buf);
        s_video_buf = NULL;
        return OPRT_COM_ERROR;
    }

    g_csi2_open_status = 10;
    s_is_running = true;
    tkl_log_output("[CSI2] CSI2 started successfully, status=10\r\n");
    return OPRT_OK;
}

OPERATE_RET tkl_csi2_stop(void)
{
    if (!s_is_running) {
        return OPRT_OK;
    }

    s_is_running = false;

    if (s_video_rec != NULL) {
        union video_req req;
        memset(&req, 0, sizeof(req));
        req.rec.channel = 1;
        req.rec.state   = VIDEO_STATE_STOP;
        server_request(s_video_rec, VIDEO_REQ_REC, &req);
        server_close(s_video_rec);
        s_video_rec = NULL;
    }

    if (s_video_buf != NULL) {
        free(s_video_buf);
        s_video_buf     = NULL;
        s_video_buf_len = 0;
    }

    return OPRT_OK;
}

#else

OPERATE_RET tkl_csi2_init(TUYA_DVP_CFG_T *csi2_cfg)
{
    (void)csi2_cfg;
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_csi2_deinit(void)
{
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_csi2_start(void)
{
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_csi2_stop(void)
{
    return OPRT_NOT_SUPPORTED;
}

#endif
