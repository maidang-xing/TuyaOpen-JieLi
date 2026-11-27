#include "tkl_ota.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "tkl_semaphore.h"
#include "tkl_system.h"
#include "tuya_error_code.h"
#include "asm/sfc_norflash_api.h"
#include "device/ioctl_cmds.h"

#if defined(JIELI_SELECTED_CHIP_WL83)
#include "generic/typedef.h"
#include "ota_impl.h"
#include "db_updata_api.h"
#else
#include "generic/typedef.h"
#include "dual_bank_updata_api.h"
#endif

/* The vendor engine copies each dev_write through an internal buffer whose
 * size it reports; a Tuya pack larger than that has to be split. */
#define JIELI_OTA_CHUNK_DEFAULT 4096u
#define JIELI_OTA_FINISH_TIMEOUT_MS 20000u

/* Advertised ceiling for a cloud image: one bank, as laid out by the vendor
 * downloader. Measured on the AC79 devkit (8 MiB flash, TuyaOpen span at
 * 0x700000): banks are 0x37C000 each, so that is the largest image the engine
 * can take. Re-read it from the flasher's FLASH INFO after moving the span. */
#define JIELI_OTA_IMAGE_MAX (0x37C000u)

/* The update bank does not necessarily start on a 4 KiB boundary: bank A begins
 * right after the boot area at 0x20020. The vendor engine programs whole
 * sectors, and its own Tuya port therefore feeds the misaligned head as a
 * separate first write before switching to aligned chunks
 * (apps/common/update/net_update.c). Same treatment here, or every write lands
 * 0x20 bytes off once the device runs from bank B. */
#if defined(CONFIG_DOUBLE_BANK_ENABLE) && !defined(JIELI_SELECTED_CHIP_WL83)
extern u32 get_target_udate_addr(void);
#endif

static uint8_t s_ota_active;
static uint32_t s_ota_chunk = JIELI_OTA_CHUNK_DEFAULT;
static TKL_SEM_HANDLE s_ota_finish_sem;
static volatile int s_ota_finish_state; /* 0 running, 1 done, -1 failed */
static uint8_t s_ota_head_buf[JIELI_OTA_CHUNK_DEFAULT];
static uint32_t s_ota_head_len;  /* bytes needed to reach the first 4 KiB line */
static uint32_t s_ota_head_fill; /* bytes of that head buffered so far */
static uint32_t s_ota_written;   /* bytes handed to the engine so far */
static uint32_t s_ota_stream_pos;/* offset of the next byte in the download stream */
static uint32_t s_ota_image_size;/* total stream length announced in start_notify */
static uint32_t s_ota_logged;    /* next progress threshold to print */
static uint8_t s_ota_warned;     /* "data before start" printed once */

/* What the cloud serves is a JL update container, not a raw image: a chain of
 * 32-byte JL_FILE_HEAD records ("update_data", "app_core", ...), each followed
 * by its data. The engine parses that container and writes only the app_core
 * payload into the update bank, so the payload — never the whole stream — is
 * what a read-back has to match. Verified against the 2.2.2 package: the bank
 * starts with the byte at stream offset 64 (header 2 + 32). */
#define JIELI_JL_HEAD_SIZE 32u
static uint32_t s_ota_payload_off;    /* first byte of app_core data in the stream */
static uint32_t s_ota_payload_len;    /* app_core data length */
static uint16_t s_ota_payload_hdr_crc;/* u16DataCrc declared by its header */
static uint16_t s_ota_crc;            /* CRC16-CCITT over the app_core payload */
static uint8_t s_ota_probe[8];        /* first bytes of that payload */
static uint8_t s_ota_probe_set;

/* The engine's own verify compares the read-back CRC against the fw_crc given
 * to dual_bank_passive_update_init(); the Tuya flow does not know the CRC when
 * the download starts, so that value is 0 and the engine verify can never
 * pass. Check the bank ourselves instead: the same CRC is accumulated while
 * writing and recomputed from the read-back data. */
