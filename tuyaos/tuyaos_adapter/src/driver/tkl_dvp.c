/**
 * @file tkl_dvp.c
 * @brief Jieli WL82 camera TKL: vendor video pipeline behind the Tuya DVP TDD.
 *
 * Two board paths share one TKL contract, and the board Kconfig choice picks
 * exactly one of them:
 *
 *   JIELI_CAMERA_PATH_DVP  J1 24-pin DVP  -> dev_open("video0") + VIDIOC overlay
 *   JIELI_CAMERA_PATH_SPI  J4 12-pin SPI  -> dev_open("video1.0") for the sensor
 *                                            + dev_open("spi2") for the data
 *
 * The board has both connectors, but PA2/PA3/PA4 and PH0-PH3 are wired to
 * both, so they cannot run together. The sensor is probed, configured and
 * reset by the vendor drivers; this file only moves frames.
 *
 * @copyright Copyright (c) 2021-2026 Tuya Inc. All Rights Reserved.
 */

#include "tkl_dvp.h"

#include <stdio.h>
#include <string.h>

#include "tkl_memory.h"
#include "tkl_system.h"
#include "tkl_thread.h"

#include "device.h"
#include "device/gpio.h"
#include "video/video.h"
#include "video/video_ioctl.h"
#include "video/camera.h"

#if defined(JIELI_SELECTED_CHIP_WL82) && ((defined(JIELI_CAMERA_PATH_DVP) && (JIELI_CAMERA_PATH_DVP == 1)) ||          \
                                          (defined(JIELI_CAMERA_PATH_SPI) && (JIELI_CAMERA_PATH_SPI == 1)))

/* The vendor make receives these from the staging overlay; the Tuya-side
 * compile of the adapter sees the board Kconfig through tuya_kconfig.h. */
#if !defined(JIELI_CAMERA_WIDTH)
#if defined(JIELI_BOARD_CAMERA_WIDTH)
#define JIELI_CAMERA_WIDTH (JIELI_BOARD_CAMERA_WIDTH)
#else
#define JIELI_CAMERA_WIDTH 640
#endif
#endif
#if !defined(JIELI_CAMERA_HEIGHT)
#if defined(JIELI_BOARD_CAMERA_HEIGHT)
#define JIELI_CAMERA_HEIGHT (JIELI_BOARD_CAMERA_HEIGHT)
#else
#define JIELI_CAMERA_HEIGHT 480
#endif
#endif

#define CAM_TAG "camera"

static TUYA_DVP_CFG_T    s_cfg;
static TKL_THREAD_HANDLE s_thread       = NULL;
static volatile int      s_running      = 0;
static volatile int      s_stopped      = 1;
static void             *s_video_dev    = NULL;
static u8               *s_yuv_buf      = NULL;
static u32               s_yuv_buf_size = 0;
static uint32_t          s_frame_id     = 0;
static u32               s_frame_count  = 0;

static void camera_deliver_frame(u8 *data, u32 len, u16 width, u16 height, TUYA_FRAME_FMT_E fmt)
{
    TUYA_DVP_FRAME_MANAGE_T *frame;

    if (s_cfg.inter_cfg.assign_cb == NULL || s_cfg.inter_cfg.post_cb == NULL) {
        return;
    }
    if (data == NULL || len == 0) {
        return;
    }

    frame = s_cfg.inter_cfg.assign_cb(fmt, s_cfg.inter_cfg.cb_param);
    if (frame == NULL || frame->data == NULL) {
        return;
    }
    if (len > frame->data_len) {
        len = frame->data_len;
    }

    memcpy(frame->data, data, len);
    frame->frame_fmt         = fmt;
    frame->frame_id          = ++s_frame_id;
    frame->width             = width;
    frame->height            = height;
    frame->data_len          = len;
    frame->total_frame_len   = len;
    frame->is_frame_complete = 1;
    frame->is_i_frame        = 1;

    s_cfg.inter_cfg.post_cb(frame, s_cfg.inter_cfg.cb_param);

    if (s_frame_count++ == 0) {
        printf("[%s] first frame: %ux%u fmt=%d len=%u\n", CAM_TAG, (unsigned)width, (unsigned)height, (int)fmt,
               (unsigned)len);
    }
}

/* ------------------------------------------------------------------------- */
/* DVP: J1 / video0                                                          */
/* ------------------------------------------------------------------------- */
#if defined(JIELI_CAMERA_PATH_DVP) && (JIELI_CAMERA_PATH_DVP == 1)