static uint16_t jieli_ota_crc16(uint16_t crc, const uint8_t *data, uint32_t len)
{
    static const uint16_t tab[16] = {
        0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50A5, 0x60C6, 0x70E7,
        0x8108, 0x9129, 0xA14A, 0xB16B, 0xC18C, 0xD1AD, 0xE1CE, 0xF1EF,
    };

    while (len--) {
        crc = (uint16_t)((crc << 4) ^ tab[((crc >> 12) ^ (*data >> 4)) & 0x0Fu]);
        crc = (uint16_t)((crc << 4) ^ tab[((crc >> 12) ^ (*data & 0x0Fu)) & 0x0Fu]);
        data++;
    }
    return crc;
}

static int jieli_ota_burn_cb(int err)
{
    s_ota_finish_state = (err == 0) ? 1 : -1;
    printf("[JIELI_OTA] burn_boot_info err=%d\n", err);
    if (s_ota_finish_sem != NULL) {
        (void)tkl_semaphore_post(s_ota_finish_sem);
    }
    return 0;
}

/* Locate the app_core record in the container head and remember where its data
 * starts, how long it is and the CRC it declares for itself. */
static int jieli_ota_parse_payload(const uint8_t *buf, uint32_t len)
{
    uint32_t off;

    for (off = 0u; off + JIELI_JL_HEAD_SIZE <= len; off += JIELI_JL_HEAD_SIZE) {
        const uint8_t *head = &buf[off];
        uint32_t data_off = off + JIELI_JL_HEAD_SIZE;
        uint32_t data_len;

        if (memcmp(&head[16], "app_core", 9u) != 0) {
            continue;
        }
        memcpy(&data_len, &head[8], sizeof(data_len));
        if (data_len == 0u || data_len > JIELI_OTA_IMAGE_MAX ||
            data_off + data_len > s_ota_image_size) {
            printf("[JIELI_OTA] bad app_core hdr off=%u len=%u size=%u\n", (unsigned)data_off,
                   (unsigned)data_len, (unsigned)s_ota_image_size);
            return OPRT_INVALID_PARM;
        }
        s_ota_payload_hdr_crc = (uint16_t)(head[2] | ((uint16_t)head[3] << 8));
        s_ota_payload_off = data_off;
        s_ota_payload_len = data_len;
        printf("[JIELI_OTA] payload off=%u len=%u crc=%04X\n", (unsigned)data_off,
               (unsigned)data_len, (unsigned)s_ota_payload_hdr_crc);
        return OPRT_OK;
    }
    printf("[JIELI_OTA] no app_core record in the first %u bytes\n", (unsigned)len);
    return OPRT_NOT_FOUND;
}

#if defined(JIELI_SELECTED_CHIP_WL83)
static int jieli_ota_verify_cb(int result)
{
    printf("[JIELI_OTA] verify result=%d\n", result);
    if (result) {
        /* Read-back verified: program the boot info that points at this bank.
         * Completion is reported through jieli_ota_burn_cb(). A non-zero return
         * means the engine could not commit it, so the upgrade did not happen
         * even though the data verified. */
        int burn = (int)dual_bank_update_burn_boot_info(jieli_ota_burn_cb);
        if (burn != 0) {
            s_ota_finish_state = -1;
            if (s_ota_finish_sem != NULL) {
                (void)tkl_semaphore_post(s_ota_finish_sem);
            }
            return -1;
        }
        return 0;
    }
    s_ota_finish_state = -1;
    if (s_ota_finish_sem != NULL) {
        (void)tkl_semaphore_post(s_ota_finish_sem);
    }
    return -1;
}
#endif /* JIELI_SELECTED_CHIP_WL83 */

static uint32_t jieli_ota_expected_pos(void)
{
    return (s_ota_head_fill < s_ota_head_len) ? s_ota_head_fill : s_ota_stream_pos;
}

static void jieli_ota_cleanup(void)
{
    (void)dual_bank_passive_update_exit(NULL);
    s_ota_active = 0;
    s_ota_head_len = 0u;
    s_ota_head_fill = 0u;
    s_ota_written = 0u;
    s_ota_stream_pos = 0u;
    s_ota_payload_off = 0u;
    s_ota_payload_len = 0u;
    s_ota_logged = 0u;
    if (s_ota_finish_sem != NULL) {
        (void)tkl_semaphore_release(s_ota_finish_sem);
        s_ota_finish_sem = NULL;
    }
}

OPERATE_RET tkl_ota_get_ability(uint32_t *image_size, TUYA_OTA_TYPE_E *type)
{
#if !defined(CONFIG_DOUBLE_BANK_ENABLE)
    /* The board did not turn on JIELI_BOARD_DUAL_BANK, so the staged vendor
     * image has no second bank to write into. */
    (void)image_size;
    (void)type;
    return OPRT_NOT_SUPPORTED;
#else
    if (NULL == image_size || NULL == type) {
        return OPRT_INVALID_PARM;
    }
    *image_size = JIELI_OTA_IMAGE_MAX;
    *type = TUYA_OTA_FULL;
    return OPRT_OK;
#endif
}

OPERATE_RET tkl_ota_start_notify(uint32_t image_size, TUYA_OTA_TYPE_E type, TUYA_OTA_PATH_E path)
{
    uint32_t max_buf;
    int ret;

    (void)path;
#if !defined(CONFIG_DOUBLE_BANK_ENABLE)
    (void)image_size;
    (void)type;
    (void)max_buf;
    (void)ret;
    return OPRT_NOT_SUPPORTED;
#else
    if (image_size == 0u || image_size > JIELI_OTA_IMAGE_MAX) {
        printf("[JIELI_OTA] start rejected size=%u max=%u\n", (unsigned)image_size,
               (unsigned)JIELI_OTA_IMAGE_MAX);
        return OPRT_INVALID_PARM;
    }
    if (type != TUYA_OTA_FULL) {
        /* The vendor engine takes a full image; it has its own patching path
         * but the Tuya difference package is not what it expects. */
        printf("[JIELI_OTA] start rejected type=%d\n", (int)type);
        return OPRT_NOT_SUPPORTED;
    }
    if (s_ota_active) {
        if (image_size == s_ota_image_size) {
            /* http_download fires DL_EVENT_ON_FILESIZE again on each Range
             * retry; tearing down the engine here would restart at offset 0
             * while the stream resumes mid-file. */
            printf("[JIELI_OTA] start resumed size=%u pos=%u\n", (unsigned)image_size,
                   (unsigned)jieli_ota_expected_pos());
            return OPRT_OK;
        }
        jieli_ota_cleanup();
    }
    /* The USB downloader leaves the flash write-protected, and the vendor OTA
     * engine does not lift it itself: every write to the target bank would
     * fail and the transfer would be abandoned after the first buffer. */
    (void)norflash_open(NULL, NULL, NULL);
    (void)norflash_ioctl(NULL, IOCTL_SET_WRITE_PROTECT, 0);
    ret = dual_bank_passive_update_init(0u, image_size, (uint16_t)JIELI_OTA_CHUNK_DEFAULT, NULL);
    if (ret != 0) {
        printf("[JIELI_OTA] init failed ret=%d size=%u\n", ret, (unsigned)image_size);
        return OPRT_COM_ERROR;
    }
    max_buf = get_dual_bank_passive_update_max_buf();
    s_ota_chunk = (max_buf >= 256u) ? max_buf : JIELI_OTA_CHUNK_DEFAULT;
    if (dual_bank_update_allow_check(image_size) != 0) {
        printf("[JIELI_OTA] allow_check failed size=%u\n", (unsigned)image_size);
        (void)dual_bank_passive_update_exit(NULL);
        return OPRT_COM_ERROR;
    }
#if defined(CONFIG_DOUBLE_BANK_ENABLE) && !defined(JIELI_SELECTED_CHIP_WL83)
    {
        /* Bytes between the update bank start and the first 4 KiB line; the
         * head is written as one call so every later write is aligned. */
        uint32_t target = get_target_udate_addr();
        uint32_t aligned = (target + JIELI_OTA_CHUNK_DEFAULT - 1u) & ~(JIELI_OTA_CHUNK_DEFAULT - 1u);

        s_ota_head_len = aligned - target;
        s_ota_head_fill = 0u;
    }
#else
    s_ota_head_len = 0u;
    s_ota_head_fill = 0u;
#endif
    s_ota_written = 0u;
    s_ota_stream_pos = 0u;
    s_ota_image_size = image_size;
    s_ota_payload_off = 0u;
    s_ota_payload_len = 0u;
    s_ota_payload_hdr_crc = 0u;
    s_ota_logged = 256u * 1024u;
    s_ota_crc = 0xFFFFu;
    s_ota_probe_set = 0u;
    s_ota_warned = 0u;
    s_ota_active = 1;
    printf("[JIELI_OTA] start ok size=%u chunk=%u max_buf=%u head=%u\n", (unsigned)image_size,
           (unsigned)s_ota_chunk, (unsigned)max_buf, (unsigned)s_ota_head_len);
    return OPRT_OK;
#endif
}