static void dvp_capture_thread(void *arg)
{
    struct video_format fmt;
    struct yuv_image    yuv;
    int                 err;

    (void)arg;

    /* The vendor's DVP examples open the YUV overlay channel as
     * "video0.2"; keep a bare "video0" fallback for profiles that register
     * the node without sub-devices. */
    s_video_dev = dev_open("video0.2", NULL);
    if (s_video_dev == NULL) {
        s_video_dev = dev_open("video0", NULL);
    }
    if (s_video_dev == NULL) {
        printf("[%s] video0 open failed (no DVP sensor?)\n", CAM_TAG);
        s_stopped = 1;
        return;
    }

    {
        char                      logo[16] = {0};
        struct camera_device_info info;

        memset(&info, 0, sizeof(info));
        if (dev_ioctl(s_video_dev, CAMERA_GET_SENSOR_TYPE, (u32)logo) == 0) {
            printf("[%s] sensor: %s\n", CAM_TAG, logo);
        }
        if (dev_ioctl(s_video_dev, CAMERA_GET_SENSOR_INFO, (u32)&info) == 0) {
            printf("[%s] sensor info: %ux%u fps=%u real_fps=%u\n", CAM_TAG, (unsigned)info.width, (unsigned)info.height,
                   (unsigned)info.fps, (unsigned)info.real_fps);
        }
    }

    memset(&fmt, 0, sizeof(fmt));
    fmt.type        = VIDEO_BUF_TYPE_VIDEO_OVERLAY;
    fmt.pixelformat = VIDEO_PIX_FMT_YUV422;
    fmt.src_width   = JIELI_CAMERA_WIDTH;
    fmt.src_height  = JIELI_CAMERA_HEIGHT;
    fmt.win.width   = JIELI_CAMERA_WIDTH;
    fmt.win.height  = JIELI_CAMERA_HEIGHT;
    fmt.fps         = s_cfg.fps;
    fmt.static_buf  = s_yuv_buf;
    fmt.sbuf_size   = s_yuv_buf_size;

    err = dev_ioctl(s_video_dev, VIDIOC_SET_FMT, (u32)&fmt);
    if (err) {
        printf("[%s] VIDIOC_SET_FMT failed: %d\n", CAM_TAG, err);
        goto out;
    }
    err = dev_ioctl(s_video_dev, VIDIOC_SET_OVERLAY, 0);
    if (err) {
        printf("[%s] VIDIOC_SET_OVERLAY failed: %d\n", CAM_TAG, err);
        goto out;
    }

    printf("[%s] DVP capture started\n", CAM_TAG);
    while (s_running) {
        memset(&yuv, 0, sizeof(yuv));
        err = dev_ioctl(s_video_dev, VIDIOC_GET_OVERLAY, (u32)&yuv);
        if (err || yuv.addr == NULL || yuv.size == 0) {
            tkl_system_sleep(5);
            continue;
        }
        camera_deliver_frame(yuv.addr, yuv.size, JIELI_CAMERA_WIDTH, JIELI_CAMERA_HEIGHT, TUYA_FRAME_FMT_YUV422);
        dev_ioctl(s_video_dev, VIDIOC_CLEAR_OVERLAY, 0);
    }

out:
    dev_close(s_video_dev);
    s_video_dev = NULL;
    printf("[%s] DVP capture stopped\n", CAM_TAG);
    s_stopped = 1;
}

static OPERATE_RET dvp_prepare(void)
{
    s_yuv_buf_size = (u32)JIELI_CAMERA_WIDTH * JIELI_CAMERA_HEIGHT * 2U;
    s_yuv_buf      = tkl_system_malloc(s_yuv_buf_size);
    if (s_yuv_buf == NULL) {
        printf("[%s] no memory for %u-byte YUV422 buffer\n", CAM_TAG, (unsigned)s_yuv_buf_size);
        return OPRT_MALLOC_FAILED;
    }
    return OPRT_OK;
}

static void dvp_release(void)
{
    if (s_yuv_buf != NULL) {
        tkl_system_free(s_yuv_buf);
        s_yuv_buf = NULL;
    }
}
#endif /* JIELI_CAMERA_PATH_DVP */

/* ------------------------------------------------------------------------- */
/* SPI: J4 / video1 + spi2                                                   */
/* ------------------------------------------------------------------------- */
#if defined(JIELI_CAMERA_PATH_SPI) && (JIELI_CAMERA_PATH_SPI == 1)