/* dual_bank_update_write() returns 0 on success and a non-zero error code on
 * failure; the vendor's own Tuya port aborts on any non-zero value. */
static OPERATE_RET jieli_ota_write(const uint8_t *data, uint32_t len)
{
    uint32_t pos = s_ota_stream_pos;
    int ret = (int)dual_bank_update_write((void *)data, (uint16_t)len, NULL);

    if (ret != 0) {
        printf("[JIELI_OTA] write failed ret=%d len=%u\n", ret, (unsigned)len);
        return OPRT_COM_ERROR;
    }
    s_ota_written += len;
    s_ota_stream_pos += len;
    if (s_ota_payload_len != 0u) {
        /* Only the app_core payload lands in the bank, so only that slice of
         * the stream is what the read-back can be compared against. */
        uint32_t from = (pos > s_ota_payload_off) ? pos : s_ota_payload_off;
        uint32_t to = pos + len;
        uint32_t end = s_ota_payload_off + s_ota_payload_len;

        if (to > end) {
            to = end;
        }
        if (from < to) {
            const uint8_t *p = &data[from - pos];
            uint32_t n = to - from;

            s_ota_crc = jieli_ota_crc16(s_ota_crc, p, n);
            if (!s_ota_probe_set && n >= sizeof(s_ota_probe)) {
                memcpy(s_ota_probe, p, sizeof(s_ota_probe));
                s_ota_probe_set = 1u;
            }
        }
    }
    if (s_ota_written >= s_ota_logged) {
        printf("[JIELI_OTA] written %u\n", (unsigned)s_ota_written);
        s_ota_logged += 256u * 1024u;
    }
    return OPRT_OK;
}

OPERATE_RET tkl_ota_data_process(TUYA_OTA_DATA_T *pack, uint32_t *remain_len)
{
    uint32_t offset = 0u;

    if (remain_len != NULL) {
        *remain_len = 0u;
    }
    if (!s_ota_active || pack == NULL || pack->data == NULL || pack->len == 0u) {
        if (!s_ota_warned) {
            s_ota_warned = 1u;
            printf("[JIELI_OTA] data before start (active=%u len=%u)\n", (unsigned)s_ota_active,
                   (pack != NULL) ? (unsigned)pack->len : 0u);
        }
        return OPRT_INVALID_PARM;
    }
    {
        uint32_t expected = jieli_ota_expected_pos();

        if (pack->offset == 0u && expected != 0u) {
            /* A fresh download started from byte 0 with the same size as an
             * interrupted attempt; reset the engine cleanly before writing. */
            uint32_t size = s_ota_image_size;

            jieli_ota_cleanup();
            if (tkl_ota_start_notify(size, TUYA_OTA_FULL, TUYA_OTA_PATH_AIR) != OPRT_OK) {
                return OPRT_COM_ERROR;
            }
            expected = 0u;
        } else if (pack->offset > expected) {
            printf("[JIELI_OTA] offset gap off=%u expected=%u\n", (unsigned)pack->offset, (unsigned)expected);
            return OPRT_COM_ERROR;
        } else if (pack->offset < expected) {
            uint32_t skip = expected - pack->offset;

            if (skip >= pack->len) {
                return OPRT_OK;
            }
            offset = skip;
        }
    }
    while (offset < pack->len) {
        uint32_t chunk;

        if (s_ota_head_fill < s_ota_head_len) {
            /* Collect the misaligned head first and hand it to the engine as a
             * single write, exactly like the vendor's own Tuya port. */
            uint32_t take = s_ota_head_len - s_ota_head_fill;
            uint32_t left = pack->len - offset;

            if (take > left) {
                take = left;
            }
            memcpy(&s_ota_head_buf[s_ota_head_fill], pack->data + offset, take);
            s_ota_head_fill += take;
            offset += take;
            if (s_ota_head_fill < s_ota_head_len) {
                break; /* wait for the rest of the head */
            }
            /* The container header is inside this first block; parse it before
             * any payload byte reaches the engine so the CRC covers exactly
             * what the engine writes into the bank. */
            if (s_ota_payload_len == 0u) {
                (void)jieli_ota_parse_payload(s_ota_head_buf, s_ota_head_len);
            }
            if (jieli_ota_write(s_ota_head_buf, s_ota_head_len) != OPRT_OK) {
                return OPRT_COM_ERROR;
            }
            continue;
        }

        chunk = pack->len - offset;
        if (chunk > s_ota_chunk) {
            chunk = s_ota_chunk;
        }
        if (jieli_ota_write(pack->data + offset, chunk) != OPRT_OK) {
            return OPRT_COM_ERROR;
        }
        offset += chunk;
    }
    return OPRT_OK;
}