#include "asm/spi.h"

/* Vendor framing: one frame is FHEAD + H*(LHEAD+payload+LEND) + FEND. The
 * header/tail words live around every line, so a frame packet is
 * (width*2 + 8) * height + 8 bytes, not just width*2*height + 24. The SPI
 * DMA block must be at least one whole packet or READ_DATA never returns a
 * buffer whose first and last words are the frame tags.
 */
#define SPI_LINE_BYTES   ((u32)JIELI_CAMERA_WIDTH * 2U)
#define SPI_PACKET_BYTES ((SPI_LINE_BYTES + 8U) * (u32)JIELI_CAMERA_HEIGHT + 8U)
#define SPI_FRAME_BYTES  (SPI_LINE_BYTES * (u32)JIELI_CAMERA_HEIGHT)

/* The DMA buffer used to be a 311 KiB static .sram array (two packets). That
 * fits the link, but it sits in .ram0_bss from reset, cutting the internal
 * heap from ~464 KiB to ~152 KiB before the vendor network/BT stacks run.
 * Allocate one packet on demand instead: the heap is whole at boot and the
 * camera pays for the buffer only when it starts. */
static u8   *s_spi_buf;
static u8   *s_spi_buf_raw;
static u32   s_spi_buf_size;
static u8   *s_spi_frame;
static void *s_spi_dev    = NULL;
static void *s_sensor_dev = NULL;

static u8 *spi_aligned_alloc(u32 size, u8 **raw_out)
{
    u8 *raw = tkl_system_malloc(size + 32U);
    if (raw == NULL) {
        return NULL;
    }
    *raw_out = raw;
    return (u8 *)(((uintptr_t)raw + 31U) & ~(uintptr_t)31U);
}

static int spi_tag_eq(const u8 *p, u8 b3)
{
    return p[0] == 0xff && p[1] == 0x00 && p[2] == 0x00 && p[3] == b3;
}

static int spi_packet_is_frame(const u8 *buf, int len)
{
    if (len < 8) {
        return 0;
    }
    return (spi_tag_eq(buf, 0xab) || spi_tag_eq(buf, 0xa9)) &&
           (spi_tag_eq(buf + len - 4, 0xb6) || spi_tag_eq(buf + len - 4, 0xa6));
}

/* Copy only the line payloads into a contiguous YUYV image. */
static u32 spi_extract_frame(const u8 *buf, int len, u8 *out, u32 out_size)
{
    const u8 *p       = buf + 4; /* skip the frame header */
    const u8 *end     = buf + len;
    u32       written = 0;

    while (p + 8 <= end && written + SPI_LINE_BYTES <= out_size) {
        if (spi_tag_eq(p, 0x80)) {
            memcpy(out + written, p + 4, SPI_LINE_BYTES);
            written += SPI_LINE_BYTES;
            p += 4 + SPI_LINE_BYTES + 4;
        } else if (spi_tag_eq(p, 0xb6) || spi_tag_eq(p, 0xa6)) {
            break;
        } else {
            p += 4;
        }
    }
    return written;
}