OPERATE_RET tkl_ota_end_notify(BOOL_T reset)
{
    OPERATE_RET rt = OPRT_COM_ERROR;

    if (!s_ota_active) {
        printf("[JIELI_OTA] end without start\n");
        return OPRT_INVALID_PARM;
    }
    /* An image shorter than the head would leave it buffered; hand the partial
     * head over before verifying so the engine sees every byte. */
    if (s_ota_head_fill != 0u && s_ota_head_fill < s_ota_head_len) {
        if (s_ota_payload_len == 0u) {
            (void)jieli_ota_parse_payload(s_ota_head_buf, s_ota_head_fill);
        }
        if (jieli_ota_write(s_ota_head_buf, s_ota_head_fill) != OPRT_OK) {
            jieli_ota_cleanup();
            return OPRT_COM_ERROR;
        }
        s_ota_head_fill = s_ota_head_len;
    }
    s_ota_finish_state = 0;
    if (tkl_semaphore_create_init(&s_ota_finish_sem, 0u, 1u) != OPRT_OK) {
        printf("[JIELI_OTA] end: semaphore init failed\n");
        jieli_ota_cleanup();
        return OPRT_COM_ERROR;
    }
#if defined(JIELI_SELECTED_CHIP_WL83)
    {
        int vret = (int)dual_bank_update_verify(NULL, NULL, jieli_ota_verify_cb);

        printf("[JIELI_OTA] end: written=%u verify_ret=%d\n", (unsigned)s_ota_written, vret);
        if (vret == 0) {
            if (tkl_semaphore_wait(s_ota_finish_sem, JIELI_OTA_FINISH_TIMEOUT_MS) != OPRT_OK) {
                printf("[JIELI_OTA] end: verify/burn timeout state=%d\n", s_ota_finish_state);
            }
        }
    }
#else
    {
        /* Read the app_core payload back out of the update bank and compare it
         * with the payload we handed to the engine. The engine's own verify
         * cannot be used: it compares against the fw_crc passed to init(), and
         * the cloud flow does not know the image CRC when the download starts,
         * so that value is 0 and its verify can never pass. */
        uint32_t total = s_ota_payload_len;
        uint32_t off = 0u;
        uint16_t read_crc = 0xFFFFu;
        uint16_t flash_word = 0u;
        uint16_t probe_word = 0u;
        int read_err = 0;
        int match = 0;

        tkl_system_sleep(200u);
#if defined(CONFIG_DOUBLE_BANK_ENABLE) && !defined(JIELI_SELECTED_CHIP_WL83)
        {
            /* One direct read of the update area, bypassing the engine, to show
             * that the bank really starts with the payload. */
            uint32_t tgt = get_target_udate_addr();

            (void)norflash_origin_read(s_ota_head_buf, tgt, 8u);
            flash_word = (uint16_t)((s_ota_head_buf[0] << 8) | s_ota_head_buf[1]);
            printf("[JIELI_OTA] tgt=%X head=%u\n", (unsigned)tgt, (unsigned)s_ota_head_len);
        }
#endif
        if (s_ota_payload_len == 0u) {
            printf("[JIELI_OTA] end: no app_core payload known, not verifying\n");
        }
        while (off < total) {
            uint32_t n = total - off;

            if (n > sizeof(s_ota_head_buf)) {
                n = sizeof(s_ota_head_buf);
            }
            if ((uint32_t)dual_bank_update_read_data(off, s_ota_head_buf, n) != n) {
                read_err = 1;
                break;
            }
            if (off == 0u) {
                probe_word = (uint16_t)((s_ota_head_buf[0] << 8) | s_ota_head_buf[1]);
            }
            read_crc = jieli_ota_crc16(read_crc, s_ota_head_buf, n);
            off += n;
        }
        printf("[JIELI_OTA] flash=%02X%02X read=%02X%02X want=%02X%02X\n", (unsigned)(flash_word >> 8),
               (unsigned)(flash_word & 0xFFu), (unsigned)(probe_word >> 8),
               (unsigned)(probe_word & 0xFFu), (unsigned)s_ota_probe[0],
               (unsigned)s_ota_probe[1]);
        printf("[JIELI_OTA] crc=%04X read=%04X hdr=%04X\n", (unsigned)s_ota_crc,
               (unsigned)read_crc, (unsigned)s_ota_payload_hdr_crc);
        if (!read_err && total != 0u && read_crc == s_ota_crc) {
            match = 1;
        }
        printf("[JIELI_OTA] end: written=%u payload=%u match=%d err=%d\n", (unsigned)s_ota_written,
               (unsigned)s_ota_payload_len, match, read_err);
        if (match != 0) {
            int burn = (int)dual_bank_update_burn_boot_info(jieli_ota_burn_cb);

            printf("[JIELI_OTA] burn_boot_info ret=%d\n", burn);
            if (burn == 0) {
                if (tkl_semaphore_wait(s_ota_finish_sem, JIELI_OTA_FINISH_TIMEOUT_MS) != OPRT_OK) {
                    printf("[JIELI_OTA] end: burn timeout state=%d\n", s_ota_finish_state);
                }
            } else {
                s_ota_finish_state = -1;
            }
        } else {
            s_ota_finish_state = -1;
        }
    }
#endif /* JIELI_SELECTED_CHIP_WL83 */
    if (s_ota_finish_state == 1 && reset) {
        printf("[JIELI_OTA] end: resetting into the new bank\n");
        /* Leave the engine alone; the reset drops the handle with the task. */
        tkl_system_reset();
    }
    printf("[JIELI_OTA] end: state=%d reset=%d\n", s_ota_finish_state, (int)reset);
    if (s_ota_finish_state == 1) {
        rt = OPRT_OK;
    }
    jieli_ota_cleanup();
    return rt;
}

OPERATE_RET tkl_ota_get_old_firmware_info(TUYA_OTA_FIRMWARE_INFO_T **info)
{
    if (info) {
        *info = NULL;
    }
    return OPRT_NOT_SUPPORTED;
}