static void spi_capture_thread(void *arg)
{
    struct spi_user su;
    u8             *read_addr;
    int             ret;

    (void)arg;

    /* The J4 sensor clock is PH2 (shared with the DVP path on purpose). The
     * vendor sensor driver configures its registers over iic1. The vendor's
     * DevKitBoard SPI-camera example drives PH2 with CH1_PLL_24M
     * (apps/common/example/Devkitboard_test/spi_camera.c:255); CH3 is the
     * wifi_camera board's PH8 channel and does not clock this pin. */
    printf("[%s] SPI: output XCLK PH2 (CH1_PLL_24M)\n", CAM_TAG);
    gpio_output_channle(IO_PORTH_02, CH1_PLL_24M);

    printf("[%s] SPI: opening video1.0\n", CAM_TAG);
    s_sensor_dev = dev_open("video1.0", NULL);
    if (s_sensor_dev == NULL) {
        printf("[%s] video1.0 open failed (no SPI sensor?)\n", CAM_TAG);
        s_stopped = 1;
        return;
    }
    printf("[%s] SPI: video1.0 ok\n", CAM_TAG);

    {
        char                      logo[16] = {0};
        struct camera_device_info info;

        memset(&info, 0, sizeof(info));
        if (dev_ioctl(s_sensor_dev, CAMERA_GET_SENSOR_TYPE, (u32)logo) == 0) {
            printf("[%s] sensor: %s\n", CAM_TAG, logo);
        }
        if (dev_ioctl(s_sensor_dev, CAMERA_GET_SENSOR_INFO, (u32)&info) == 0) {
            printf("[%s] sensor info: %ux%u fps=%u\n", CAM_TAG, (unsigned)info.width, (unsigned)info.height,
                   (unsigned)info.fps);
        }
    }

    printf("[%s] SPI: opening spi2\n", CAM_TAG);
    s_spi_dev = dev_open("spi2", NULL);
    if (s_spi_dev == NULL) {
        printf("[%s] spi2 open failed\n", CAM_TAG);
        goto out_sensor;
    }
    printf("[%s] SPI: spi2 ok\n", CAM_TAG);

    memset(&su, 0, sizeof(su));
    su.buf            = s_spi_buf;
    su.buf_size       = s_spi_buf_size;
    su.block_size     = SPI_PACKET_BYTES;
    su.first_dma_size = SPI_PACKET_BYTES;
    su.dma_max_cnt    = SPI_MAX_SIZE;

    dev_ioctl(s_spi_dev, IOCTL_SPI_SET_IRQ_CPU_ID, 1);
    printf("[%s] SPI: SET_USER_INFO buf=%p size=%u\n", CAM_TAG, s_spi_buf, (unsigned)s_spi_buf_size);
    dev_ioctl(s_spi_dev, IOCTL_SPI_SET_USER_INFO, (u32)&su);

    printf("[%s] SPI capture started (%ux%u, packet %u B)\n", CAM_TAG, JIELI_CAMERA_WIDTH, JIELI_CAMERA_HEIGHT,
           (unsigned)SPI_PACKET_BYTES);

    while (s_running) {
        read_addr = NULL;
        ret       = dev_ioctl(s_spi_dev, IOCTL_SPI_READ_DATA, (u32)&read_addr);
        if (ret <= 0 || read_addr == NULL) {
            continue;
        }
        if (spi_packet_is_frame(read_addr, ret)) {
            u32 got = spi_extract_frame(read_addr, ret, s_spi_frame, SPI_FRAME_BYTES);
            if (got == SPI_FRAME_BYTES) {
                camera_deliver_frame(s_spi_frame, got, JIELI_CAMERA_WIDTH, JIELI_CAMERA_HEIGHT, TUYA_FRAME_FMT_YUV422);
            } else {
                static u32 bad;
                if ((bad++ & 0x3f) == 0) {
                    printf("[%s] short line copy %u/%u bytes\n", CAM_TAG, (unsigned)got, (unsigned)SPI_FRAME_BYTES);
                }
            }
        } else {
            static u32 bad_frame;
            if ((bad_frame++ & 0x1f) == 0) {
                printf("[%s] bad packet head=%02x%02x%02x%02x len=%d\n", CAM_TAG, read_addr[0], read_addr[1],
                       read_addr[2], read_addr[3], ret);
            }
        }
        dev_ioctl(s_spi_dev, IOCTL_SPI_FREE_DATA, 0);
    }

    dev_close(s_spi_dev);
    s_spi_dev = NULL;
out_sensor:
    dev_close(s_sensor_dev);
    s_sensor_dev = NULL;
    gpio_clear_output_channle(IO_PORTH_02, CH1_PLL_24M);
    printf("[%s] SPI capture stopped\n", CAM_TAG);
    s_stopped = 1;
}

static OPERATE_RET spi_prepare(void)
{
    /* DMA buffer first: it is the one that most benefits from internal RAM. */
    s_spi_buf_size = SPI_PACKET_BYTES;
    s_spi_buf      = spi_aligned_alloc(s_spi_buf_size, &s_spi_buf_raw);
    if (s_spi_buf == NULL) {
        printf("[%s] no memory for %u-byte SPI DMA buffer\n", CAM_TAG, (unsigned)s_spi_buf_size);
        return OPRT_MALLOC_FAILED;
    }

    s_spi_frame = tkl_system_malloc(SPI_FRAME_BYTES);
    if (s_spi_frame == NULL) {
        printf("[%s] no memory for %u-byte SPI frame\n", CAM_TAG, (unsigned)SPI_FRAME_BYTES);
        tkl_system_free(s_spi_buf_raw);
        s_spi_buf     = NULL;
        s_spi_buf_raw = NULL;
        return OPRT_MALLOC_FAILED;
    }
    printf("[%s] SPI buffers: dma=%p (%u B) frame=%p (%u B)\n", CAM_TAG, s_spi_buf, (unsigned)s_spi_buf_size,
           s_spi_frame, (unsigned)SPI_FRAME_BYTES);
    return OPRT_OK;
}

static void spi_release(void)
{
    if (s_spi_frame != NULL) {
        tkl_system_free(s_spi_frame);
        s_spi_frame = NULL;
    }
    if (s_spi_buf != NULL) {
        /* Free the original allocation, not the aligned interior pointer. */
        tkl_system_free(s_spi_buf_raw);
        s_spi_buf     = NULL;
        s_spi_buf_raw = NULL;
    }
}
#endif /* JIELI_CAMERA_PATH_SPI */

/* ------------------------------------------------------------------------- */
/* TKL entry points                                                          */
/* ------------------------------------------------------------------------- */
OPERATE_RET tkl_dvp_init(TUYA_DVP_CFG_T *dvp_cfg)
{
    if (dvp_cfg == NULL) {
        return OPRT_INVALID_PARM;
    }
    memcpy(&s_cfg, dvp_cfg, sizeof(s_cfg));
    s_frame_id    = 0;
    s_frame_count = 0;

#if defined(JIELI_CAMERA_PATH_DVP) && (JIELI_CAMERA_PATH_DVP == 1)
    return dvp_prepare();
#elif defined(JIELI_CAMERA_PATH_SPI) && (JIELI_CAMERA_PATH_SPI == 1)
    return spi_prepare();
#else
    return OPRT_NOT_SUPPORTED;
#endif
}

OPERATE_RET tkl_dvp_start(void)
{
    if (s_thread != NULL) {
        return OPRT_OK;
    }
    s_running = 1;
    s_stopped = 0;

#if defined(JIELI_CAMERA_PATH_DVP) && (JIELI_CAMERA_PATH_DVP == 1)
    if (tkl_thread_create(&s_thread, "cam_dvp", 4096, 4, dvp_capture_thread, NULL) != OPRT_OK)
#elif defined(JIELI_CAMERA_PATH_SPI) && (JIELI_CAMERA_PATH_SPI == 1)
    if (tkl_thread_create(&s_thread, "cam_spi", 4096, 4, spi_capture_thread, NULL) != OPRT_OK)
#else
    if (1)
#endif
    {
        s_running = 0;
        return OPRT_COM_ERROR;
    }
    return OPRT_OK;
}

OPERATE_RET tkl_dvp_stop(void)
{
    int waited;

    if (s_thread == NULL) {
        return OPRT_OK;
    }
    s_running = 0;
    /* The capture thread closes its vendor devices (SPI2 -> sensor -> XCLK,
     * or video0) before it marks itself stopped. Wait for that teardown so a
     * later path switch sees PA2/PA3/PA4 and PH0-PH3 released, not a task that
     * was killed mid-ioctl still holding them. */
    for (waited = 0; waited < 100 && !s_stopped; waited++) {
        tkl_system_sleep(10);
    }
    if (!s_stopped) {
        printf("[%s] capture thread did not stop in 1s; releasing it\n", CAM_TAG);
    }
    tkl_thread_release(s_thread);
    s_thread = NULL;
    return OPRT_OK;
}

OPERATE_RET tkl_dvp_deinit(void)
{
    OPERATE_RET rt = tkl_dvp_stop();

#if defined(JIELI_CAMERA_PATH_DVP) && (JIELI_CAMERA_PATH_DVP == 1)
    dvp_release();
#elif defined(JIELI_CAMERA_PATH_SPI) && (JIELI_CAMERA_PATH_SPI == 1)
    spi_release();
#endif
    return rt;
}

#else /* not a WL82 camera build */

OPERATE_RET tkl_dvp_init(TUYA_DVP_CFG_T *dvp_cfg)
{
    (void)dvp_cfg;
    return OPRT_NOT_SUPPORTED;
}
OPERATE_RET tkl_dvp_deinit(void)
{
    return OPRT_NOT_SUPPORTED;
}
OPERATE_RET tkl_dvp_start(void)
{
    return OPRT_NOT_SUPPORTED;
}
OPERATE_RET tkl_dvp_stop(void)
{
    return OPRT_NOT_SUPPORTED;
}

#endif
